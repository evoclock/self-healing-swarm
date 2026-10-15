import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from agent.client import ProxyClient
from agent.runner import execute
from evals.gate import config_hash, gate, load_golden, load_recordings
from outbox.send import approve, create_draft, deliver
from outbox.smtp_log import serve_smtp
from runtime.config import load_agent
from runtime.docker_api import (
    container_ip,
    create_agent_container,
    inspect_container,
    kill_container,
    remove_container,
    start_container,
)
from runtime.runs import bind_token, connect, events, open_run, run_row, step_costs
from runtime.scheduler import load_config, on_lease_expired


def main() -> int:
    lines: list[str] = []

    def report(name: str, ok: bool, detail: str) -> bool:
        lines.append(f"{name} {'PASS' if ok else 'FAIL'}")
        lines.append(detail.rstrip())
        print(lines[-2], flush=True)
        print(detail.rstrip(), flush=True)
        return ok

    checks = [
        ("proxies", check_proxies),
        ("kill", scenario_kill),
        ("provider", scenario_provider),
        ("injection", scenario_injection),
        ("escape", scenario_escape),
        ("budget", scenario_budget),
        ("hang", scenario_hang),
        ("loop", scenario_loop),
        ("eval_gate", scenario_eval),
        ("empty", scenario_empty),
        ("send", scenario_send),
    ]
    failed = False
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if not report(name, ok, detail):
            failed = True
    path = Path("docs/drill-output.txt")
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path}", flush=True)
    return 1 if failed else 0


def check_proxies() -> tuple[bool, str]:
    left = _get(os.environ["PROXY_A_URL"] + "/health")
    right = _get(os.environ["PROXY_B_URL"] + "/health")
    ok = left.get("ok") and right.get("ok") and left.get("name") != right.get("name")
    return ok, f"proxy-a {left.get('name')} {left.get('mode')}\nproxy-b {right.get('name')} {right.get('mode')}"


def scenario_kill() -> tuple[bool, str]:
    database_url = os.environ["DATABASE_URL"]
    network = os.environ["AGENT_NETWORK"]
    host_repo = os.environ["HOST_REPO"]
    run_id = "drill-kill"
    token = "drill-kill-token"
    conn = connect(database_url)
    open_run(conn, "t-a", run_id, "daily-brief", token, "10.255.255.1")
    first = _start_agent(host_repo, network, token, run_id, pause=True)
    try:
        ip = container_ip(inspect_container(first), network)
        bind_token(conn, "t-a", run_id, token, ip)
        if not _wait_step(conn, run_id, 2):
            return False, "step 2 did not complete\n" + _logs(first)
        before = step_costs(conn, "t-a", run_id)
        kill_container(first)
        state, count = on_lease_expired(0)
        conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
        conn.execute(
            "UPDATE runs SET state = %s, resume_count = %s WHERE id = %s",
            (state, count, run_id),
        )
        token_2 = "drill-kill-token-2"
        second = _start_agent(host_repo, network, token_2, run_id, pause=False)
        try:
            ip_2 = container_ip(inspect_container(second), network)
            bind_token(conn, "t-a", run_id, token_2, ip_2)
            if not _wait_state(conn, run_id, "SUCCEEDED"):
                return False, "resume did not succeed\n" + _logs(second)
        finally:
            remove_container(second)
    finally:
        remove_container(first)
    after = step_costs(conn, "t-a", run_id)
    same = [row for row in before if row in after or (row[0], row[1]) == (row[0], row[1])]
    costs_held = all(any(later[0] == earlier[0] and later[2] == earlier[2] for later in after) for earlier in before)
    row = run_row(conn, "t-a", run_id)
    ok = row["state"] == "SUCCEEDED" and costs_held and row["resume_count"] == 1
    detail = f"run_id {run_id} state {row['state']} resume {row['resume_count']}\nsteps {after}\nevents {events(conn, 't-a', run_id)}"
    return ok, detail if ok else detail + f"\nbefore {before}"


def scenario_provider() -> tuple[bool, str]:
    row, ev, result = _run("drill-provider", "t-a", "daily-brief", "agents/t-a/daily_brief.yaml", "brief", ["error", "429"])
    ok = row["state"] == "SUCCEEDED" and "provider_fallback" in ev
    return ok, f"run_id drill-provider state {row['state']}\nevents {ev}\n{result}"


def scenario_injection() -> tuple[bool, str]:
    row, ev, result = _run("drill-injection", "t-a", "daily-brief", "agents/t-a/daily_brief.yaml", "brief", [])
    conn = connect(os.environ["DATABASE_URL"])
    conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
    draft = conn.execute(
        "SELECT flagged, state FROM outbox WHERE draft_id = %s",
        ("drill-injection:injection",),
    ).fetchone()
    ok = draft is not None and draft[0] is True and draft[1] == "DRAFT" and row["state"] == "SUCCEEDED"
    return ok, f"run_id drill-injection draft {draft} state {row['state']}\nevents {ev}"


def scenario_escape() -> tuple[bool, str]:
    row, ev, result = _run("drill-escape", "t-b", "doc-analyst", "agents/t-b/doc_analyst.yaml", "escape", [])
    denials = [name for name in ev if name == "policy_denied"]
    ok = row["state"] == "ESCALATED" and len(denials) >= 3
    return ok, f"run_id drill-escape state {row['state']} denials {len(denials)}\nevents {ev}"


def scenario_budget() -> tuple[bool, str]:
    row, ev, result = _run("drill-budget", "t-a", "daily-brief", "agents/t-a/daily_brief.yaml", "budget", [])
    ok = row["state"] == "STOPPED_BUDGET" and "saved progress" in (row["artifact"] or "")
    return ok, f"run_id drill-budget state {row['state']} artifact {row['artifact']}\nevents {ev}"


def scenario_hang() -> tuple[bool, str]:
    row, ev, result = _run("drill-hang", "t-a", "daily-brief", "agents/t-a/daily_brief.yaml", "hang", [])
    ok = "hang_cancel" in ev and row["state"] == "FAILED"
    return ok, f"run_id drill-hang state {row['state']}\nevents {ev}"


def scenario_loop() -> tuple[bool, str]:
    row, ev, result = _run("drill-loop", "t-a", "daily-brief", "agents/t-a/daily_brief.yaml", "loop", [])
    ok = row["state"] == "STOPPED_LOOP" and row["artifact"].strip() != ""
    return ok, f"run_id drill-loop state {row['state']} artifact {row['artifact']}\nevents {ev}"


def scenario_empty() -> tuple[bool, str]:
    row, ev, result = _run("drill-empty", "t-a", "daily-brief", "agents/t-a/daily_brief.yaml", "empty", [])
    ok = row["state"] == "FAILED" and row["artifact"] == ""
    return ok, f"run_id drill-empty state {row['state']} artifact {row['artifact']!r}\nevents {ev}"


def scenario_eval() -> tuple[bool, str]:
    conn = connect(os.environ["DATABASE_URL"])
    golden = Path("evals/golden")
    recordings = Path("recordings/evals")
    cases = load_golden(golden, "daily-brief")
    worse = load_agent(Path("agents/daily_brief.worse.yaml"))
    harmless = load_agent(Path("agents/daily_brief.harmless.yaml"))
    worse_hash = config_hash(worse)
    harmless_hash = config_hash(harmless)
    from evals.store import latest_mean, promoted_hashes

    baseline = latest_mean(conn, "t-a", "daily-brief")
    worse_out = load_recordings(recordings, worse_hash, cases)
    worse_result = gate(cases, worse_out, baseline, worse_hash)
    harmless_out = load_recordings(recordings, harmless_hash, cases)
    harmless_result = gate(cases, harmless_out, baseline, harmless_hash)
    promoted = promoted_hashes(conn, "t-a", "daily-brief")
    refused = False
    try:
        load_config(worse, promoted)
    except Exception as exc:
        refused = "eval_required" in str(exc)
    accepted = load_config(harmless, promoted)
    named = any("brief-01-injection" in item for item in worse_result.failures) and any(
        "brief-02-cross-tenant" in item for item in worse_result.failures
    )
    ok = worse_result.exit_code == 1 and named and refused and harmless_result.exit_code == 0 and accepted.id == "daily-brief"
    detail = (
        f"worse exit {worse_result.exit_code} delta {worse_result.delta}\n"
        + "\n".join(worse_result.failures[:6])
        + f"\nharmless exit {harmless_result.exit_code} mean {harmless_result.mean} refused {refused}"
    )
    return ok, detail


def scenario_send() -> tuple[bool, str]:
    conn = connect(os.environ["DATABASE_URL"])
    conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
    conn.execute("DELETE FROM outbox WHERE draft_id = 'drill-send'")
    create_draft(conn, "t-a", "drill-send", "ada@a.example", "hello", "body", ("a.example",))
    approve(conn, "t-a", "drill-send", "approver-a")
    path = Path(".audit/smtp.log")
    if path.exists():
        path.write_text("")
    server, _thread = serve_smtp(path, "127.0.0.1", 2525)
    sink: list = []
    try:
        deliver(conn, "t-a", "drill-send", sink, ("127.0.0.1", 2525))
        conn.execute("UPDATE outbox SET state = 'SENDING' WHERE draft_id = 'drill-send'")
        deliver(conn, "t-a", "drill-send", sink, ("127.0.0.1", 2525))
    finally:
        server.close()
    logged = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    ok = len(logged) == 1 and len(sink) == 1
    return ok, f"smtp {len(logged)} sink {len(sink)} message {logged[0]['message_id'] if logged else None}"


def _run(run_id, tenant, agent_id, config_path, scenario, faults):
    proxy = os.environ["PROXY_A_URL"]
    token = f"token-{run_id}"
    client = ProxyClient(proxy, token)
    conn = connect(os.environ["DATABASE_URL"])
    open_run(conn, tenant, run_id, agent_id, token, client.whoami())
    client.faults(faults)
    result = execute(client, load_agent(Path(config_path)), run_id, scenario)
    client.faults([])
    return run_row(conn, tenant, run_id), events(conn, tenant, run_id), result


def _start_agent(host_repo: str, network: str, token: str, run_id: str, pause: bool) -> str:
    env = [
        "PYTHONPATH=/src",
        "PYTHONDONTWRITEBYTECODE=1",
        "HOME=/tmp",
        f"PROXY_URL={os.environ['PROXY_A_URL']}",
        f"RUN_TOKEN={token}",
        f"RUN_ID={run_id}",
        "AGENT_CONFIG=/src/agents/t-a/daily_brief.yaml",
        "SCENARIO=brief",
    ]
    if pause:
        env.extend(["DRILL_PAUSE_AFTER_STEP=2", "DRILL_PAUSE_SECONDS=300"])
    name = "swarm-" + token
    container_id = create_agent_container(name, host_repo, network, env)
    start_container(container_id)
    return container_id


def _wait_step(conn, run_id: str, step_no: int) -> bool:
    for _ in range(90):
        rows = step_costs(conn, "t-a", run_id)
        if any(row[0] == step_no and row[1] == "COMPLETED" for row in rows):
            return True
        time.sleep(1)
    return False


def _wait_state(conn, run_id: str, state: str) -> bool:
    for _ in range(120):
        row = run_row(conn, "t-a", run_id)
        if row and row["state"] == state:
            return True
        time.sleep(1)
    return False


def _logs(container_id: str) -> str:
    from runtime.docker_api import docker_request

    status, payload = docker_request("GET", f"/containers/{container_id}/logs?stdout=1&stderr=1&tail=40")
    return str(payload)[:1000]


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode())


if __name__ == "__main__":
    sys.exit(main())
