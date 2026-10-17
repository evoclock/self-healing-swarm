"""Dispatcher. Each pass asks schedule() which runs to start now, starts them, escalates predicted deadline
misses, and resumes runs whose lease lapsed. `python -m runtime.dispatch` loops over this; the drill calls
dispatch_once and watch_once directly, so it exercises the same code.
"""
import math
import os
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

from runtime.config import load_agent
from runtime.docker_api import container_ip, create_agent_container, inspect_container, start_container
from runtime.policy import RunPolicy
from runtime.runs import bind_token, connect, open_run, record_container
from runtime.scheduler import Job, schedule, transition
from runtime.watcher import resume_in_container, watch_once


def dispatch_once(jobs, tenant_caps, launch, escalate, host_cap=8, window=90, provider_caps=None, provider_tpm=None):
    """Start the jobs schedule() places at minute 0; escalate the ones it predicts will miss their deadline.

    provider_tpm splits each provider's tokens per minute across tenants (a token bucket per tenant).
    Jobs placed later are left for a later pass. Returns the placements.
    """
    by_id = {job.job_id: job for job in jobs}
    placements = schedule(tuple(jobs), tenant_caps, host_cap, window, provider_caps, provider_tpm)
    for placement in placements:
        if placement.escalated:
            escalate(by_id[placement.job_id])
        elif placement.start == 0:
            launch(by_id[placement.job_id])
    return placements


def launch_run(conn, tenant, run_id, agent_id, config_path, scenario, policy) -> str:
    """Open the run, start a fresh locked container, record it, bind the token to its IP."""
    network = os.environ["AGENT_NETWORK"]
    token = secrets.token_urlsafe(24)
    open_run(conn, tenant, run_id, agent_id, token, "10.255.255.1", policy, config_path, scenario)
    env = [
        "PYTHONPATH=/src",
        "PYTHONDONTWRITEBYTECODE=1",
        "HOME=/tmp",
        f"PROXY_URL={os.environ['PROXY_A_URL']}",
        f"RUN_TOKEN={token}",
        f"RUN_ID={run_id}",
        f"AGENT_CONFIG=/src/{config_path}",
    ]
    if scenario:
        env.append(f"SCENARIO={scenario}")
    cid = create_agent_container(f"swarm-{run_id}", os.environ["HOST_REPO"], network, env)
    start_container(cid)
    record_container(conn, tenant, run_id, cid)
    bind_token(conn, tenant, run_id, token, container_ip(inspect_container(cid), network))
    return cid


def escalate_run(conn, tenant, run_id, agent_id, config_path, policy) -> None:
    """A predicted deadline miss becomes an ESCALATED run row with a deadline_escalated event."""
    open_run(conn, tenant, run_id, agent_id, secrets.token_urlsafe(24), "10.255.255.1", policy, config_path, None)
    transition("RUNNING", "ESCALATED")
    conn.execute("UPDATE runs SET state = 'ESCALATED', artifact = 'predicted deadline miss' WHERE id = %s", (run_id,))
    conn.execute(
        "INSERT INTO run_events (tenant_id, run_id, event, detail) VALUES (%s, %s, 'deadline_escalated', '{}'::jsonb)",
        (tenant, run_id),
    )


def jobs_for_today(root: Path, now: datetime):
    """One job per agent config per day: deadline in minutes from now, duration from wall_s."""
    jobs = {}
    for path in sorted(root.glob("*/*.yaml")):
        config = load_agent(path)
        hour, minute = (int(part) for part in config.deadline.split(":"))
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        run_id = f"{config.id}-{config.tenant_id}-{now:%Y%m%d}"
        job = Job(
            run_id,
            config.tenant_id,
            max(1, math.ceil(config.limits.wall_s / 60)),
            int((due - now).total_seconds() // 60),
            tokens=config.limits.max_tokens,
        )
        jobs[run_id] = (job, config, str(path))
    return jobs


def main() -> int:
    conn = connect(os.environ["DATABASE_URL"])
    caps = {tenant: int(os.environ.get("TENANT_CAP", "2")) for tenant in ("t-a", "t-b", "t-c", "t-d")}
    tpm = {"primary": int(os.environ["PROVIDER_TPM"])} if os.environ.get("PROVIDER_TPM") else None
    while True:
        now = datetime.now()
        planned = jobs_for_today(Path("agents"), now)
        conn.execute("SELECT set_config('app.tenant_id', '', false)")
        waiting = []
        for run_id, (job, config, path) in planned.items():
            conn.execute("SELECT set_config('app.tenant_id', %s, false)", (job.tenant_id,))
            if conn.execute("SELECT 1 FROM runs WHERE id = %s", (run_id,)).fetchone() is None and job.deadline > 0:
                waiting.append(job)

        def launch(job):
            _job, config, path = planned[job.job_id]
            launch_run(conn, job.tenant_id, job.job_id, config.id, path, None, RunPolicy.from_config(config))

        def escalate(job):
            _job, config, path = planned[job.job_id]
            escalate_run(conn, job.tenant_id, job.job_id, config.id, path, RunPolicy.from_config(config))

        dispatch_once(waiting, caps, launch, escalate, provider_tpm=tpm)
        for tenant, run_id, state in watch_once(conn, lambda tenant, run_id: resume_in_container(conn, tenant, run_id)):
            print(f"watcher {tenant}.{run_id} {state}", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
