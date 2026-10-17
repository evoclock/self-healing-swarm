"""Address first, token bound to it, then create and start. Needs the database (make test)."""
import os
from decimal import Decimal

import psycopg

import runtime.docker_api as docker_api
import runtime.launch as launch
from runtime.policy import RunPolicy

URL = os.environ.get("DATABASE_URL", "")
POLICY = RunPolicy(Decimal("1.00"), frozenset({"mail.list"}))


def _conn():
    conn = psycopg.connect(URL, autocommit=True)
    conn.execute("SELECT set_config('app.tenant_id', 't-d', false)")
    conn.execute("DELETE FROM ip_leases WHERE tenant_id = 't-d'")
    return conn


def test_addresses_are_taken_lowest_first_inside_the_tenant_subnet_and_released():
    conn = _conn()
    first = launch.allocate_ip(conn, "t-d", "launch-1")
    second = launch.allocate_ip(conn, "t-d", "launch-2")
    assert (first, second) == ("10.231.4.16", "10.231.4.17")
    launch.release_ip(conn, "t-d", first)
    assert launch.allocate_ip(conn, "t-d", "launch-3") == first
    conn.execute("DELETE FROM ip_leases WHERE tenant_id = 't-d'")


def test_the_token_is_bound_to_the_address_before_the_container_exists(monkeypatch):
    conn = _conn()
    seen = []

    def create(name, host_repo, network, env, ip=None, tenant=None, **kwargs):
        bound = conn.execute(
            "SELECT host(container_ip) FROM run_tokens WHERE run_id = 'launch-bind'"
        ).fetchone()
        seen.append(("create", network, ip, bound[0] if bound else None, tenant))
        return "cid-launch"

    monkeypatch.setenv("HOST_REPO", "/repo")
    monkeypatch.setattr(launch, "create_agent_container", create)
    monkeypatch.setattr(launch, "start_container", lambda cid: seen.append(("start", cid)))
    monkeypatch.setattr(launch, "remove_container", lambda cid: seen.append(("remove", cid)))
    cid, ip = launch.start_agent(conn, "t-d", "launch-bind", "tok-launch", "agents/t-d/daily_brief.yaml", [], "swarm-launch",
                                 fresh=True, agent_id="daily-brief", policy=POLICY)
    assert seen == [("create", "swarm-agents-t-d", ip, ip, "t-d"), ("start", "cid-launch")]
    assert launch.leased_ip(conn, "t-d", cid) == ip
    launch.stop_agent(conn, "t-d", cid)
    assert seen[-1] == ("remove", "cid-launch") and launch.leased_ip(conn, "t-d", cid) is None


def test_a_failed_create_gives_the_address_back(monkeypatch):
    conn = _conn()

    def broken(*args, **kwargs):
        raise RuntimeError("docker create 500")

    monkeypatch.setenv("HOST_REPO", "/repo")
    monkeypatch.setattr(launch, "create_agent_container", broken)
    try:
        launch.start_agent(conn, "t-d", "launch-fail", "tok-launch-fail", "agents/t-d/daily_brief.yaml", [], "x",
                           fresh=True, agent_id="daily-brief", policy=POLICY)
    except RuntimeError:
        pass
    assert conn.execute("SELECT count(*) FROM ip_leases WHERE tenant_id = 't-d'").fetchone()[0] == 0


def test_container_gets_its_address_and_only_its_tenant_config(monkeypatch):
    calls = []

    def fake(method, path, body=None, socket_path=None):
        calls.append((method, path, body))
        return 201, {"Id": "x"}

    monkeypatch.setattr(docker_api, "docker_request", fake)
    docker_api.create_agent_container("n", "/repo", "swarm-agents-t-a", [], ip="10.231.1.16", tenant="t-a")
    body = calls[-1][2]
    assert body["HostConfig"]["Binds"] == ["/repo/agents/t-a:/src/agents/t-a:ro"]
    assert body["HostConfig"]["NetworkMode"] == "swarm-agents-t-a"
    assert body["NetworkingConfig"] == {"EndpointsConfig": {"swarm-agents-t-a": {"IPAMConfig": {"IPv4Address": "10.231.1.16"}}}}


def test_reap_frees_exited_dead_missing_and_stale_launches_but_not_created_or_running():
    conn = _conn()
    states = {"c-exited": "exited", "c-dead": "dead", "c-created": "created", "c-running": "running"}
    for n, cid in enumerate(list(states) + ["c-missing"], start=16):
        conn.execute("INSERT INTO ip_leases (ip, tenant_id, run_id, container_id) VALUES (%s, 't-d', 'reap', %s)", (f"10.231.4.{n}", cid))
    conn.execute("INSERT INTO ip_leases (ip, tenant_id, run_id, allocated_at) VALUES ('10.231.4.30', 't-d', 'reap', now() - interval '3 minutes')")
    conn.execute("INSERT INTO ip_leases (ip, tenant_id, run_id) VALUES ('10.231.4.31', 't-d', 'reap')")

    def inspect(cid):
        if cid not in states:
            raise RuntimeError("docker inspect 404")
        return {"State": {"Status": states[cid]}}

    removed = []
    launch.remove_container = lambda cid: removed.append(cid)
    try:
        freed = launch.reap_exited(conn, inspect=inspect)
    finally:
        launch.remove_container = docker_api.remove_container
    conn.execute("SELECT set_config('app.tenant_id', 't-d', false)")
    left = sorted(str(row[0]) for row in conn.execute("SELECT ip FROM ip_leases WHERE tenant_id = 't-d'").fetchall())
    assert sorted(removed) == ["c-dead", "c-exited", "c-missing"]
    assert left == ["10.231.4.18", "10.231.4.19", "10.231.4.31"]
    assert "10.231.4.30" in freed
    conn.execute("DELETE FROM ip_leases WHERE tenant_id = 't-d'")
