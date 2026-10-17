import sys
import threading
import time
from contextlib import contextmanager

import random

from agent.loop import after_hang, after_provider_pressure, after_tool_error, backoff_delay, is_loop, stop_with_progress
from agent.prompts import brief_prompt, doc_prompt, external_recipient
from agent.tools import py_exec, sheet_total
from proxy.providers import OUTPUT_CAP, prompt_tokens
from runtime.citations import cites_line
from runtime.config import AgentConfig


NO_NEW_MAIL = "no new mail"
HEARTBEAT_S = 5.0


@contextmanager
def local_work(client, interval: float = HEARTBEAT_S):
    """Heartbeat to the proxy every `interval` seconds while the agent works locally, so the lease renews."""
    stop = threading.Event()

    def beat():
        while not stop.wait(interval):
            try:
                client.heartbeat()
            except Exception as exc:  # a missed beat only shortens the lease; the next one retries
                print(f"heartbeat failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=interval)


class LimitReached(Exception):
    def __init__(self, limit: str):
        self.limit = limit
        super().__init__(limit)


class Guarded:
    """Wraps the proxy client. Each call is checked against max_steps, max_tokens and wall_s before it goes out.

    Every attempt counts as a step, retries included. A tool error (status 500 and up) retries three times with
    backoff (0.5 s, 1 s, 2 s), then the error goes back to the model.
    """

    backoff_s = 0.5

    def __init__(self, client, config: AgentConfig, clock, sleep, uniform=random.uniform):
        self._uniform = uniform
        self._client = client
        self._limits = config.limits
        self._clock = clock
        self._sleep = sleep
        self._start = None
        self.steps = 0
        self.tokens = 0
        self.pause_after = getattr(client, "pause_after", None)
        self.pause_after_seconds = getattr(client, "pause_after_seconds", 0)

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)

    def uniform(self, low: float, high: float) -> float:
        return self._uniform(low, high)

    def heartbeat(self) -> dict:
        beat = getattr(self._client, "heartbeat", None)
        return beat() if beat else {}

    def forward(self, method: str, target: str) -> dict:
        return self._client.forward(method, target)

    def state(self, state: str, artifact: str) -> dict:
        return self._client.state(state, artifact)

    def event(self, event: str, detail: dict) -> dict:
        return self._client.event(event, detail)

    def _check(self, tokens_next: int = 0) -> None:
        now = self._clock()
        if self._start is None:
            self._start = now
        if self.steps >= self._limits.max_steps:
            raise LimitReached("max_steps")
        if now - self._start > self._limits.wall_s:
            raise LimitReached("wall_s")
        if self.tokens + tokens_next > self._limits.max_tokens:
            raise LimitReached("max_tokens")
        self.steps += 1

    def tool(self, name: str, args: dict, step_no: int) -> dict:
        failures = 0
        while True:
            self._check()
            result = self._client.tool(name, args, step_no)
            if result.get("status", 200) < 500:
                return result
            action = after_tool_error(failures)
            failures += 1
            if action == "retry":
                self._client.event("tool_retry", {"tool": name, "attempt": failures})
                self._sleep(self.backoff_s * 2 ** (failures - 1))
                continue
            self._client.event("tool_error_to_model", {"tool": name, "error": result.get("error")})
            return result

    def complete(self, messages, model: str, step_no: int) -> dict:
        self._check(prompt_tokens(messages, []) + OUTPUT_CAP)
        result = self._client.complete(messages, model, step_no)
        self.tokens += int(result.get("tokens_in") or 0) + int(result.get("tokens_out") or 0)
        return result


def execute(
    client, config: AgentConfig, run_id: str, scenario: str = "brief", executor=None, clock=time.monotonic, sleep=time.sleep, uniform=random.uniform,
    heartbeat_s: float = HEARTBEAT_S,
) -> dict:
    """executor runs code locally, in whatever container this process is in. Tests pass a fake.

    A limit stop is STOPPED_BUDGET with artifact "saved progress: <limit>" and a limit_reached event naming it.
    """
    guarded = Guarded(client, config, clock, sleep, uniform)
    try:
        return _dispatch(guarded, config, run_id, scenario, _beating(guarded, executor or py_exec, heartbeat_s))
    except LimitReached as exc:
        kind, artifact = stop_with_progress("STOPPED_BUDGET", f"saved progress: {exc.limit}")
        client.event("limit_reached", {"limit": exc.limit})
        stored = client.state(kind, artifact)
        return {"state": stored.get("state", kind), "artifact": artifact}


def _beating(client, executor, interval: float):
    """Every local executor call (py.exec, the sheet program) runs with the heartbeat on."""

    def run(*args, **kwargs):
        with local_work(client, interval):
            return executor(*args, **kwargs)

    return run


def _dispatch(client, config: AgentConfig, run_id: str, scenario: str, executor) -> dict:
    if scenario == "escape":
        return _escape(client, run_id, executor)
    if scenario == "budget":
        return _budget(client, config, run_id)
    if scenario == "hang":
        return _hang(client, run_id, executor)
    if scenario == "loop":
        return _loop(client, config, run_id)
    if scenario == "empty":
        stored = client.state("SUCCEEDED", "")
        return {"state": stored.get("state"), "artifact": ""}
    if scenario == "doc":
        return _document(client, config, run_id, executor)
    return _brief(client, config, run_id)


def _brief(client, config: AgentConfig, run_id: str, base: int = 0) -> dict:
    """base offsets the step numbers for each re-list after a new_mail retry (11 to 14, 21 to 24, ...)."""
    listed = client.tool("mail.list", {}, base + 1)
    messages = listed.get("messages") or []
    if not messages and "messages" in listed:
        # mail.list starts after the stored cursor; nothing new means nothing to brief. The proxy checks this.
        stored = client.state("SUCCEEDED", NO_NEW_MAIL)
        if stored.get("retry") == "new_mail":
            # Mail arrived between the list and the state post: list again and brief it. Each round uses steps, so
            # repeated races end in STOPPED_BUDGET (max_steps), never a run left RUNNING.
            return _brief(client, config, run_id, base=base + 10)
        return {"state": stored.get("state"), "artifact": NO_NEW_MAIL}
    client.tool("mail.read", {"ids": [message["id"] for message in messages]}, base + 2)
    if getattr(client, "pause_after", None) == 2:
        time.sleep(client.pause_after_seconds)
    text, model = _model(client, config, brief_prompt(config.instructions, messages, listed.get("facts")), base + 3)
    if text is None:
        return {"state": model, "artifact": ""}
    recipient = external_recipient(messages, listed.get("domains") or [])
    if recipient:
        client.tool(
            "draft.create",
            {
                "draft_id": f"{run_id}:injection",
                "to": recipient,
                "subject": "flagged",
                "body": "Flagged request. Not sent.",
            },
            base + 4,
        )
    cited = tuple(message["id"] for message in messages)
    artifact = (text or "Brief") + "\n" + cites_line(cited)
    stored = client.state("SUCCEEDED", artifact)
    return {"state": stored.get("state"), "artifact": artifact, "model": model}


def _document(client, config: AgentConfig, run_id: str, executor) -> dict:
    listed = client.tool("mail.list", {}, 1)
    attachment = next((message["attachment_id"] for message in listed.get("messages") or [] if message.get("attachment_id")), None)
    document = client.tool("docs.read", {"id": attachment}, 2).get("document") or {}
    total = sheet_total(document.get("body") or "", executor=executor)
    text, model = _model(client, config, doc_prompt(config.instructions, document, total, listed.get("facts")), 4)
    if text is None:
        return {"state": model, "artifact": "", "figures": total}
    artifact = f"{text}\nfigures {total}\n{cites_line((document.get('id') or 'doc',))}"
    stored = client.state("SUCCEEDED", artifact)
    return {"state": stored.get("state"), "artifact": artifact, "figures": total, "model": model}


def _model(client, config: AgentConfig, messages, step_no: int) -> tuple[str | None, str]:
    """Returns (text, model). text is None when the run already stopped; model then holds the stored state."""
    model = config.models[0]
    errors = 0
    throttles = 0
    while True:
        result = client.complete(messages, model, step_no)
        status = result.get("status", 200)
        if status == 402:
            kind, artifact = stop_with_progress("STOPPED_BUDGET", "saved progress")
            client.state(kind, artifact)
            return None, kind
        if status in (503, 429):
            if status == 503:
                errors += 1
            else:
                throttles += 1
            action, nxt = after_provider_pressure(errors, throttles if status == 429 else 0, config.models, model)
            if action == "backoff":
                delay = backoff_delay(throttles, client.uniform)
                client.event("provider_backoff", {"model": model, "attempt": throttles, "delay_s": round(delay, 3)})
                client.sleep(delay)
            elif action == "fallback":
                client.event("provider_fallback", {"model": nxt, "simulated": bool(result.get("simulated"))})
                model = nxt
                errors = throttles = 0
            elif action == "escalate":
                client.state("ESCALATED", "provider")
                return None, "ESCALATED"
            continue
        if status == 409:
            raise RuntimeError(result.get("digest") or result.get("error") or "replay_miss")
        if status == 202:
            client.event("in_progress", {"step_no": step_no})
            client.state("FAILED", "in_progress")
            return None, "FAILED"
        return result.get("text") or "", model


def _escape(client, run_id: str, executor) -> dict:
    """Two local OS probes through the executor, one foreign id through the proxy, then the proxy used as a
    forward proxy (CONNECT to the internet, GET to loopback). The proxy refuses and logs both."""
    probes = (
        "print(open('/etc/shadow').read())",
        "import urllib.request; urllib.request.urlopen('http://example.com', timeout=3)",
    )
    outcomes = []
    for code in probes:
        try:
            executor(code, 5)
            outcomes.append("ran")
        except Exception as exc:
            outcomes.append(f"{type(exc).__name__}: {str(exc).strip().splitlines()[-1] if str(exc).strip() else ''}")
    foreign = client.tool("mail.read", {"ids": ["m-b-1"]}, 1)
    outcomes.append(f"mail.read m-b-1: {foreign.get('reason') or 'returned'}")
    for method, target in (("CONNECT", "example.com:443"), ("GET", "http://127.0.0.1:18894/")):
        try:
            answer = client.forward(method, target)
            outcomes.append(f"{method} {target}: {answer.get('status')} {answer.get('reason') or ''}".strip())
        except Exception as exc:
            outcomes.append(f"{method} {target}: {type(exc).__name__}")
    failures = sum(1 for item in outcomes[:2] if item != "ran") + (1 if foreign.get("denied") else 0)
    state = "ESCALATED" if failures >= 3 else "FAILED"
    stored = client.state(state, f"failures {failures}")
    return {"state": stored.get("state", state), "failures": failures, "outcomes": outcomes}


def _budget(client, config: AgentConfig, run_id: str) -> dict:
    """STOPPED_BUDGET only after the proxy answered 402 for the recorded brief call."""
    listed = client.tool("mail.list", {}, 1)
    messages = listed.get("messages") or []
    client.tool("mail.read", {"ids": [message["id"] for message in messages]}, 2)
    _text, state = _model(client, config, brief_prompt(config.instructions, messages, listed.get("facts")), 3)
    if state == "STOPPED_BUDGET":
        return {"state": state, "artifact": "saved progress"}
    client.state("FAILED", "budget not enforced")
    return {"state": "FAILED", "artifact": "budget not enforced"}


def _hang(client, run_id: str, executor) -> dict:
    attempts = 0
    while True:
        try:
            executor("import time; time.sleep(30)", 1)
        except Exception as exc:
            action = after_hang(attempts)
            attempts += 1
            client.event("hang_" + action, {"attempt": attempts, "error": type(exc).__name__})
            if action == "cancel":
                # The retry hung too: hand to a person.
                client.state("ESCALATED", "hang cancelled")
                return {"state": "ESCALATED", "artifact": "hang cancelled"}
            continue
        client.state("SUCCEEDED", "hang clear")
        return {"state": "SUCCEEDED"}


def _loop(client, config: AgentConfig, run_id: str) -> dict:
    messages = [{"role": "user", "content": "loop probe"}]
    digests = []
    text = ""
    for step in (1, 2, 3):
        digests.append("loop-probe")
        result = client.complete(messages, config.models[0], step)
        if result.get("status") == 202:
            client.event("in_progress", {"step_no": step})
            stored = client.state("FAILED", "in_progress")
            return {"state": stored.get("state"), "artifact": "in_progress", "pending": True}
        text = result.get("text") or text
        if is_loop(tuple(digests), 0):
            kind, artifact = stop_with_progress("STOPPED_LOOP", text or "saved progress")
            client.state(kind, artifact)
            return {"state": kind, "artifact": artifact}
    return {"state": "FAILED", "artifact": text}
