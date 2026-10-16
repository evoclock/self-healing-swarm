import json
import os
import threading
import time
from decimal import Decimal

import psycopg

import proxy.gate
import proxy.providers
import proxy.service
from proxy.app import begin_step, finish_step, resolve
from proxy.providers import Cassette, Completion, Provider, step_digest
from proxy.service import ProxyContext, dispatch
from runtime.config import KNOWN_TOOLS
from runtime.policy import RunPolicy
from runtime.runs import bind_token, open_run

URL = os.environ["DATABASE_URL"]
IP = "10.9.9.9"
BRIEF_TOOLS = frozenset({"mail.list", "mail.read", "memory.write", "draft.create"})


def context(tmp_path, faults=None) -> ProxyContext:
    return ProxyContext(URL, Cassette(tmp_path), "replay", "test", faults=list(faults or []))


def opened(run_id, tenant="t-a", agent="daily-brief", tools=BRIEF_TOOLS, max_usd=Decimal("1.00")) -> str:
    token = f"tok-{run_id}"
    with psycopg.connect(URL, autocommit=True) as conn:
        open_run(conn, tenant, run_id, agent, token, IP, RunPolicy(max_usd, frozenset(tools)))
        conn.execute("DELETE FROM audit_log WHERE detail->>'run_id' = %s", (run_id,))
    return token


def post(ctx, path, body) -> tuple[int, dict]:
    return dispatch(ctx, "POST", path, json.dumps(body).encode(), IP)


def query(tenant, sql, params=()):
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        return conn.execute(sql, params).fetchall()


def recorded(tmp_path, messages, text="ok", cost=None):
    Cassette(tmp_path).put(step_digest(messages, []), Completion(text, 3, 1, cost).to_json())


def test_open_run_stores_the_policy():
    opened("gate-policy", max_usd=Decimal("0.25"))
    rows = query("t-a", "SELECT max_usd, tools, generation FROM runs WHERE id = 'gate-policy'")
    assert rows == [(Decimal("0.25"), sorted(BRIEF_TOOLS), 0)]


def test_proxy_tools_are_the_config_vocabulary_without_py_exec():
    assert KNOWN_TOOLS - {"py.exec"} == proxy.gate.PROXY_TOOLS


def test_lying_budget_fields_are_ignored(tmp_path):
    token = opened("gate-budget", max_usd=Decimal("0.00001"))
    messages = [{"role": "user", "content": "budget probe"}]
    recorded(tmp_path, messages, cost=Decimal("0.01"))
    status, body = post(context(tmp_path), "/v1/complete", {
        "token": token,
        "messages": messages,
        "model": "primary",
        "step_no": 1,
        "spent_usd": 0,
        "max_usd": 1e9,
        "tokens_in_est": 0,
        "tokens_out_est": 0,
    })
    assert status == 402
    assert body["error"] == "budget"
    denials = query("t-a", "SELECT count(*) FROM audit_log WHERE detail->>'run_id' = 'gate-budget' AND detail->>'reason' = 'budget'")
    assert denials == [(1,)]


def test_a_reserved_estimate_counts_against_the_cap(tmp_path):
    token = opened("gate-reserve", max_usd=Decimal("0.005"))
    with psycopg.connect(URL, autocommit=True) as conn:
        begin_step(conn, "t-a", "gate-reserve", 1, "held", "primary", Decimal("0.004"), 0)
    messages = [{"role": "user", "content": "second step"}]
    recorded(tmp_path, messages, cost=Decimal("0.002"))
    status, _body = post(context(tmp_path), "/v1/complete", {"token": token, "messages": messages, "model": "primary", "step_no": 2})
    assert status == 402


def test_local_is_priced_at_zero_so_a_dollar_cap_does_not_deny_it(tmp_path):
    token = opened("gate-local", max_usd=Decimal("0.00001"))
    messages = [{"role": "user", "content": "local probe"}]
    recorded(tmp_path, messages, "pong")
    status, body = post(context(tmp_path), "/v1/complete", {"token": token, "messages": messages, "model": "local", "step_no": 1})
    assert status == 200
    assert body["text"] == "pong"


def test_unknown_model_is_refused(tmp_path):
    token = opened("gate-model")
    status, body = post(context(tmp_path), "/v1/complete", {
        "token": token, "messages": [{"role": "user", "content": "x"}], "model": "gpt-unknown", "step_no": 1,
    })
    assert status == 400
    assert body["error"] == "unknown_model"


def test_fault_queue_runs_before_the_provider(tmp_path):
    token = opened("gate-faults")
    messages = [{"role": "user", "content": "faults"}]
    recorded(tmp_path, messages)
    ctx = context(tmp_path, ["error", "429"])
    payload = {"token": token, "messages": messages, "model": "primary", "step_no": 1}
    assert post(ctx, "/v1/complete", payload)[0] == 503
    assert post(ctx, "/v1/complete", payload)[0] == 429
    assert post(ctx, "/v1/complete", {**payload, "model": "local"})[0] == 200


def test_tool_outside_the_run_list_is_denied(tmp_path):
    token = opened("gate-allow", tools={"mail.list"})
    status, body = post(context(tmp_path), "/v1/tool", {
        "token": token,
        "name": "draft.create",
        "args": {"draft_id": "gate-allow:x", "to": "team@a.example", "body": "hi"},
        "step_no": 1,
    })
    assert status == 200
    assert body == {"denied": True, "reason": "tool_not_allowed"}
    assert query("t-a", "SELECT draft_id FROM outbox WHERE draft_id = 'gate-allow:x'") == []


def test_py_exec_is_refused_and_not_run(tmp_path):
    token = opened("gate-pyexec", tools=BRIEF_TOOLS | {"py.exec"})
    marker = tmp_path / "marker"
    status, body = post(context(tmp_path), "/v1/tool", {
        "token": token,
        "name": "py.exec",
        "args": {"code": f"open({str(marker)!r}, 'w').write('ran')", "timeout": 2},
        "step_no": 1,
    })
    assert status == 200
    assert body == {"denied": True, "reason": "not_executed"}
    assert not marker.exists()
    for module in (proxy.service, proxy.gate):
        assert not hasattr(module, "py_exec")
        assert not hasattr(module, "_guard")


def test_draft_domains_come_from_the_tenant_row(tmp_path):
    token = opened("gate-domains")
    status, body = post(context(tmp_path), "/v1/tool", {
        "token": token,
        "name": "draft.create",
        "args": {"draft_id": "gate-domains:x", "to": "out@evil.example", "body": "hi", "domains": ["evil.example"]},
        "step_no": 1,
    })
    assert status == 200
    assert body["draft"]["flagged"] is True
    assert body["draft"]["flag_reasons"] == ["external"]


def test_mail_read_of_a_foreign_id_is_denied(tmp_path):
    token = opened("gate-foreign")
    status, body = post(context(tmp_path), "/v1/tool", {"token": token, "name": "mail.read", "args": {"ids": ["m-b-1"]}, "step_no": 1})
    assert status == 200
    assert body == {"skipped": False, "denied": True, "reason": "foreign_id"}
    steps = query("t-a", "SELECT response->>'reason' FROM steps WHERE run_id = 'gate-foreign'")
    assert steps == [("foreign_id",)]


def test_foreign_citation_stores_failed_and_no_cursor(tmp_path):
    token = opened("gate-cite-foreign")
    status, body = post(context(tmp_path), "/v1/state", {"token": token, "state": "SUCCEEDED", "artifact": "Brief\ncites: m-b-1"})
    assert status == 200
    assert body["state"] == "FAILED"
    assert query("t-a", "SELECT state FROM runs WHERE id = 'gate-cite-foreign'") == [("FAILED",)]
    assert query("t-a", "SELECT id FROM memory WHERE run_id = 'gate-cite-foreign'") == []


def test_missing_cites_line_stores_failed(tmp_path):
    token = opened("gate-cite-missing")
    status, body = post(context(tmp_path), "/v1/state", {"token": token, "state": "SUCCEEDED", "artifact": "Brief cites m-a-1"})
    assert (status, body["state"]) == (200, "FAILED")


def test_own_citation_succeeds_and_the_cursor_is_written_with_the_state(tmp_path):
    token = opened("gate-cite-own")
    status, body = post(context(tmp_path), "/v1/state", {"token": token, "state": "SUCCEEDED", "artifact": "Brief\ncites: m-a-1 m-a-2"})
    assert (status, body["state"]) == (200, "SUCCEEDED")
    assert query("t-a", "SELECT body, source_ids FROM memory WHERE run_id = 'gate-cite-own' AND kind = 'cursor'") == [("m-a-2", ["m-a-2"])]
    again, _ = post(context(tmp_path), "/v1/state", {"token": token, "state": "SUCCEEDED", "artifact": "Brief\ncites: m-a-3"})
    assert again == 409
    assert len(query("t-a", "SELECT id FROM memory WHERE run_id = 'gate-cite-own'")) == 1


def test_document_citation_succeeds_without_a_cursor(tmp_path):
    token = opened("gate-cite-doc", tenant="t-b", agent="doc-analyst", tools={"mail.list", "docs.read"})
    status, body = post(context(tmp_path), "/v1/state", {"token": token, "state": "SUCCEEDED", "artifact": "Total\nfigures 4875\ncites: doc-b-budget"})
    assert (status, body["state"]) == (200, "SUCCEEDED")
    assert query("t-b", "SELECT id FROM memory WHERE run_id = 'gate-cite-doc'") == []


def test_cursor_memory_write_from_the_agent_is_refused(tmp_path):
    token = opened("gate-cursor")
    status, body = post(context(tmp_path), "/v1/tool", {
        "token": token,
        "name": "memory.write",
        "args": {"agent_id": "daily-brief", "kind": "cursor", "body": "m-a-1", "source_ids": ["m-a-1"]},
        "step_no": 1,
    })
    assert body["reason"] == "cursor_on_success_only"
    assert query("t-a", "SELECT id FROM memory WHERE run_id = 'gate-cursor'") == []


def test_same_generation_started_step_gets_202_and_no_provider_call(tmp_path):
    token = opened("gate-202")
    messages = [{"role": "user", "content": "in flight"}]
    with psycopg.connect(URL, autocommit=True) as conn:
        begin_step(conn, "t-a", "gate-202", 1, step_digest(messages, []), "primary", Decimal("0.001"), 0)
    # Empty cassette: a provider call would be a 409 replay miss.
    status, body = post(context(tmp_path), "/v1/complete", {"token": token, "messages": messages, "model": "primary", "step_no": 1})
    assert (status, body) == (202, {"in_progress": True})


def test_old_same_generation_started_step_stays_202(tmp_path):
    token = opened("gate-stale")
    messages = [{"role": "user", "content": "still held"}]
    digest = step_digest(messages, [])
    with psycopg.connect(URL, autocommit=True) as conn:
        begin_step(conn, "t-a", "gate-stale", 1, digest, "primary", Decimal("0.001"), 0)
        conn.execute("UPDATE steps SET started_at = now() - interval '31 seconds' WHERE run_id = 'gate-stale'")
    before = query("t-a", "SELECT status, generation, cost_usd, started_at FROM steps WHERE run_id = 'gate-stale'")
    status, body = post(context(tmp_path), "/v1/complete", {"token": token, "messages": messages, "model": "primary", "step_no": 1})
    assert (status, body) == (202, {"in_progress": True})
    assert query("t-a", "SELECT status, generation, cost_usd, started_at FROM steps WHERE run_id = 'gate-stale'") == before


def test_a_recorded_call_sends_a_trace_and_the_next_step_nests(tmp_path):
    seen = []

    def upstream(messages, tools, model, temperature=None, max_tokens=None, project_id=None, trace=None):
        seen.append(trace)
        return Completion("ok", 1, 1, Decimal("0.00001"), "11111111-1111-1111-1111-111111111111")

    token = opened("gate-trace")
    ctx = ProxyContext(URL, Cassette(tmp_path), "record", "test", upstream=upstream)
    first, _body = post(ctx, "/v1/complete", {"token": token, "messages": [{"role": "user", "content": "one"}], "model": "primary", "step_no": 1})
    second, _body = post(ctx, "/v1/complete", {"token": token, "messages": [{"role": "user", "content": "two"}], "model": "primary", "step_no": 2})
    third, _body = post(ctx, "/v1/complete", {"token": token, "messages": [{"role": "user", "content": "local"}], "model": "local", "step_no": 3})
    assert (first, second, third) == (200, 200, 200)
    assert seen[0]["X-Merge-Trace-Id"] == "t-a.gate-trace"
    assert seen[0]["X-Merge-Thread-Id"] == "t-a"
    assert seen[0]["X-Merge-Turn-Id"] == "step-1"
    assert "X-Merge-Parent-Span-Id" not in seen[0]
    assert seen[1]["X-Merge-Parent-Span-Id"] == "11111111-1111-1111-1111-111111111111"
    assert seen[1]["X-Merge-Turn-Id"] == "step-2"
    assert seen[2] is None


def test_one_provider_call_while_a_duplicate_waits(tmp_path, monkeypatch):
    token = opened("gate-inflight")
    messages = [{"role": "user", "content": "slow call"}]
    recorded(tmp_path, messages, "once")
    entered = threading.Event()
    release = threading.Event()
    calls = []
    original = Provider.complete

    def blocking(self, messages, tools, model, temperature=None, max_tokens=None, project_id=None, trace=None):
        calls.append(model)
        entered.set()
        assert release.wait(5)
        return original(
            self, messages, tools, model, temperature=temperature, max_tokens=max_tokens, project_id=project_id, trace=trace
        )

    monkeypatch.setattr(Provider, "complete", blocking)
    holder = {}

    def run_holder():
        holder["result"] = post(
            context(tmp_path),
            "/v1/complete",
            {"token": token, "messages": messages, "model": "primary", "step_no": 1},
        )

    thread = threading.Thread(target=run_holder)
    thread.start()
    assert entered.wait(5)
    status, body = post(
        context(tmp_path),
        "/v1/complete",
        {"token": token, "messages": messages, "model": "primary", "step_no": 1},
    )
    assert (status, body) == (202, {"in_progress": True})
    assert calls == ["primary"]
    release.set()
    thread.join(5)
    assert holder["result"][0] == 200
    assert holder["result"][1]["text"] == "once"
    assert calls == ["primary"]


def test_newer_generation_takes_over_and_the_late_finisher_writes_nothing(tmp_path):
    opened("gate-gen")
    messages = [{"role": "user", "content": "resumed"}]
    digest = step_digest(messages, [])
    recorded(tmp_path, messages, "winner")
    with psycopg.connect(URL, autocommit=True) as conn:
        old_claim = begin_step(conn, "t-a", "gate-gen", 1, digest, "primary", Decimal("0.001"), 0)
        generation = bind_token(conn, "t-a", "gate-gen", "tok-gate-gen-2", IP)
    assert generation == 1
    status, body = post(context(tmp_path), "/v1/complete", {"token": "tok-gate-gen-2", "messages": messages, "model": "primary", "step_no": 1})
    assert (status, body["text"]) == (200, "winner")
    with psycopg.connect(URL, autocommit=True) as conn:
        late = finish_step(conn, "t-a", "gate-gen", 1, digest, "COMPLETED", {"text": "late"}, old_claim)
    assert late is False
    rows = query("t-a", "SELECT status, generation, response->>'text', cost_uncertain FROM steps WHERE run_id = 'gate-gen'")
    assert rows == [("COMPLETED", 1, "winner", True)]


def test_rebind_drops_the_previous_token():
    opened("gate-rebind")
    with psycopg.connect(URL, autocommit=True) as conn:
        bind_token(conn, "t-a", "gate-rebind", "tok-gate-rebind-2", IP)
        assert resolve(conn, "tok-gate-rebind", IP) is None
        assert resolve(conn, "tok-gate-rebind-2", IP) == {"tenant_id": "t-a", "run_id": "gate-rebind", "generation": 1}
    assert len(query("t-a", "SELECT token FROM run_tokens WHERE run_id = 'gate-rebind'")) == 1


class Upstream:
    def __init__(self):
        self.calls = 0

    def __call__(self, messages, tools, model, **kwargs):
        self.calls += 1
        return Completion("live", 10, 10, Decimal("0.000001"))


def live(tmp_path, upstream) -> ProxyContext:
    return ProxyContext(URL, Cassette(tmp_path), "record", "test", upstream=upstream)


def test_a_live_call_whose_estimate_would_pass_the_cap_is_refused_before_it_goes_out(tmp_path):
    token = opened("gate-live-cap", max_usd=Decimal("0.00001"))
    upstream = Upstream()
    status, body = post(live(tmp_path, upstream), "/v1/complete", {
        "token": token, "messages": [{"role": "user", "content": "live"}], "model": "primary", "step_no": 1,
    })
    assert status == 402 and body["error"] == "budget"
    assert upstream.calls == 0


def test_a_live_call_inside_the_cap_goes_out_and_is_charged_its_real_cost(tmp_path):
    token = opened("gate-live-ok", max_usd=Decimal("1.00"))
    upstream = Upstream()
    status, _body = post(live(tmp_path, upstream), "/v1/complete", {
        "token": token, "messages": [{"role": "user", "content": "live"}], "model": "primary", "step_no": 1,
    })
    assert status == 200 and upstream.calls == 1
    assert query("t-a", "SELECT cost_usd FROM steps WHERE run_id = 'gate-live-ok'") == [(Decimal("0.000001"),)]


def test_each_proxy_request_renews_the_run_lease(tmp_path):
    token = opened("gate-lease")
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
        conn.execute("UPDATE runs SET lease_until = now() - interval '1 hour' WHERE id = 'gate-lease'")
    post(context(tmp_path), "/v1/tool", {"token": token, "name": "mail.list", "args": {}, "step_no": 1})
    assert query("t-a", "SELECT lease_until > now() FROM runs WHERE id = 'gate-lease'") == [(True,)]


def test_a_live_model_without_a_price_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy.providers, "PRICE_PER_TOKEN", {})
    token = opened("gate-no-price")
    upstream = Upstream()
    status, body = post(live(tmp_path, upstream), "/v1/complete", {
        "token": token, "messages": [{"role": "user", "content": "x"}], "model": "primary", "step_no": 1,
    })
    assert status == 400 and body["error"] == "no_price"
    assert upstream.calls == 0


def test_the_lease_is_renewed_while_a_provider_call_is_in_flight(tmp_path):
    token = opened("gate-heartbeat")
    seen = []

    def slow(messages, tools, model, **kwargs):
        with psycopg.connect(URL, autocommit=True) as conn:
            conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
            conn.execute("UPDATE runs SET lease_until = now() - interval '1 hour' WHERE id = 'gate-heartbeat'")
            time.sleep(0.4)
            seen.append(conn.execute("SELECT lease_until > now() FROM runs WHERE id = 'gate-heartbeat'").fetchone()[0])
        return Completion("live", 1, 1, Decimal("0.000001"))

    ctx = live(tmp_path, slow)
    ctx.heartbeat_s = 0.05
    status, _body = post(ctx, "/v1/complete", {
        "token": token, "messages": [{"role": "user", "content": "slow"}], "model": "primary", "step_no": 1,
    })
    assert status == 200
    assert seen == [True]


def test_an_armed_tool_error_fails_one_tool_call_and_leaves_the_model_fault_queue(tmp_path):
    token = opened("gate-tool-fault")
    ctx = context(tmp_path, ["tool_error", "error"])
    status, body = post(ctx, "/v1/tool", {"token": token, "name": "mail.list", "args": {}, "step_no": 1})
    assert status == 500 and body["simulated"] is True
    status, _body = post(ctx, "/v1/tool", {"token": token, "name": "mail.list", "args": {}, "step_no": 1})
    assert status == 200
    assert ctx.faults == ["error"]
