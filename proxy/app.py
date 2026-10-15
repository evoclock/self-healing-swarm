import json
import re
from dataclasses import dataclass

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
    spent_usd: float
    estimate_usd: float
    max_usd: float


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str


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
    if call.spent_usd + call.estimate_usd > call.max_usd:
        return Decision(False, "budget")
    return Decision(True, "allow")


def resolve(conn, token: str, source_ip: str):
    row = conn.execute(
        "SELECT tenant_id, run_id FROM resolve_token(%s, %s)",
        (token, source_ip),
    ).fetchone()
    if row is None:
        return None
    return {"tenant_id": row[0], "run_id": row[1]}


def begin_step(conn, tenant: str, run_id: str, step_no: int, arg_hash: str, model: str, estimate: float) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute(
        """
        INSERT INTO steps (
          tenant_id, run_id, step_no, arg_hash, status, provider, model, cost_usd
        ) VALUES (%s, %s, %s, %s, 'STARTED', 'proxy', %s, %s)
        ON CONFLICT (run_id, step_no, arg_hash) DO NOTHING
        """,
        (tenant, run_id, step_no, arg_hash, model, estimate),
    )


def finish_step(conn, tenant: str, run_id: str, step_no: int, arg_hash: str, status: str, response: dict) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute(
        """
        UPDATE steps
        SET status = %s, response = %s::jsonb
        WHERE run_id = %s AND step_no = %s AND arg_hash = %s
        """,
        (status, json.dumps(response), run_id, step_no, arg_hash),
    )


def write_denial(conn, tenant: str, reason: str, detail: dict) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute(
        """
        INSERT INTO audit_log (tenant_id, event, detail)
        VALUES (%s, 'policy_denied', %s::jsonb)
        """,
        (tenant, json.dumps({"reason": reason, **detail})),
    )
