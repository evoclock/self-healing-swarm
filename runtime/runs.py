import os
import secrets

import psycopg

from runtime.policy import RunPolicy
from runtime.scheduler import transition


# One lease length for the proxy heartbeat and the watcher.
LEASE_SECONDS = int(os.environ.get("PROXY_LEASE_S", "15"))


def connect(database_url: str):
    return psycopg.connect(database_url, autocommit=True)


def issue_token() -> str:
    return secrets.token_urlsafe(32)


def reset_run(conn, tenant: str, run_id: str) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute("DELETE FROM steps WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM run_events WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM run_tokens WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM memory WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM outbox WHERE draft_id LIKE %s", (run_id + ":%",))
    conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))


def open_run(
    conn,
    tenant: str,
    run_id: str,
    agent_id: str,
    token: str,
    source_ip: str,
    policy: RunPolicy,
    config_path: str | None = None,
    scenario: str | None = None,
) -> None:
    """Fresh run: wipes the journal. Resume must call bind_token, never this."""
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    reset_run(conn, tenant, run_id)
    conn.execute(
        """
        INSERT INTO runs (id, tenant_id, agent_id, state, max_usd, tools, generation, config_path, scenario)
        VALUES (%s, %s, %s, 'QUEUED', %s, %s, 0, %s, %s)
        """,
        (run_id, tenant, agent_id, policy.max_usd, sorted(policy.tools), config_path, scenario),
    )
    transition("QUEUED", "RUNNING")
    conn.execute("UPDATE runs SET state = 'RUNNING', resume_count = 0 WHERE id = %s", (run_id,))
    conn.execute(
        """
        INSERT INTO run_tokens (token, tenant_id, run_id, container_ip, generation)
        VALUES (%s, %s, %s, %s, 0)
        """,
        (token, tenant, run_id, source_ip),
    )


def record_container(conn, tenant: str, run_id: str, container_id: str) -> None:
    """The container now working the run. The watcher kills it before starting a replacement."""
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute("UPDATE runs SET container_id = %s WHERE id = %s", (container_id, run_id))


def bind_token(conn, tenant: str, run_id: str, token: str, source_ip: str) -> int:
    """Bind a token to a container IP under a new generation. Older tokens for the run stop resolving."""
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        row = conn.execute(
            "UPDATE runs SET generation = generation + 1 WHERE id = %s RETURNING generation",
            (run_id,),
        ).fetchone()
        if row is None:
            raise LookupError(run_id)
        generation = row[0]
        conn.execute("DELETE FROM run_tokens WHERE run_id = %s", (run_id,))
        conn.execute(
            """
            INSERT INTO run_tokens (token, tenant_id, run_id, container_ip, generation)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (token, tenant, run_id, source_ip, generation),
        )
    return generation


def run_row(conn, tenant: str, run_id: str) -> dict | None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    row = conn.execute(
        "SELECT state, artifact, resume_count FROM runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return {"state": row[0], "artifact": row[1] or "", "resume_count": row[2]}


def events(conn, tenant: str, run_id: str) -> list[str]:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    rows = conn.execute(
        "SELECT event FROM run_events WHERE run_id = %s ORDER BY id",
        (run_id,),
    ).fetchall()
    return [row[0] for row in rows]


def step_costs(conn, tenant: str, run_id: str) -> list[tuple]:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    return conn.execute(
        """
        SELECT step_no, status, cost_usd
        FROM steps WHERE run_id = %s ORDER BY step_no
        """,
        (run_id,),
    ).fetchall()
