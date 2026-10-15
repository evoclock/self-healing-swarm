import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import psycopg

TENANT_PATH = re.compile(r"/tenants/([^/]+)")


@dataclass(frozen=True)
class Call:
    method: str
    host: str
    port: int
    path: str
    tenant_id: str
    source_ip: str
    token_ip: str
    allow_hosts: frozenset[str]
    spent_usd: Decimal
    estimate_usd: Decimal
    max_usd: Decimal


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str


@dataclass(frozen=True)
class Claim:
    """The (generation, started_at) pair that won a step. finish_step writes only under this pair."""

    generation: int
    started_at: datetime


def over_budget(spent, estimate, cap) -> bool:
    return Decimal(str(spent)) + Decimal(str(estimate)) > Decimal(str(cap))


def decide(call: Call) -> Decision:
    if call.method.upper() == "CONNECT":
        return Decision(False, "connect")
    if call.host == "127.0.0.1" and call.port == 18894:
        return Decision(False, "loopback")
    if call.source_ip != call.token_ip:
        return Decision(False, "ip_mismatch")
    found = TENANT_PATH.search(call.path)
    if found and found.group(1) != call.tenant_id:
        return Decision(False, "cross_tenant")
    if call.host not in call.allow_hosts:
        return Decision(False, "host")
    if over_budget(call.spent_usd, call.estimate_usd, call.max_usd):
        return Decision(False, "budget")
    return Decision(True, "allow")


def resolve(conn, token: str, source_ip: str):
    row = conn.execute(
        "SELECT tenant_id, run_id, generation FROM resolve_token(%s, %s)",
        (token, source_ip),
    ).fetchone()
    if row is None:
        return None
    return {"tenant_id": row[0], "run_id": row[1], "generation": row[2]}


def begin_step(conn, tenant: str, run_id: str, step_no: int, arg_hash: str, model: str, estimate, generation: int = 0) -> Claim | None:
    """Insert STARTED. Returns the claim when this caller won the row, None when the row already existed."""
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    row = conn.execute(
        """
        INSERT INTO steps (
          tenant_id, run_id, step_no, arg_hash, status, provider, model, cost_usd, generation, started_at
        ) VALUES (%s, %s, %s, %s, 'STARTED', 'proxy', %s, %s, %s, clock_timestamp())
        ON CONFLICT (run_id, step_no, arg_hash) DO NOTHING
        RETURNING generation, started_at
        """,
        (tenant, run_id, step_no, arg_hash, model, estimate, generation),
    ).fetchone()
    return Claim(row[0], row[1]) if row else None


def finish_step(
    conn,
    tenant: str,
    run_id: str,
    step_no: int,
    arg_hash: str,
    status: str,
    response: dict,
    claim: Claim,
    usage: tuple[int, int, Decimal] | None = None,
) -> bool:
    """Close a STARTED row. A caller whose claim was taken over writes nothing and gets False."""
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    tokens_in, tokens_out, cost = usage if usage else (None, None, None)
    row = conn.execute(
        """
        UPDATE steps
        SET status = %s, response = %s::jsonb,
            tokens_in = COALESCE(%s, tokens_in),
            tokens_out = COALESCE(%s, tokens_out),
            cost_usd = COALESCE(%s, cost_usd)
        WHERE run_id = %s AND step_no = %s AND arg_hash = %s
          AND status = 'STARTED' AND generation = %s AND started_at = %s
        RETURNING 1
        """,
        (
            status,
            json.dumps(response),
            tokens_in,
            tokens_out,
            cost,
            run_id,
            step_no,
            arg_hash,
            claim.generation,
            claim.started_at,
        ),
    ).fetchone()
    return row is not None


def write_denial(conn, tenant: str, reason: str, detail: dict) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute(
        """
        INSERT INTO audit_log (tenant_id, event, detail)
        VALUES (%s, 'policy_denied', %s::jsonb)
        """,
        (tenant, json.dumps({"reason": reason, **detail})),
    )
