import json
from dataclasses import dataclass
from decimal import Decimal

from outbox.send import create_draft
from proxy.app import Claim, begin_step, finish_step, over_budget, write_denial
from runtime.citations import parse_cites
from runtime.memory import MemoryRefused, memory_write
from runtime.policy import RunPolicy
from runtime.scheduler import IllegalTransition, transition

PROXY_TOOLS = frozenset({"mail.list", "mail.read", "docs.read", "memory.write", "draft.create"})
TAKEOVER_SECONDS = 30


@dataclass(frozen=True)
class Bound:
    tenant_id: str
    run_id: str
    generation: int
    policy: RunPolicy


@dataclass(frozen=True)
class Proceed:
    """This caller won the step and may do the work."""

    claim: Claim
    uncertain: bool


@dataclass(frozen=True)
class Finished:
    status: int
    body: dict


def load_bound(conn, found: dict) -> Bound | None:
    """Read the run's stored policy once per request. The request body never supplies it."""
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (found["tenant_id"],))
    row = conn.execute("SELECT max_usd, tools FROM runs WHERE id = %s", (found["run_id"],)).fetchone()
    if row is None:
        return None
    policy = RunPolicy(Decimal(row[0]), frozenset(row[1]))
    return Bound(found["tenant_id"], found["run_id"], found["generation"], policy)


def write_event(conn, tenant: str, run_id: str, event: str, detail: dict) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute(
        """
        INSERT INTO run_events (tenant_id, run_id, event, detail)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        (tenant, run_id, event, json.dumps(detail)),
    )


def claim_step(conn, bound: Bound, step_no: int, digest: str, model: str, estimate: Decimal | None) -> Proceed | Finished:
    """Claim one journal row. estimate None skips the budget check (tool steps).

    The run row is locked for the claim, so the budget sum and the reservation are one decision
    across both proxies. The transaction commits before the caller does the work.
    """
    tenant, run_id = bound.tenant_id, bound.run_id
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        conn.execute("SELECT 1 FROM runs WHERE id = %s FOR UPDATE", (run_id,))
        existing = conn.execute(
            """
            SELECT status, response, generation, started_at,
                   now() - started_at >= make_interval(secs => %s)
            FROM steps WHERE run_id = %s AND step_no = %s AND arg_hash = %s
            """,
            (TAKEOVER_SECONDS, run_id, step_no, digest),
        ).fetchone()
        if existing is not None:
            status, response, generation, started_at, stale = existing
            if status == "COMPLETED":
                write_event(conn, tenant, run_id, "step_skipped", {"step_no": step_no})
                return Finished(200, {"skipped": True, **_as_dict(response)})
            if status == "STARTED" and generation > bound.generation:
                return Finished(409, {"error": "stale_generation"})
            if status == "STARTED" and generation == bound.generation and not stale:
                return Finished(202, {"in_progress": True})
        if estimate is not None:
            spent = conn.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0) FROM steps
                WHERE run_id = %s AND NOT (step_no = %s AND arg_hash = %s)
                """,
                (run_id, step_no, digest),
            ).fetchone()[0]
            if over_budget(spent, estimate, bound.policy.max_usd):
                write_denial(conn, tenant, "budget", {"run_id": run_id, "model": model, "estimate": str(estimate)})
                write_event(conn, tenant, run_id, "budget_denied", {"estimate": str(estimate)})
                return Finished(402, {"error": "budget", "estimate": str(estimate)})
        reserve = Decimal(0) if estimate is None else estimate
        if existing is None:
            claim = begin_step(conn, tenant, run_id, step_no, digest, model, reserve, bound.generation)
            if claim is None:
                return Finished(202, {"in_progress": True})
            return Proceed(claim, False)
        uncertain = status == "STARTED"
        row = conn.execute(
            """
            UPDATE steps
            SET status = 'STARTED', generation = %s, started_at = clock_timestamp(),
                cost_usd = %s, model = %s, cost_uncertain = cost_uncertain OR %s
            WHERE run_id = %s AND step_no = %s AND arg_hash = %s
              AND status = %s AND generation = %s AND started_at = %s
            RETURNING generation, started_at
            """,
            (bound.generation, reserve, model, uncertain, run_id, step_no, digest, status, generation, started_at),
        ).fetchone()
        if row is None:
            return Finished(202, {"in_progress": True})
        if uncertain:
            write_event(conn, tenant, run_id, "cost_uncertain", {"step_no": step_no})
        return Proceed(Claim(row[0], row[1]), uncertain)


def admit_tool(conn, bound: Bound, name: str, args: dict, step_no: int, digest: str) -> Finished:
    tenant, run_id = bound.tenant_id, bound.run_id
    if name == "py.exec":
        return _deny(conn, bound, name, "not_executed")
    if name not in PROXY_TOOLS or name not in bound.policy.tools:
        return _deny(conn, bound, name, "tool_not_allowed")
    claimed = claim_step(conn, bound, step_no, digest, "tool", None)
    if isinstance(claimed, Finished):
        return claimed
    result = perform(conn, bound, name, args)
    if result.get("denied"):
        write_denial(conn, tenant, result["reason"], {"run_id": run_id, "tool": name})
        write_event(conn, tenant, run_id, "policy_denied", {"tool": name, "reason": result["reason"]})
    finish_step(conn, tenant, run_id, step_no, digest, "COMPLETED", result, claimed.claim)
    if not result.get("denied"):
        write_event(conn, tenant, run_id, "step_completed", {"step_no": step_no, "tool": name})
    return Finished(200, {"skipped": False, **result})


def perform(conn, bound: Bound, name: str, args: dict) -> dict:
    tenant = bound.tenant_id
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    if name == "mail.list":
        rows = conn.execute(
            """
            SELECT id, from_addr, to_addr, subject, body, attachment_id
            FROM mail WHERE tenant_id = %s ORDER BY id
            """,
            (tenant,),
        ).fetchall()
        return {"messages": [_mail(row, tenant) for row in rows], "domains": _domains(conn, tenant)}
    if name == "mail.read":
        ids = [str(item) for item in args.get("ids") or []]
        rows = conn.execute(
            """
            SELECT id, from_addr, to_addr, subject, body, attachment_id
            FROM mail WHERE tenant_id = %s AND id = ANY(%s) ORDER BY id
            """,
            (tenant, ids),
        ).fetchall()
        if len({row[0] for row in rows}) != len(set(ids)):
            return {"denied": True, "reason": "foreign_id"}
        return {"messages": [_mail(row, tenant) for row in rows]}
    if name == "docs.read":
        row = conn.execute(
            "SELECT id, name, body FROM documents WHERE tenant_id = %s AND id = %s",
            (tenant, args.get("id")),
        ).fetchone()
        if row is None:
            return {"denied": True, "reason": "foreign_id"}
        return {"document": {"id": row[0], "name": row[1], "body": row[2]}}
    if name == "draft.create":
        draft = create_draft(
            conn,
            tenant,
            args["draft_id"],
            args["to"],
            args.get("subject") or "brief",
            args.get("body") or "",
            tuple(args.get("attachments") or ()),
        )
        return {"draft": draft}
    if name == "memory.write":
        kind = args.get("kind") or "fact"
        if kind == "cursor":
            return {"denied": True, "reason": "cursor_on_success_only"}
        try:
            memory_id = memory_write(
                conn,
                tenant,
                args["agent_id"],
                kind,
                args.get("body") or "",
                tuple(args.get("source_ids") or ()),
                bound.run_id,
            )
        except MemoryRefused as exc:
            return {"denied": True, "reason": exc.reason}
        return {"memory_id": memory_id}
    return {"denied": True, "reason": "tool_not_allowed"}


def finish_state(conn, bound: Bound, state: str, artifact: str) -> Finished:
    """One transaction for the state, the artifact, and the cursor."""
    tenant, run_id = bound.tenant_id, bound.run_id
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        row = conn.execute("SELECT state, agent_id FROM runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
        if row is None:
            return Finished(404, {"error": "run"})
        current, agent_id = row
        cursor = None
        if state == "SUCCEEDED":
            cited = parse_cites(artifact)
            mail_ids = _owned(conn, "mail", tenant, cited)
            doc_ids = _owned(conn, "documents", tenant, cited)
            if not cited or any(item not in mail_ids and item not in doc_ids for item in cited):
                state = "FAILED"
            else:
                cursor = next((item for item in reversed(cited) if item in mail_ids), None)
        try:
            nxt = transition(current, state)
        except IllegalTransition as exc:
            return Finished(409, {"error": "illegal_transition", "from": exc.src, "to": exc.dst})
        conn.execute("UPDATE runs SET state = %s, artifact = %s WHERE id = %s", (nxt, artifact, run_id))
        if cursor is not None:
            memory_write(conn, tenant, agent_id, "cursor", cursor, (cursor,), run_id)
        write_event(conn, tenant, run_id, "state", {"state": nxt})
    return Finished(200, {"state": nxt})


def _deny(conn, bound: Bound, name: str, reason: str) -> Finished:
    write_denial(conn, bound.tenant_id, reason, {"run_id": bound.run_id, "tool": name})
    write_event(conn, bound.tenant_id, bound.run_id, "policy_denied", {"tool": name, "reason": reason})
    return Finished(200, {"denied": True, "reason": reason})


def _owned(conn, table: str, tenant: str, ids: tuple[str, ...] | None) -> set[str]:
    if not ids:
        return set()
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE tenant_id = %s AND id = ANY(%s)",
        (tenant, list(ids)),
    ).fetchall()
    return {row[0] for row in rows}


def _domains(conn, tenant: str) -> list[str]:
    row = conn.execute("SELECT domains FROM tenants WHERE tenant_id = %s", (tenant,)).fetchone()
    return list(row[0]) if row else []


def _as_dict(response) -> dict:
    if isinstance(response, str):
        response = json.loads(response)
    return response if isinstance(response, dict) else {}


def _mail(row, tenant: str) -> dict:
    return {
        "id": row[0],
        "tenant_id": tenant,
        "from_addr": row[1],
        "to_addr": row[2],
        "subject": row[3],
        "body": row[4],
        "attachment_id": row[5],
    }
