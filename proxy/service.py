import json
from dataclasses import dataclass, field

import psycopg

from agent.tools import py_exec
from proxy.app import Call, begin_step, decide, finish_step, resolve, write_denial
from proxy.providers import Cassette, Completion, Provider, ReplayMiss, estimate_usd, step_digest

PROVIDER_HOST = "provider.internal"


@dataclass
class ProxyContext:
    database_url: str
    cassette: Cassette
    mode: str
    name: str
    upstream: object | None = None
    faults: list = field(default_factory=list)


def dispatch(ctx: ProxyContext, method: str, path: str, raw: bytes, source_ip: str) -> tuple[int, dict]:
    if method == "GET" and path == "/health":
        return 200, {"ok": True, "name": ctx.name, "mode": ctx.mode}
    if method == "GET" and path == "/whoami":
        return 200, {"source_ip": source_ip}
    if method != "POST":
        return 404, {"error": "not_found"}
    try:
        payload = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError:
        return 400, {"error": "bad_json"}
    with psycopg.connect(ctx.database_url, autocommit=True) as conn:
        if path == "/v1/faults":
            ctx.faults[:] = list(payload.get("faults") or [])
            return 200, {"armed": list(ctx.faults)}
        if path == "/v1/complete":
            return complete(ctx, conn, payload, source_ip)
        if path == "/v1/tool":
            return tool(ctx, conn, payload, source_ip)
        if path == "/v1/state":
            return set_state(conn, payload, source_ip)
        if path == "/v1/event":
            return add_event(conn, payload, source_ip)
    return 404, {"error": "not_found"}


def complete(ctx: ProxyContext, conn, payload: dict, source_ip: str) -> tuple[int, dict]:
    bound = _bound(conn, payload, source_ip)
    if bound is None:
        return 403, {"error": "token"}
    tenant, run_id = bound
    model = payload.get("model") or "primary"
    fault = _pop_fault(ctx, model)
    if fault:
        write_event(conn, tenant, run_id, "provider_" + fault, {"simulated": True, "model": model})
        status = 429 if fault == "429" else 503
        return status, {"error": fault, "simulated": True, "model": model}
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    digest = step_digest(messages, tools)
    step_no = int(payload["step_no"])
    existing = _step(conn, tenant, run_id, step_no, digest)
    if existing and existing["status"] == "COMPLETED":
        write_event(conn, tenant, run_id, "step_skipped", {"step_no": step_no})
        return 200, {"skipped": True, "text": existing["text"], "usage": existing["usage"]}
    estimate = estimate_usd(model, int(payload.get("tokens_in_est") or 1000), int(payload.get("tokens_out_est") or 1000))
    spent = float(payload.get("spent_usd") or 0)
    max_usd = float(payload.get("max_usd") or 1)
    decision = decide(
        Call(
            method="POST",
            host=PROVIDER_HOST,
            port=443,
            path="/complete",
            tenant_id=tenant,
            source_ip=source_ip,
            token_ip=source_ip,
            allow_hosts=frozenset({PROVIDER_HOST}),
            spent_usd=spent,
            estimate_usd=estimate,
            max_usd=max_usd,
        )
    )
    if not decision.allow:
        write_denial(conn, tenant, decision.reason, {"model": model})
        write_event(conn, tenant, run_id, "budget_denied", {"estimate": estimate})
        return 402, {"error": decision.reason, "estimate": estimate}
    if existing and existing["status"] == "STARTED":
        _mark_uncertain(conn, tenant, run_id, step_no, digest)
    else:
        begin_step(conn, tenant, run_id, step_no, digest, model, estimate)
    try:
        completion = _provider(ctx).complete(messages, tools, model)
    except ReplayMiss as exc:
        finish_step(conn, tenant, run_id, step_no, digest, "FAILED", {"error": "replay_miss"})
        return 409, {"error": "replay_miss", "digest": exc.digest}
    body = {"text": completion.text, "tokens_in": completion.tokens_in, "tokens_out": completion.tokens_out}
    finish_step(conn, tenant, run_id, step_no, digest, "COMPLETED", body)
    _set_cost(conn, tenant, run_id, step_no, digest, model, completion)
    write_event(conn, tenant, run_id, "step_completed", {"step_no": step_no, "model": model})
    return 200, {"skipped": False, "text": completion.text, "usage": body}


def tool(ctx: ProxyContext, conn, payload: dict, source_ip: str) -> tuple[int, dict]:
    bound = _bound(conn, payload, source_ip)
    if bound is None:
        return 403, {"error": "token"}
    tenant, run_id = bound
    name = payload["name"]
    args = payload.get("args") or {}
    step_no = int(payload["step_no"])
    digest = step_digest([{"tool": name, "args": args}], [])
    existing = _step(conn, tenant, run_id, step_no, digest)
    if existing and existing["status"] == "COMPLETED":
        write_event(conn, tenant, run_id, "step_skipped", {"step_no": step_no, "tool": name})
        return 200, {"skipped": True, **existing["response"]}
    begin_step(conn, tenant, run_id, step_no, digest, "tool", 0)
    denied = _guard(name, args, tenant)
    if denied:
        write_denial(conn, tenant, denied, {"tool": name})
        write_event(conn, tenant, run_id, "policy_denied", {"tool": name, "reason": denied})
        result = {"denied": True, "reason": denied}
        finish_step(conn, tenant, run_id, step_no, digest, "COMPLETED", result)
        return 200, {"skipped": False, **result}
    result = _run_tool(conn, tenant, name, args)
    finish_step(conn, tenant, run_id, step_no, digest, "COMPLETED", result)
    write_event(conn, tenant, run_id, "step_completed", {"step_no": step_no, "tool": name})
    return 200, {"skipped": False, **result}


def set_state(conn, payload: dict, source_ip: str) -> tuple[int, dict]:
    from runtime.scheduler import transition

    bound = _bound(conn, payload, source_ip)
    if bound is None:
        return 403, {"error": "token"}
    tenant, run_id = bound
    row = conn.execute("SELECT state FROM runs WHERE id = %s", (run_id,)).fetchone()
    if row is None:
        return 404, {"error": "run"}
    nxt = transition(row[0], payload["state"])
    conn.execute(
        "UPDATE runs SET state = %s, artifact = %s WHERE id = %s",
        (nxt, payload.get("artifact"), run_id),
    )
    write_event(conn, tenant, run_id, "state", {"state": nxt})
    return 200, {"state": nxt}


def add_event(conn, payload: dict, source_ip: str) -> tuple[int, dict]:
    bound = _bound(conn, payload, source_ip)
    if bound is None:
        return 403, {"error": "token"}
    tenant, run_id = bound
    write_event(conn, tenant, run_id, payload["event"], payload.get("detail") or {})
    return 200, {"ok": True}


def write_event(conn, tenant: str, run_id: str, event: str, detail: dict) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute(
        """
        INSERT INTO run_events (tenant_id, run_id, event, detail)
        VALUES (%s, %s, %s, %s::jsonb)
        """,
        (tenant, run_id, event, json.dumps(detail)),
    )


def _bound(conn, payload: dict, source_ip: str):
    conn.execute("SELECT set_config('app.tenant_id', '', false)")
    found = resolve(conn, payload.get("token") or "", source_ip)
    if found is None:
        return None
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (found["tenant_id"],))
    return found["tenant_id"], found["run_id"]


def _provider(ctx: ProxyContext) -> Provider:
    if ctx.mode == "replay":
        return Provider(ctx.cassette, mode="replay")
    return Provider(ctx.cassette, mode="record", upstream=ctx.upstream)


def _pop_fault(ctx: ProxyContext, model: str) -> str | None:
    if model != "primary" or not ctx.faults:
        return None
    return str(ctx.faults.pop(0))


def _step(conn, tenant: str, run_id: str, step_no: int, digest: str) -> dict | None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    row = conn.execute(
        """
        SELECT status, response
        FROM steps
        WHERE run_id = %s AND step_no = %s AND arg_hash = %s
        """,
        (run_id, step_no, digest),
    ).fetchone()
    if row is None:
        return None
    response = row[1] or {}
    if isinstance(response, str):
        response = json.loads(response)
    text = ""
    if isinstance(response, dict):
        text = response.get("text") or ""
    return {"status": row[0], "response": response, "text": text, "usage": response}


def _mark_uncertain(conn, tenant: str, run_id: str, step_no: int, digest: str) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute(
        """
        UPDATE steps SET cost_uncertain = true
        WHERE run_id = %s AND step_no = %s AND arg_hash = %s
        """,
        (run_id, step_no, digest),
    )


def _set_cost(conn, tenant: str, run_id: str, step_no: int, digest: str, model: str, completion: Completion) -> None:
    cost = estimate_usd(model, completion.tokens_in, completion.tokens_out)
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute(
        """
        UPDATE steps
        SET tokens_in = %s, tokens_out = %s, cost_usd = %s
        WHERE run_id = %s AND step_no = %s AND arg_hash = %s
        """,
        (completion.tokens_in, completion.tokens_out, cost, run_id, step_no, digest),
    )


def _guard(name: str, args: dict, tenant: str) -> str | None:
    if name != "py.exec":
        return None
    code = str(args.get("code") or "")
    if "/etc" in code or "shadow" in code:
        return "sandbox"
    if "example.com" in code or "socket" in code or "http://" in code or "https://" in code:
        return "sandbox"
    if "/tenants/" in code or "t-other" in code:
        return "policy"
    for other in ("t-a", "t-b", "t-c", "t-d"):
        if other != tenant and other in code:
            return "policy"
    return None


def _run_tool(conn, tenant: str, name: str, args: dict) -> dict:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    if name == "mail.list":
        rows = conn.execute(
            """
            SELECT id, from_addr, to_addr, subject, body, attachment_id
            FROM mail WHERE tenant_id = %s ORDER BY id
            """,
            (tenant,),
        ).fetchall()
        domains = conn.execute("SELECT domains FROM tenants WHERE tenant_id = %s", (tenant,)).fetchone()
        return {
            "messages": [_mail(row, tenant) for row in rows],
            "domains": list(domains[0]) if domains else [],
        }
    if name == "mail.read":
        rows = conn.execute(
            """
            SELECT id, from_addr, to_addr, subject, body, attachment_id
            FROM mail WHERE tenant_id = %s AND id = ANY(%s) ORDER BY id
            """,
            (tenant, list(args.get("ids") or [])),
        ).fetchall()
        return {"messages": [_mail(row, tenant) for row in rows]}
    if name == "docs.read":
        row = conn.execute(
            "SELECT id, name, body FROM documents WHERE tenant_id = %s AND id = %s",
            (tenant, args.get("id")),
        ).fetchone()
        if row is None:
            return {"document": None}
        return {"document": {"id": row[0], "name": row[1], "body": row[2]}}
    if name == "py.exec":
        timeout = float(args.get("timeout") or 5)
        try:
            stdout = py_exec(str(args.get("code") or ""), timeout)
        except Exception as exc:
            return {"error": str(exc), "stdout": ""}
        return {"stdout": stdout}
    if name == "draft.create":
        from outbox.send import create_draft

        draft = create_draft(
            conn,
            tenant,
            args["draft_id"],
            args["to"],
            args.get("subject") or "brief",
            args.get("body") or "",
            tuple(args.get("domains") or ()),
            tuple(args.get("attachments") or ()),
        )
        return {"draft": draft}
    if name == "memory.write":
        from agent.tools import memory_write

        memory_id = memory_write(
            conn,
            tenant,
            args["agent_id"],
            args.get("kind") or "cursor",
            args.get("body") or "",
            tuple(args.get("source_ids") or ()),
            args["run_id"],
        )
        return {"memory_id": memory_id}
    return {"error": "unknown_tool"}


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
