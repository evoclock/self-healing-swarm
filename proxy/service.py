import json
from dataclasses import dataclass, field

import psycopg

from proxy.app import finish_step, resolve
from proxy.gate import Bound, Finished, admit_tool, claim_step, finish_state, load_bound, write_event
from proxy.providers import Cassette, Provider, ReplayMiss, estimate_usd, server_estimate, step_digest


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
    """Budget fields in the payload (spent_usd, max_usd, tokens_*_est) are never read."""
    bound = _bound(conn, payload, source_ip)
    if bound is None:
        return 403, {"error": "token"}
    tenant, run_id = bound.tenant_id, bound.run_id
    model = payload.get("model") or "primary"
    fault = _pop_fault(ctx, model)
    if fault:
        write_event(conn, tenant, run_id, "provider_" + fault, {"simulated": True, "model": model})
        status = 429 if fault == "429" else 503
        return status, {"error": fault, "simulated": True, "model": model}
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    try:
        estimate = server_estimate(model, messages)
    except KeyError:
        return 400, {"error": "unknown_model", "model": model}
    digest = step_digest(messages, tools)
    step_no = int(payload["step_no"])
    claimed = claim_step(conn, bound, step_no, digest, model, estimate)
    if isinstance(claimed, Finished):
        return claimed.status, claimed.body
    try:
        completion = _provider(ctx).complete(messages, tools, model)
    except ReplayMiss as exc:
        finish_step(conn, tenant, run_id, step_no, digest, "FAILED", {"error": "replay_miss"}, claimed.claim)
        return 409, {"error": "replay_miss", "digest": exc.digest}
    body = {"text": completion.text, "tokens_in": completion.tokens_in, "tokens_out": completion.tokens_out}
    cost = estimate_usd(model, completion.tokens_in, completion.tokens_out)
    won = finish_step(
        conn,
        tenant,
        run_id,
        step_no,
        digest,
        "COMPLETED",
        body,
        claimed.claim,
        (completion.tokens_in, completion.tokens_out, cost),
    )
    if won:
        write_event(conn, tenant, run_id, "step_completed", {"step_no": step_no, "model": model})
    return 200, {"skipped": False, **body}


def tool(ctx: ProxyContext, conn, payload: dict, source_ip: str) -> tuple[int, dict]:
    bound = _bound(conn, payload, source_ip)
    if bound is None:
        return 403, {"error": "token"}
    name = str(payload.get("name") or "")
    args = payload.get("args") or {}
    step_no = int(payload["step_no"])
    digest = step_digest([{"tool": name, "args": args}], [])
    finished = admit_tool(conn, bound, name, args, step_no, digest)
    return finished.status, finished.body


def set_state(conn, payload: dict, source_ip: str) -> tuple[int, dict]:
    bound = _bound(conn, payload, source_ip)
    if bound is None:
        return 403, {"error": "token"}
    finished = finish_state(conn, bound, str(payload.get("state") or ""), payload.get("artifact") or "")
    return finished.status, finished.body


def add_event(conn, payload: dict, source_ip: str) -> tuple[int, dict]:
    bound = _bound(conn, payload, source_ip)
    if bound is None:
        return 403, {"error": "token"}
    write_event(conn, bound.tenant_id, bound.run_id, payload["event"], payload.get("detail") or {})
    return 200, {"ok": True}


def _bound(conn, payload: dict, source_ip: str) -> Bound | None:
    conn.execute("SELECT set_config('app.tenant_id', '', false)")
    found = resolve(conn, payload.get("token") or "", source_ip)
    if found is None:
        return None
    return load_bound(conn, found)


def _provider(ctx: ProxyContext) -> Provider:
    if ctx.mode == "replay":
        return Provider(ctx.cassette, mode="replay")
    return Provider(ctx.cassette, mode="record", upstream=ctx.upstream)


def _pop_fault(ctx: ProxyContext, model: str) -> str | None:
    if model != "primary" or not ctx.faults:
        return None
    return str(ctx.faults.pop(0))
