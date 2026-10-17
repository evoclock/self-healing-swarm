"""Lease watcher. A run whose lease ran out (no proxy heartbeat within the lease) is resumed or escalated.

`python -m runtime.watcher` runs watch_once in a loop. The drill's kill scenario calls the same watch_once and
resume_in_container, so it exercises this code rather than a copy.
"""
import os
import secrets
import sys
import time

from runtime.docker_api import kill_container
from runtime.launch import agent_env, start_agent, stop_agent
from runtime.runs import LEASE_SECONDS, connect
from runtime.scheduler import on_lease_expired, transition


def claim(conn, tenant: str, run_id: str) -> str | None:
    """Claim one expired run in a single transaction. Returns the new state, or None if another watcher has it."""
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        row = conn.execute(
            """
            SELECT resume_count FROM runs
            WHERE id = %s AND state = 'RUNNING' AND lease_until < now()
            FOR UPDATE SKIP LOCKED
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        state, count = on_lease_expired(row[0])
        if state != "RUNNING":
            transition("RUNNING", state)
        updated = conn.execute(
            """
            UPDATE runs SET state = %s, resume_count = %s, lease_until = now() + make_interval(secs => %s)
            WHERE id = %s AND state = 'RUNNING' AND lease_until < now()
            RETURNING resume_count
            """,
            (state, count, LEASE_SECONDS, run_id),
        ).fetchone()
        if updated is None:
            return None
        conn.execute(
            "INSERT INTO run_events (tenant_id, run_id, event, detail) VALUES (%s, %s, %s, '{}'::jsonb)",
            (tenant, run_id, "lease_resumed" if state == "RUNNING" else "lease_escalated"),
        )
    return state


def watch_once(conn, resume, only: set[str] | None = None) -> list[tuple[str, str, str]]:
    """One pass. resume(tenant, run_id) starts the replacement worker, only for runs this pass claimed."""
    conn.execute("SELECT set_config('app.tenant_id', '', false)")
    expired = conn.execute("SELECT tenant_id, run_id FROM expired_leases()").fetchall()
    outcomes = []
    for tenant, run_id in expired:
        if only is not None and run_id not in only:
            continue
        state = claim(conn, tenant, run_id)
        if state is None:
            continue
        if state == "RUNNING":
            resume(tenant, run_id)
        outcomes.append((tenant, run_id, state))
    return outcomes


def resume_in_container(conn, tenant: str, run_id: str) -> str:
    """Kill the previous container and free its address, then bind a new token generation to a fresh address and
    start a fresh locked container there. The token is bound before the container starts."""
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    config_path, scenario, previous, resumes = conn.execute(
        "SELECT config_path, scenario, container_id, resume_count FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    if previous:
        kill_container(previous)
        stop_agent(conn, tenant, previous)
    token = secrets.token_urlsafe(24)
    cid, _ip = start_agent(conn, tenant, run_id, token, config_path, agent_env(token, run_id, config_path, scenario),
                           f"swarm-{run_id}-resume-{resumes}", fresh=False)
    return cid


def main() -> int:
    conn = connect(os.environ["DATABASE_URL"])
    while True:
        for tenant, run_id, state in watch_once(conn, lambda tenant, run_id: resume_in_container(conn, tenant, run_id)):
            print(f"watcher {tenant}.{run_id} {state}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
