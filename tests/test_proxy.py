import os
import socket

import psycopg
import pytest

from proxy.app import Call, Decision, begin_step, decide, finish_step, resolve, write_denial
from proxy.providers import (
    Cassette,
    Completion,
    Provider,
    ReplayMiss,
    estimate_usd,
    gateway_model,
    gateway_upstream,
    outbound_headers,
    step_digest,
)

ALLOW = frozenset({"provider.internal"})


def call(**overrides) -> Call:
    base = dict(
        method="GET",
        host="provider.internal",
        port=443,
        path="/tenants/t-a/mail",
        tenant_id="t-a",
        source_ip="10.0.0.8",
        token_ip="10.0.0.8",
        allow_hosts=ALLOW,
        spent_usd=0.10,
        estimate_usd=0.20,
        max_usd=1.00,
    )
    base.update(overrides)
    return Call(**base)


def test_allow_when_host_tenant_and_budget_fit():
    assert decide(call()) == Decision(True, "allow")


def test_cross_tenant_path_is_denied():
    decision = decide(call(path="/tenants/t-b/mail"))
    assert decision.allow is False
    assert decision.reason == "cross_tenant"


def test_connect_is_denied():
    decision = decide(call(method="CONNECT", path="/"))
    assert decision.allow is False
    assert decision.reason == "connect"


def test_loopback_proxy_port_is_denied():
    decision = decide(call(host="127.0.0.1", port=18894, path="/"))
    assert decision.allow is False
    assert decision.reason == "loopback"


def test_budget_refusal():
    estimate = estimate_usd("primary", 100_000, 0)
    decision = decide(call(spent_usd=0.90, estimate_usd=estimate, max_usd=1.00))
    assert decision.allow is False
    assert decision.reason == "budget"


def test_replay_serves_a_recording_without_network(tmp_path, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(socket, "create_connection", explode)
    messages = [{"role": "user", "content": "hello"}]
    tools = []
    digest = step_digest(messages, tools)
    cassette = Cassette(tmp_path)
    cassette.put(digest, Completion("ok", 3, 1).to_json())
    result = Provider(cassette, mode="replay").complete(messages, tools, "primary")
    assert result.text == "ok"
    assert result.tokens_in == 3
    assert result.tokens_out == 1


def test_replay_miss_names_the_digest(tmp_path):
    with pytest.raises(ReplayMiss) as caught:
        Provider(Cassette(tmp_path), mode="replay").complete(
            [{"role": "user", "content": "missing"}],
            [],
            "primary",
        )
    assert caught.value.digest == step_digest([{"role": "user", "content": "missing"}], [])


def _chat_client():
    class Message:
        content = "pong"

    class Choice:
        message = Message()

    class Usage:
        prompt_tokens = 4
        completion_tokens = 1

    class Response:
        choices = [Choice()]
        usage = Usage()

    class Completions:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return Response()

    class Chat:
        def __init__(self):
            self.completions = Completions()

    class Client:
        def __init__(self):
            self.chat = Chat()

    return Client()


def test_primary_maps_to_the_gateway_model():
    assert gateway_model("primary") == "zai/glm-5.3-flash"
    assert gateway_model("local") == "GLM-5.3-Flash-EXL3-TF-FNC7"


def test_local_uses_its_own_client(tmp_path):
    primary = _chat_client()
    local = _chat_client()
    messages = [{"role": "user", "content": "Reply with the single word pong."}]
    result = Provider(
        Cassette(tmp_path),
        mode="record",
        upstream=gateway_upstream("gateway-key", clients={"primary": primary, "local": local}),
    ).complete(messages, [], "local")
    assert result.text == "pong"
    assert local.chat.completions.kwargs["model"] == "GLM-5.3-Flash-EXL3-TF-FNC7"
    assert primary.chat.completions.kwargs is None


def test_record_calls_the_gateway_and_stores_the_reply(tmp_path):
    client = _chat_client()
    messages = [{"role": "user", "content": "Reply with the single word pong."}]
    cassette = Cassette(tmp_path)
    result = Provider(cassette, mode="record", upstream=gateway_upstream("not-a-key", client)).complete(
        messages, [], "primary"
    )
    assert result.text == "pong"
    assert result.tokens_in == 4
    assert client.chat.completions.kwargs["model"] == "zai/glm-5.3-flash"
    stored = Cassette(tmp_path).get(step_digest(messages, []))
    assert Completion.from_json(stored).text == "pong"


def test_provider_key_is_injected_and_not_taken_from_the_agent():
    headers = outbound_headers({"x-run": "1", "authorization": "Bearer stolen"}, "real-key")
    assert headers["authorization"] == "Bearer real-key"
    assert headers["x-run"] == "1"


def test_two_connections_resolve_the_same_token():
    url = os.environ["DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as setup:
        setup.execute("SELECT set_config('app.tenant_id', 't-a', false)")
        setup.execute(
            """
            INSERT INTO run_tokens (token, tenant_id, run_id, container_ip)
            VALUES ('tok-a', 't-a', 'r-a-1', '10.0.0.8')
            ON CONFLICT (token) DO NOTHING
            """
        )
        hidden = setup.execute("SELECT token FROM run_tokens").fetchall()
    with psycopg.connect(url, autocommit=True) as left, psycopg.connect(url, autocommit=True) as right:
        assert resolve(left, "tok-a", "10.0.0.8") == {"tenant_id": "t-a", "run_id": "r-a-1", "generation": 0}
        assert resolve(right, "tok-a", "10.0.0.8") == {"tenant_id": "t-a", "run_id": "r-a-1", "generation": 0}
        assert resolve(left, "tok-a", "10.0.0.9") is None
        direct = left.execute("SELECT token FROM run_tokens").fetchall()
    assert ("tok-a",) in hidden
    assert direct == []


def test_journal_writes_started_then_completed_and_denial():
    url = os.environ["DATABASE_URL"]
    digest = "abc123"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.tenant_id', 't-a', false)")
        conn.execute("DELETE FROM steps WHERE arg_hash = %s", (digest,))
        conn.execute("DELETE FROM audit_log WHERE detail->>'reason' = 'connect'")
        claim = begin_step(conn, "t-a", "r-a-1", 1, digest, "primary", 0.01)
        assert claim is not None
        assert begin_step(conn, "t-a", "r-a-1", 1, digest, "primary", 0.01) is None
        status = conn.execute(
            "SELECT status FROM steps WHERE run_id = 'r-a-1' AND step_no = 1 AND arg_hash = %s",
            (digest,),
        ).fetchone()
        assert status == ("STARTED",)
        assert finish_step(conn, "t-a", "r-a-1", 1, digest, "COMPLETED", {"text": "ok"}, claim) is True
        status = conn.execute(
            "SELECT status FROM steps WHERE run_id = 'r-a-1' AND step_no = 1 AND arg_hash = %s",
            (digest,),
        ).fetchone()
        assert status == ("COMPLETED",)
        write_denial(conn, "t-a", "connect", {"path": "/"})
        event = conn.execute(
            "SELECT event, detail->>'reason' FROM audit_log WHERE tenant_id = 't-a' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert event == ("policy_denied", "connect")
