import json
from dataclasses import dataclass
from decimal import Decimal

from outbox.send import create_draft
from proxy.app import Claim, begin_step, finish_step, over_budget, write_denial
from runtime.citations import parse_cites
from runtime.memory import MemoryRefused, memory_write
from runtime.policy import RunPolicy
from runtime.scheduler import IllegalTransition, transition

NO_NEW_MAIL = "no new mail"
PROXY_TOOLS = frozenset({"mail.list", "mail.read", "docs.read", "memory.write", "draft.create"})


@dataclass(frozen=True)
class Bound:
    tenant_id: str
    run_id: str
    generation: int
    policy: RunPolicy
    project_id: str | None = None


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
    row = conn.execute(
        """
        SELECT runs.max_usd, runs.tools, tenants.gateway_project_id
        FROM runs JOIN tenants ON tenants.tenant_id = runs.tenant_id
        WHERE runs.id = %s
        """,
        (found["run_id"],),
    ).fetchone()
    if row is None:
        return None
    policy = RunPolicy(Decimal(row[0]), frozenset(row[1]))
    return Bound(found["tenant_id"], found["run_id"], found["generation"], policy, row[2])


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
            SELECT status, response, generation, started_at
            FROM steps WHERE run_id = %s AND step_no = %s AND arg_hash = %s
            """,
            (run_id, step_no, digest),
        ).fetchone()
        if existing is not None:
            status, response, generation, started_at = existing
            if status == "COMPLETED":
                write_event(conn, tenant, run_id, "step_skipped", {"step_no": step_no})
                return Finished(200, {"skipped": True, **_as_dict(response)})
            if status == "STARTED" and generation >= bound.generation:
                if generation > bound.generation:
                    return Finished(409, {"error": "stale_generation"})
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
        agent_id = _agent(conn, bound.run_id)
        rows = _mail_after_cursor(conn, tenant, agent_id)
        result = {"messages": [_mail(row, tenant) for row in rows], "domains": _domains(conn, tenant)}
        facts = _facts(conn, tenant, agent_id)
        if facts:
            result["facts"] = facts
        return result
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
            if not cited and artifact.strip() == NO_NEW_MAIL and not _mail_after_cursor(conn, tenant, agent_id):
                pass  # nothing past the cursor: an empty brief is correct, checked here rather than trusted
            elif not cited or any(item not in mail_ids and item not in doc_ids for item in cited):
                state = "FAILED"
            else:
                cursor = _latest(conn, tenant, [item for item in cited if item in mail_ids])
                if cursor is not None and not _is_after_cursor(conn, tenant, agent_id, cursor):
                    cursor = None  # the cursor only moves forward by (received_at, id)
        try:
            nxt = transition(current, state)
        except IllegalTransition as exc:
            return Finished(409, {"error": "illegal_transition", "from": exc.src, "to": exc.dst})
        conn.execute("UPDATE runs SET state = %s, artifact = %s WHERE id = %s", (nxt, artifact, run_id))
        if cursor is not None:
            memory_write(conn, tenant, agent_id, "cursor", cursor, (cursor,), run_id)
        write_event(conn, tenant, run_id, "state", {"state": nxt})
    return Finished(200, {"state": nxt})


def _agent(conn, run_id: str) -> str:
    return conn.execute("SELECT agent_id FROM runs WHERE id = %s", (run_id,)).fetchone()[0]


def _mail_after_cursor(conn, tenant: str, agent_id: str) -> list:
    """The tenant's mail after this agent's stored cursor, oldest first; all of it when no cursor."""
    return conn.execute(
        """
        WITH c AS (
            SELECT m.received_at, m.id FROM memory k
            JOIN mail m ON m.id = k.body AND m.tenant_id = k.tenant_id
            WHERE k.tenant_id = %s AND k.agent_id = %s AND k.kind = 'cursor'
            ORDER BY m.received_at DESC, m.id DESC LIMIT 1
        )
        SELECT mail.id, from_addr, to_addr, subject, body, attachment_id
        FROM mail LEFT JOIN c ON true
        WHERE mail.tenant_id = %s
          AND (c.id IS NULL OR (mail.received_at, mail.id) > (c.received_at, c.id))
        ORDER BY mail.received_at, mail.id
        """,
        (tenant, agent_id, tenant),
    ).fetchall()


def _is_after_cursor(conn, tenant: str, agent_id: str, mail_id: str) -> bool:
    row = conn.execute(
        """
        SELECT NOT EXISTS (
            SELECT 1 FROM memory k
            JOIN mail c ON c.id = k.body AND c.tenant_id = k.tenant_id
            JOIN mail m ON m.id = %s AND m.tenant_id = k.tenant_id
            WHERE k.tenant_id = %s AND k.agent_id = %s AND k.kind = 'cursor'
              AND (c.received_at, c.id) >= (m.received_at, m.id)
        )
        """,
        (mail_id, tenant, agent_id),
    ).fetchone()
    return bool(row[0])


def _latest(conn, tenant: str, ids: list) -> str | None:
    if not ids:
        return None
    row = conn.execute(
        "SELECT id FROM mail WHERE tenant_id = %s AND id = ANY(%s) ORDER BY received_at DESC, id DESC LIMIT 1",
        (tenant, ids),
    ).fetchone()
    return row[0] if row else None


def _facts(conn, tenant: str, agent_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT body FROM memory
        WHERE tenant_id = %s AND agent_id = %s AND kind = 'fact' AND NOT flagged
        ORDER BY created_at, id
        """,
        (tenant, agent_id),
    ).fetchall()
    return [row[0] for row in rows]


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
