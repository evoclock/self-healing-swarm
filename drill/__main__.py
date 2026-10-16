import json
import os
import sys
import time
import urllib.request
import uuid
from decimal import Decimal
from pathlib import Path

from agent.client import ProxyClient
from evals.gate import config_hash, gate, load_golden, load_recordings
from evals.store import promoted_hashes, write_eval_record
from outbox.send import FlaggedNeedsReview, approve, create_draft, deliver
from outbox.smtp_log import serve_smtp
from proxy.providers import step_digest
from runtime.config import load_agent
from runtime.docker_api import (
    container_ip,
    container_logs,
    create_agent_container,
    inspect_container,
    kill_container,
    remove_container,
    start_container,
    wait_container,
)
from runtime.policy import RunPolicy
from runtime.runs import bind_token, connect, events, open_run, record_container, reset_run, run_row, step_costs
from runtime.dispatch import dispatch_once, escalate_run, launch_run
from runtime.scheduler import Job, load_config
from runtime.watcher import resume_in_container, watch_once

BRIEF = "agents/t-a/daily_brief.yaml"
DOC = "agents/t-b/doc_analyst.yaml"
TERMINAL = ("SUCCEEDED", "FAILED", "STOPPED_LOOP", "STOPPED_BUDGET", "ESCALATED")


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
        ("document", scenario_document),
        ("tool_error", scenario_tool_error),
        ("dispatch", scenario_dispatch),
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
    network = os.environ["AGENT_NETWORK"]
    host_repo = os.environ["HOST_REPO"]
    run_id = "drill-kill"
    token = "drill-kill-token"
    conn = connect(os.environ["DATABASE_URL"])
    open_run(conn, "t-a", run_id, "daily-brief", token, "10.255.255.1", _policy(BRIEF), BRIEF, "brief")
    first = _start_agent(host_repo, network, token, run_id, BRIEF, "brief", pause=True)
    record_container(conn, "t-a", run_id, first)
    try:
        ip = container_ip(inspect_container(first), network)
        bind_token(conn, "t-a", run_id, token, ip)
        if not _wait_step(conn, run_id, 2):
            return False, "step 2 did not complete\n" + container_logs(first)
        before = step_costs(conn, "t-a", run_id)
        kill_container(first)
        # The lease watcher notices the lapsed lease and starts a fresh container under a new token generation.
        # It never calls open_run, which would wipe the journal.
        started: list[str] = []
        for _ in range(60):
            watch_once(conn, lambda tenant, rid: started.append(resume_in_container(conn, tenant, rid)), only={run_id})
            if started:
                break
            time.sleep(1)
        if not started:
            return False, "watcher did not resume the run"
        second = started[0]
        try:
            generation = conn.execute("SELECT generation FROM runs WHERE id = %s", (run_id,)).fetchone()[0]
            if not _wait_state(conn, "t-a", run_id, ("SUCCEEDED",)):
                return False, "resume did not succeed\n" + container_logs(second)
        finally:
            remove_container(second)
    finally:
        remove_container(first)
    after = step_costs(conn, "t-a", run_id)
    costs_held = all(any(later[0] == earlier[0] and later[2] == earlier[2] for later in after) for earlier in before)
    row = run_row(conn, "t-a", run_id)
    ok = row["state"] == "SUCCEEDED" and costs_held and row["resume_count"] == 1
    detail = (
        f"run_id {run_id} state {row['state']} resume {row['resume_count']} generation {generation}\n"
        f"steps {after}\nevents {events(conn, 't-a', run_id)}"
    )
    return ok, detail if ok else detail + f"\nbefore {before}"


def scenario_provider() -> tuple[bool, str]:
    row, ev, result = _run("drill-provider", "t-a", "daily-brief", BRIEF, "brief", ["error", "429"])
    ok = row["state"] == "SUCCEEDED" and "provider_fallback" in ev
    return ok, f"run_id drill-provider state {row['state']}\nevents {ev}\n{result}"


def scenario_injection() -> tuple[bool, str]:
    row, ev, result = _run("drill-injection", "t-a", "daily-brief", BRIEF, "brief", [])
    conn = connect(os.environ["DATABASE_URL"])
    conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
    draft = conn.execute(
        "SELECT flagged, state FROM outbox WHERE draft_id = %s",
        ("drill-injection:injection",),
    ).fetchone()
    cursor = conn.execute(
        "SELECT body FROM memory WHERE run_id = 'drill-injection' AND kind = 'cursor'"
    ).fetchall()
    try:
        approve(conn, "t-a", "drill-injection:injection", "approver-a")
        held = False
    except FlaggedNeedsReview:
        held = True
    ok = held and draft is not None and draft[0] is True and draft[1] == "DRAFT" and row["state"] == "SUCCEEDED" and len(cursor) == 1
    return ok, (
        f"run_id drill-injection draft {draft} ordinary approval refused {held} state {row['state']} cursor {cursor}\nevents {ev}"
    )


def scenario_escape() -> tuple[bool, str]:
    """Proof is what the drill observes itself. The agent's ESCALATED state is reported, not counted."""
    conn = connect(os.environ["DATABASE_URL"])
    network = os.environ["AGENT_NETWORK"]
    host_repo = os.environ["HOST_REPO"]

    # 1. The proxy refuses py.exec. The code would leave a marker in the proxy's /src and print /etc/shadow.
    marker = Path(f".audit/escape-marker-{uuid.uuid4().hex}")
    code = f"open('/src/{marker}', 'w').write('ran'); print(open('/etc/shadow').read())"
    client = ProxyClient(os.environ["PROXY_A_URL"], "token-drill-escape-proxy")
    open_run(conn, "t-a", "drill-escape-proxy", "daily-brief", client.token, client.whoami(), _policy(BRIEF))
    refused = client.tool("py.exec", {"code": code, "timeout": 2}, 1)
    body = json.dumps(refused)
    proxy_ok = refused.get("reason") == "not_executed" and "root:" not in body and not marker.exists()

    # 2. A container with the agent HostConfig on swarm-agents runs the two probes itself. Both must fail.
    probes = {
        "shadow": "open('/etc/shadow').read()",
        "connect": "import socket; socket.create_connection(('example.com', 80), 3)",
    }
    probe_out = []
    probes_ok = True
    for name, probe in probes.items():
        cid = create_agent_container(
            f"swarm-drill-escape-{name}",
            host_repo,
            network,
            ["PYTHONDONTWRITEBYTECODE=1", "HOME=/tmp"],
            cmd=["python", "-c", probe],
        )
        try:
            start_container(cid)
            code_exit = wait_container(cid)
            last = (container_logs(cid).strip().splitlines() or [""])[-1]
        finally:
            remove_container(cid)
        probes_ok = probes_ok and code_exit != 0
        probe_out.append(f"{name} exit {code_exit} {last}")

    # 3. The agent's own escape script, in a locked container. One proxy-written foreign_id step is required.
    # Older drills ran drill-escape as t-b. RLS hides that row from a t-a reset, so clear it under t-b.
    reset_run(conn, "t-b", "drill-escape")
    row, logs = run_locked("drill-escape", "t-a", "daily-brief", BRIEF, "escape", _policy(BRIEF))
    denied = foreign_denials(conn, "t-a", "drill-escape")
    ok = proxy_ok and probes_ok and len(denied) == 1
    detail = (
        f"proxy py.exec reason {refused.get('reason')} marker {marker.exists()}\n"
        + "\n".join(probe_out)
        + f"\nrun_id drill-escape agent state {row['state']} (reported) foreign_id steps {len(denied)}"
    )
    return ok, detail if ok else detail + "\n" + logs[-800:]


def scenario_budget() -> tuple[bool, str]:
    conn = connect(os.environ["DATABASE_URL"])
    _clear_audit(conn, "t-a", "drill-budget")
    policy = _policy(BRIEF).with_max(Decimal("0.00001"))
    row, ev, result = _run("drill-budget", "t-a", "daily-brief", BRIEF, "budget", [], policy)
    denials = _budget_denials(conn, "t-a", "drill-budget")
    estimate = _budget_estimate(conn, "t-a", "drill-budget")
    ok = row["state"] == "STOPPED_BUDGET" and "saved progress" in (row["artifact"] or "") and denials == 1
    return ok, (
        f"run_id drill-budget state {row['state']} artifact {row['artifact']} "
        f"proxy denials {denials} estimate {estimate}\nevents {ev}"
    )


def scenario_hang() -> tuple[bool, str]:
    row, ev, result = _run("drill-hang", "t-a", "daily-brief", BRIEF, "hang", [])
    ok = "hang_cancel" in ev and row["state"] == "ESCALATED"
    return ok, f"run_id drill-hang state {row['state']}\nevents {ev}"


def scenario_loop() -> tuple[bool, str]:
    row, ev, result = _run("drill-loop", "t-a", "daily-brief", BRIEF, "loop", [])
    ok = row["state"] == "STOPPED_LOOP" and row["artifact"].strip() != ""
    first_line = row["artifact"].strip().splitlines()[0] if row["artifact"].strip() else ""
    return ok, f"run_id drill-loop state {row['state']} artifact {first_line}\nevents {ev}"


def scenario_empty() -> tuple[bool, str]:
    row, ev, result = _run("drill-empty", "t-a", "daily-brief", BRIEF, "empty", [])
    ok = row["state"] == "FAILED" and row["artifact"] == ""
    return ok, f"run_id drill-empty state {row['state']} artifact {row['artifact']!r}\nevents {ev}"


def scenario_document() -> tuple[bool, str]:
    """The document agent reads the spreadsheet attachment and runs the fixed sheet program inside its container."""
    row, ev, result = _run("drill-document", "t-b", "doc-analyst", DOC, "doc", [])
    ok = row["state"] == "SUCCEEDED" and "figures " in (row["artifact"] or "")
    figures = next((line for line in (row["artifact"] or "").splitlines() if line.startswith("figures ")), "")
    return ok, f"run_id drill-document state {row['state']} {figures}\nevents {ev}" + ("" if ok else f"\n{result}")


def scenario_tool_error() -> tuple[bool, str]:
    """Two tool calls fail with 500; the agent retries with backoff and the run still succeeds."""
    row, ev, _result = _run("drill-tool-error", "t-a", "daily-brief", BRIEF, "brief", ["tool_error", "tool_error"])
    ok = row["state"] == "SUCCEEDED" and ev.count("tool_retry") == 2 and ev.count("tool_error") == 2
    return ok, f"run_id drill-tool-error state {row['state']}\nevents {ev}"


def scenario_dispatch() -> tuple[bool, str]:
    """dispatch_once, the code the dispatch service loops over: deadline order, start in containers, escalate a miss."""
    conn = connect(os.environ["DATABASE_URL"])
    plan = {
        "drill-dispatch-brief": ("t-a", "daily-brief", BRIEF, Job("drill-dispatch-brief", "t-a", 2, deadline=30)),
        "drill-dispatch-doc": ("t-b", "doc-analyst", DOC, Job("drill-dispatch-doc", "t-b", 2, deadline=20)),
        "drill-dispatch-late": ("t-c", "daily-brief", "agents/t-c/daily_brief.yaml", Job("drill-dispatch-late", "t-c", 2, deadline=1)),
    }
    for run_id, (tenant, *_rest) in plan.items():
        reset_run(conn, tenant, run_id)
    started = []

    def launch(job):
        tenant, agent_id, path, _job = plan[job.job_id]
        started.append((tenant, job.job_id, launch_run(conn, tenant, job.job_id, agent_id, path, None, _policy(path))))

    def escalate(job):
        tenant, agent_id, path, _job = plan[job.job_id]
        escalate_run(conn, tenant, job.job_id, agent_id, path, _policy(path))

    dispatch_once([item[3] for item in plan.values()], {"t-a": 1, "t-b": 1, "t-c": 1}, launch, escalate)
    try:
        for tenant, run_id, _cid in started:
            _wait_state(conn, tenant, run_id, TERMINAL)
    finally:
        for _tenant, _run_id, cid in started:
            remove_container(cid)
    states = {run_id: run_row(conn, plan[run_id][0], run_id)["state"] for run_id in plan}
    order = [run_id for _tenant, run_id, _cid in started]
    ok = (
        order == ["drill-dispatch-doc", "drill-dispatch-brief"]
        and states["drill-dispatch-doc"] == "SUCCEEDED"
        and states["drill-dispatch-brief"] == "SUCCEEDED"
        and states["drill-dispatch-late"] == "ESCALATED"
        and "deadline_escalated" in events(conn, "t-c", "drill-dispatch-late")
    )
    return ok, f"started in order {order}\nstates {states}"


def scenario_eval() -> tuple[bool, str]:
    """Writes its own baseline every run. No model call; scores the committed recordings."""
    conn = connect(os.environ["DATABASE_URL"])
    golden = Path("evals/golden")
    recordings = Path("recordings/evals")
    cases = load_golden(golden, "daily-brief")
    brief = load_agent(Path(BRIEF))
    worse = load_agent(Path("agents/daily_brief.worse.yaml"))
    harmless = load_agent(Path("agents/daily_brief.harmless.yaml"))
    brief_hash, worse_hash, harmless_hash = config_hash(brief), config_hash(worse), config_hash(harmless)
    conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
    conn.execute("DELETE FROM eval_records WHERE tenant_id = 't-a' AND agent_id = 'daily-brief'")
    baseline = gate(cases, load_recordings(recordings, brief_hash, cases), None, brief_hash)
    write_eval_record(conn, "t-a", "daily-brief", baseline)
    worse_result = gate(cases, load_recordings(recordings, worse_hash, cases), baseline.mean, worse_hash)
    write_eval_record(conn, "t-a", "daily-brief", worse_result)
    harmless_result = gate(cases, load_recordings(recordings, harmless_hash, cases), baseline.mean, harmless_hash)
    write_eval_record(conn, "t-a", "daily-brief", harmless_result)
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
    ok = (
        baseline.promoted
        and worse_result.exit_code == 1
        and named
        and refused
        and harmless_result.exit_code == 0
        and accepted.id == "daily-brief"
    )
    detail = (
        f"baseline mean {baseline.mean} promoted {baseline.promoted}\n"
        f"worse exit {worse_result.exit_code}\n"
        + "\n".join(worse_result.failures[:6])
        + f"\nharmless exit {harmless_result.exit_code} mean {harmless_result.mean} delta {_round(harmless_result.delta)}"
        + f"\nload_config worse refused {refused} harmless loaded {accepted.id == 'daily-brief'}"
    )
    return ok, detail


def scenario_send() -> tuple[bool, str]:
    conn = connect(os.environ["DATABASE_URL"])
    conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
    conn.execute("DELETE FROM outbox WHERE draft_id = 'drill-send'")
    create_draft(conn, "t-a", "drill-send", "ada@a.example", "hello", "body")
    approve(conn, "t-a", "drill-send", "approver-a")
    path = Path(".audit/smtp.log")
    if path.exists():
        path.write_text("")
    server, _thread = serve_smtp(path, "127.0.0.1", 2525)
    first_sink: list = []
    second_sink: list = []
    try:
        deliver(conn, "t-a", "drill-send", first_sink, ("127.0.0.1", 2525), path)
        # A crash after SMTP leaves SENDING. The retry has a fresh sink; the SMTP log is what stops a resend.
        conn.execute("UPDATE outbox SET state = 'SENDING' WHERE draft_id = 'drill-send'")
        deliver(conn, "t-a", "drill-send", second_sink, ("127.0.0.1", 2525), path)
    finally:
        server.close()
    logged = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    ok = len(logged) == 1 and len(first_sink) == 1 and second_sink == []
    message = logged[0]["message_id"] if logged else None
    return ok, f"smtp {len(logged)} first sink {len(first_sink)} second sink {len(second_sink)} message {message}"


def run_locked(
    run_id: str, tenant: str, agent_id: str, config_path: str, scenario: str, policy: RunPolicy, faults: list | None = None
) -> tuple[dict, str]:
    """open_run, start a fresh locked agent container, bind its IP, wait for a terminal state."""
    network = os.environ["AGENT_NETWORK"]
    conn = connect(os.environ["DATABASE_URL"])
    token = f"token-{run_id}"
    open_run(conn, tenant, run_id, agent_id, token, "10.255.255.1", policy, config_path, scenario)
    ProxyClient(os.environ["PROXY_A_URL"], "").faults(list(faults or []))
    cid = _start_agent(os.environ["HOST_REPO"], network, token, run_id, config_path, scenario, pause=False)
    record_container(conn, tenant, run_id, cid)
    try:
        bind_token(conn, tenant, run_id, token, container_ip(inspect_container(cid), network))
        _wait_state(conn, tenant, run_id, TERMINAL)
        logs = container_logs(cid)
    finally:
        remove_container(cid)
        ProxyClient(os.environ["PROXY_A_URL"], "").faults([])
    return run_row(conn, tenant, run_id), logs


def foreign_denials(conn, tenant: str, run_id: str) -> list:
    """Proxy-written steps for mail.read of m-b-1 that ended in foreign_id. The agent cannot write steps."""
    digest = step_digest([{"tool": "mail.read", "args": {"ids": ["m-b-1"]}}], [])
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    return conn.execute(
        """
        SELECT step_no FROM steps
        WHERE run_id = %s AND arg_hash = %s AND status = 'COMPLETED'
          AND response->>'reason' = 'foreign_id'
        """,
        (run_id, digest),
    ).fetchall()


def _budget_estimate(conn, tenant: str, run_id: str) -> str:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    row = conn.execute(
        """
        SELECT detail->>'estimate' FROM audit_log
        WHERE detail->>'reason' = 'budget' AND detail->>'run_id' = %s
        ORDER BY id DESC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    return "" if row is None or row[0] is None else row[0]


def _budget_denials(conn, tenant: str, run_id: str) -> int:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    row = conn.execute(
        "SELECT count(*) FROM audit_log WHERE detail->>'reason' = 'budget' AND detail->>'run_id' = %s",
        (run_id,),
    ).fetchone()
    return int(row[0])


def _clear_audit(conn, tenant: str, run_id: str) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    conn.execute("DELETE FROM audit_log WHERE detail->>'run_id' = %s", (run_id,))


def _policy(config_path: str) -> RunPolicy:
    return RunPolicy.from_config(load_agent(Path(config_path)))


def _run(run_id, tenant, agent_id, config_path, scenario, faults, policy: RunPolicy | None = None):
    """Every scenario runs in a fresh locked container through run_locked. Returns (row, events, container log tail)."""
    row, logs = run_locked(run_id, tenant, agent_id, config_path, scenario, policy or _policy(config_path), faults)
    conn = connect(os.environ["DATABASE_URL"])
    return row, events(conn, tenant, run_id), (logs.strip().splitlines() or [""])[-1][:600]


def _start_agent(
    host_repo: str,
    network: str,
    token: str,
    run_id: str,
    config_path: str,
    scenario: str,
    pause: bool,
    suffix: str = "",
) -> str:
    env = [
        "PYTHONPATH=/src",
        "PYTHONDONTWRITEBYTECODE=1",
        "HOME=/tmp",
        f"PROXY_URL={os.environ['PROXY_A_URL']}",
        f"RUN_TOKEN={token}",
        f"RUN_ID={run_id}",
        f"AGENT_CONFIG=/src/{config_path}",
        f"SCENARIO={scenario}",
    ]
    if pause:
        env.extend(["DRILL_PAUSE_AFTER_STEP=2", "DRILL_PAUSE_SECONDS=300"])
    container_id = create_agent_container(f"swarm-{run_id}{suffix}", host_repo, network, env)
    start_container(container_id)
    return container_id


def _wait_step(conn, run_id: str, step_no: int) -> bool:
    for _ in range(90):
        rows = step_costs(conn, "t-a", run_id)
        if any(row[0] == step_no and row[1] == "COMPLETED" for row in rows):
            return True
        time.sleep(1)
    return False


def _wait_state(conn, tenant: str, run_id: str, states: tuple[str, ...]) -> bool:
    for _ in range(120):
        row = run_row(conn, tenant, run_id)
        if row and row["state"] in states:
            return True
        time.sleep(1)
    return False


def _round(value):
    return None if value is None else round(value, 2)


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode())


if __name__ == "__main__":
    sys.exit(main())
