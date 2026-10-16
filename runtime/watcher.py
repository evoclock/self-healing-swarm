"""Lease watcher. A run whose lease ran out (no proxy request within the lease) is resumed or escalated."""
import os
import secrets
import sys
import time

from runtime.docker_api import container_ip, create_agent_container, inspect_container, start_container
from runtime.runs import bind_token, connect
from runtime.scheduler import on_lease_expired, transition

LEASE_SECONDS = int(os.environ.get("PROXY_LEASE_S", "15"))


def watch_once(conn, resume, only: set[str] | None = None) -> list[tuple[str, str, str]]:
    """One pass. resume(tenant, run_id) starts the replacement worker. only limits the pass to those run ids."""
    conn.execute("SELECT set_config('app.tenant_id', '', false)")
    expired = conn.execute("SELECT tenant_id, run_id, resume_count FROM expired_leases()").fetchall()
    outcomes = []
    for tenant, run_id, resume_count in expired:
        if only is not None and run_id not in only:
            continue
        state, count = on_lease_expired(resume_count)
        if state != "RUNNING":
            transition("RUNNING", state)
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        conn.execute(
            """
            UPDATE runs SET state = %s, resume_count = %s, lease_until = now() + make_interval(secs => %s)
            WHERE id = %s AND state = 'RUNNING'
            """,
            (state, count, LEASE_SECONDS, run_id),
        )
        conn.execute(
            "INSERT INTO run_events (tenant_id, run_id, event, detail) VALUES (%s, %s, %s, '{}'::jsonb)",
            (tenant, run_id, "lease_resumed" if state == "RUNNING" else "lease_escalated"),
        )
        if state == "RUNNING":
            resume(tenant, run_id)
        outcomes.append((tenant, run_id, state))
    return outcomes


def resume_in_container(conn, tenant: str, run_id: str) -> str:
    """Start a fresh locked container for the run and bind a new token generation to its IP."""
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    agent_id, resumes = conn.execute("SELECT agent_id, resume_count FROM runs WHERE id = %s", (run_id,)).fetchone()
    network = os.environ["AGENT_NETWORK"]
    token = secrets.token_urlsafe(24)
    env = [
        "PYTHONPATH=/src",
        "PYTHONDONTWRITEBYTECODE=1",
        "HOME=/tmp",
        f"PROXY_URL={os.environ['PROXY_A_URL']}",
        f"RUN_TOKEN={token}",
        f"RUN_ID={run_id}",
        f"AGENT_CONFIG=/src/agents/{tenant}/{agent_id.replace('-', '_')}.yaml",
    ]
    cid = create_agent_container(f"swarm-{run_id}-resume-{resumes}", os.environ["HOST_REPO"], network, env)
    start_container(cid)
    bind_token(conn, tenant, run_id, token, container_ip(inspect_container(cid), network))
    return cid


def main() -> int:
    conn = connect(os.environ["DATABASE_URL"])
    while True:
        for tenant, run_id, state in watch_once(conn, lambda tenant, run_id: resume_in_container(conn, tenant, run_id)):
            print(f"watcher {tenant}.{run_id} {state}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
