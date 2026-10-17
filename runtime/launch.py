"""Start an agent container with its address chosen first.

Each tenant has its own internal Docker network with a fixed subnet. The launcher takes a free address from that
subnet (a row in ip_leases), binds the run token to it, and only then creates and starts the container with that
address. There is no moment when a container runs with a token bound elsewhere. The address is released when the
container is removed.
"""
import ipaddress
import os

import psycopg

from runtime.docker_api import create_agent_container, inspect_container, remove_container, start_container
from runtime.runs import bind_token, open_run, record_container

TENANT_SUBNETS = {
    "t-a": "10.231.1.0/24",
    "t-b": "10.231.2.0/24",
    "t-c": "10.231.3.0/24",
    "t-d": "10.231.4.0/24",
}
# Docker hands out .1 to .7 (the gateway and the proxies, ip_range /29); runs take .16 to .254.
FIRST_HOST = 16


class NoFreeAddress(Exception):
    pass


def tenant_network(tenant: str) -> str:
    return f"swarm-agents-{tenant}"


def allocate_ip(conn, tenant: str, run_id: str) -> str:
    """The lowest free address in the tenant's subnet. The primary key makes two launchers race safely."""
    if tenant not in TENANT_SUBNETS:
        raise NoFreeAddress(f"no subnet for {tenant}")
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    hosts = list(ipaddress.ip_network(TENANT_SUBNETS[tenant]).hosts())[FIRST_HOST - 1:]
    for _attempt in range(3):
        used = {str(row[0]) for row in conn.execute("SELECT ip FROM ip_leases WHERE tenant_id = %s", (tenant,)).fetchall()}
        for host in hosts:
            if str(host) in used:
                continue
            try:
                conn.execute(
                    "INSERT INTO ip_leases (ip, tenant_id, run_id) VALUES (%s, %s, %s)", (str(host), tenant, run_id)
                )
                return str(host)
            except psycopg.errors.UniqueViolation:
                break  # another launcher took it; reread
    raise NoFreeAddress(tenant)


def release_ip(conn, tenant: str, ip: str) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute("DELETE FROM ip_leases WHERE tenant_id = %s AND ip = %s", (tenant, ip))


def leased_ip(conn, tenant: str, container_id: str) -> str | None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    row = conn.execute("SELECT ip FROM ip_leases WHERE container_id = %s", (container_id,)).fetchone()
    return None if row is None else str(row[0])


def agent_env(token: str, run_id: str, config_path: str, scenario: str | None, extra: list[str] | None = None) -> list[str]:
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
    return env + list(extra or [])


def start_agent(conn, tenant, run_id, token, config_path, env, name, fresh: bool, agent_id=None, policy=None, scenario=None):
    """Allocate, bind, then create and start. fresh=True opens the run (open_run); False rebinds for a resume."""
    ip = allocate_ip(conn, tenant, run_id)
    try:
        if fresh:
            open_run(conn, tenant, run_id, agent_id, token, ip, policy, config_path, scenario)
        else:
            bind_token(conn, tenant, run_id, token, ip)
        cid = create_agent_container(name, os.environ["HOST_REPO"], tenant_network(tenant), env, ip=ip, tenant=tenant)
    except Exception:
        release_ip(conn, tenant, ip)
        raise
    conn.execute("UPDATE ip_leases SET container_id = %s WHERE ip = %s", (cid, ip))
    record_container(conn, tenant, run_id, cid)
    start_container(cid)
    return cid, ip


def stop_agent(conn, tenant: str, container_id: str) -> None:
    """Remove the container, then free its address. Never the other way round."""
    remove_container(container_id)
    ip = leased_ip(conn, tenant, container_id)
    if ip:
        release_ip(conn, tenant, ip)


def reap_exited(conn) -> list[str]:
    """Free addresses whose container has exited or is gone (runs the dispatcher started and nobody removed)."""
    freed = []
    for tenant in TENANT_SUBNETS:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        rows = conn.execute("SELECT ip, container_id FROM ip_leases WHERE container_id IS NOT NULL").fetchall()
        for ip, cid in rows:
            try:
                running = inspect_container(cid)["State"]["Running"]
            except RuntimeError:
                running = False
            if not running:
                stop_agent(conn, tenant, cid)
                freed.append(str(ip))
    return freed
