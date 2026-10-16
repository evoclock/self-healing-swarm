import json
import os
from dataclasses import dataclass, field
from decimal import Decimal

import psycopg

from proxy.app import finish_step, resolve
from proxy.gate import Bound, Finished, admit_tool, claim_step, finish_state, load_bound, write_event
from proxy.providers import Cassette, KNOWN_MODELS, Provider, ReplayMiss, live_estimate, merge_trace_headers, recorded_cost, step_digest


@dataclass
class ProxyContext:
    database_url: str
    cassette: Cassette
    mode: str
    name: str
    upstream: object | None = None
    faults: list = field(default_factory=list)
    parents: dict = field(default_factory=dict)


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
            return set_state(ctx, conn, payload, source_ip)
        if path == "/v1/event":
            return add_event(ctx, conn, payload, source_ip)
    return 404, {"error": "not_found"}


def complete(ctx: ProxyContext, conn, payload: dict, source_ip: str) -> tuple[int, dict]:
    """Budget fields in the payload (spent_usd, max_usd, tokens_*_est) are never read."""
    bound = _bound(conn, payload, source_ip)
    if bound is None:
        _note(ctx, "complete 403 token")
        return 403, {"error": "token"}
    tenant, run_id = bound.tenant_id, bound.run_id
    model = payload.get("model") or "primary"
    fault = _pop_fault(ctx, model)
    if fault:
        write_event(conn, tenant, run_id, "provider_" + fault, {"simulated": True, "model": model})
        status = 429 if fault == "429" else 503
        _note(ctx, f"complete {status} trace {tenant}.{run_id} {model} fault {fault}")
        return status, {"error": fault, "simulated": True, "model": model}
    if model not in KNOWN_MODELS:
        _note(ctx, f"complete 400 trace {tenant}.{run_id} unknown_model {model}")
        return 400, {"error": "unknown_model", "model": model}
    messages = payload.get("messages") or []
    tools = payload.get("tools") or []
    digest = step_digest(messages, tools)
    if model == "local":
        charge = Decimal(0)
    elif ctx.mode == "replay":
        charge = recorded_cost(ctx.cassette, digest) or Decimal(0)
    else:
        # Live: reserve an upper bound before the call. finish_step replaces it with the returned usage.cost.
        charge = live_estimate(messages, tools, model)
    step_no = int(payload["step_no"])
    claimed = claim_step(conn, bound, step_no, digest, model, charge)
    if isinstance(claimed, Finished):
        _note(ctx, _complete_line(claimed.status, tenant, run_id, step_no, model, claimed.body, charge))
        return claimed.status, claimed.body
    trace = None
    if ctx.mode != "replay" and model != "local":
        parent = ctx.parents.get((tenant, run_id))
        trace = merge_trace_headers(tenant, run_id, step_no, model, parent)
    try:
        completion = _provider(ctx).complete(messages, tools, model, project_id=bound.project_id, trace=trace)
    except ReplayMiss as exc:
        finish_step(conn, tenant, run_id, step_no, digest, "FAILED", {"error": "replay_miss"}, claimed.claim)
        _note(ctx, _complete_line(409, tenant, run_id, step_no, model, {"error": "replay_miss"}, charge))
        return 409, {"error": "replay_miss", "digest": exc.digest}
    if completion.request_id:
        ctx.parents[(tenant, run_id)] = completion.request_id
    body = {"text": completion.text, "tokens_in": completion.tokens_in, "tokens_out": completion.tokens_out}
    cost = Decimal(0) if model == "local" else completion.cost_usd
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
    shown = "unset" if cost is None else cost
    _note(ctx, f"complete 200 trace {tenant}.{run_id} step {step_no} {model} cost {shown}")
    return 200, {"skipped": False, **body}


def tool(ctx: ProxyContext, conn, payload: dict, source_ip: str) -> tuple[int, dict]:
    bound = _bound(conn, payload, source_ip)
    if bound is None:
        _note(ctx, "tool 403 token")
        return 403, {"error": "token"}
    name = str(payload.get("name") or "")
    args = payload.get("args") or {}
    step_no = int(payload["step_no"])
    digest = step_digest([{"tool": name, "args": args}], [])
    finished = admit_tool(conn, bound, name, args, step_no, digest)
    mark = finished.body.get("error") or finished.body.get("reason") or "ok"
    _note(ctx, f"tool {finished.status} trace {bound.tenant_id}.{bound.run_id} step {step_no} {name} {mark}")
    return finished.status, finished.body


def set_state(ctx: ProxyContext, conn, payload: dict, source_ip: str) -> tuple[int, dict]:
    bound = _bound(conn, payload, source_ip)
    if bound is None:
        _note(ctx, "state 403 token")
        return 403, {"error": "token"}
    state = str(payload.get("state") or "")
    finished = finish_state(conn, bound, state, payload.get("artifact") or "")
    _note(ctx, f"state {finished.status} trace {bound.tenant_id}.{bound.run_id} {finished.body.get('state') or state}")
    return finished.status, finished.body


def add_event(ctx: ProxyContext, conn, payload: dict, source_ip: str) -> tuple[int, dict]:
    bound = _bound(conn, payload, source_ip)
    if bound is None:
        _note(ctx, "event 403 token")
        return 403, {"error": "token"}
    write_event(conn, bound.tenant_id, bound.run_id, payload["event"], payload.get("detail") or {})
    _note(ctx, f"event trace {bound.tenant_id}.{bound.run_id} {payload['event']}")
    return 200, {"ok": True}


def _bound(conn, payload: dict, source_ip: str) -> Bound | None:
    conn.execute("SELECT set_config('app.tenant_id', '', false)")
    found = resolve(conn, payload.get("token") or "", source_ip)
    if found is None:
        return None
    bound = load_bound(conn, found)
    if bound is not None:
        renew_lease(conn, bound)
    return bound


LEASE_SECONDS = int(os.environ.get("PROXY_LEASE_S", "15"))


def renew_lease(conn, bound: Bound) -> None:
    """Every authenticated request is a heartbeat. The watcher resumes a run whose lease runs out."""
    conn.execute(
        "UPDATE runs SET lease_until = now() + make_interval(secs => %s) WHERE id = %s AND state = 'RUNNING'",
        (LEASE_SECONDS, bound.run_id),
    )


def _provider(ctx: ProxyContext) -> Provider:
    if ctx.mode == "replay":
        return Provider(ctx.cassette, mode="replay")
    return Provider(ctx.cassette, mode="record", upstream=ctx.upstream)


def _note(ctx: ProxyContext, line: str) -> None:
    print(f"{ctx.name} {line}", flush=True)


def _complete_line(status, tenant, run_id, step_no, model, body, charge) -> str:
    parts = ["complete", str(status), f"trace {tenant}.{run_id}", f"step {step_no}", model]
    if status == 402:
        parts.append(f"budget {body.get('estimate', charge)}")
    elif body.get("in_progress"):
        parts.append("in_progress")
    elif body.get("skipped"):
        parts.append("skipped")
    elif body.get("error"):
        parts.append(str(body["error"]))
    return " ".join(parts)


def _pop_fault(ctx: ProxyContext, model: str) -> str | None:
    if model != "primary" or not ctx.faults:
        return None
    return str(ctx.faults.pop(0))
