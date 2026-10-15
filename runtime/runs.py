import psycopg

from runtime.scheduler import transition


def connect(database_url: str):
    return psycopg.connect(database_url, autocommit=True)


def reset_run(conn, tenant: str, run_id: str) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute("DELETE FROM steps WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM run_events WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM run_tokens WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM memory WHERE run_id = %s", (run_id,))
    conn.execute("DELETE FROM outbox WHERE draft_id LIKE %s", (run_id + ":%",))
    conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))


def open_run(conn, tenant: str, run_id: str, agent_id: str, token: str, source_ip: str) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    reset_run(conn, tenant, run_id)
    conn.execute(
        """
        INSERT INTO runs (id, tenant_id, agent_id, state)
        VALUES (%s, %s, %s, 'QUEUED')
        """,
        (run_id, tenant, agent_id),
    )
    transition("QUEUED", "RUNNING")
    conn.execute("UPDATE runs SET state = 'RUNNING', resume_count = 0 WHERE id = %s", (run_id,))
    conn.execute(
        """
        INSERT INTO run_tokens (token, tenant_id, run_id, container_ip)
        VALUES (%s, %s, %s, %s)
        """,
        (token, tenant, run_id, source_ip),
    )


def bind_token(conn, tenant: str, run_id: str, token: str, source_ip: str) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute(
        """
        INSERT INTO run_tokens (token, tenant_id, run_id, container_ip)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (token) DO UPDATE SET container_ip = EXCLUDED.container_ip, run_id = EXCLUDED.run_id
        """,
        (token, tenant, run_id, source_ip),
    )


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
