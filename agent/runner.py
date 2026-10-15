import json
import time

from agent.loop import after_hang, after_provider_pressure, classify_artifact, is_loop, stop_with_progress
from agent.prompts import brief_prompt, doc_prompt, external_recipient, sheet_code
from runtime.config import AgentConfig


def execute(client, config: AgentConfig, run_id: str, scenario: str = "brief") -> dict:
    if scenario == "escape":
        return _escape(client, run_id)
    if scenario == "budget":
        return _budget(client, config, run_id)
    if scenario == "hang":
        return _hang(client, run_id)
    if scenario == "loop":
        return _loop(client, config, run_id)
    if scenario == "empty":
        outcome = classify_artifact("", (), frozenset())
        client.state(outcome, "")
        return {"state": outcome, "artifact": ""}
    if scenario == "doc":
        return _document(client, config, run_id)
    return _brief(client, config, run_id)


def _brief(client, config: AgentConfig, run_id: str) -> dict:
    listed = client.tool("mail.list", {}, 1)
    messages = listed.get("messages") or []
    client.tool("mail.read", {"ids": [message["id"] for message in messages]}, 2)
    if getattr(client, "pause_after", None) == 2:
        time.sleep(client.pause_after_seconds)
    text, model = _model(client, config, brief_prompt(config.instructions, messages), 3)
    recipient = external_recipient(messages, listed.get("domains") or [])
    if recipient:
        client.tool(
            "draft.create",
            {
                "draft_id": f"{run_id}:injection",
                "to": recipient,
                "subject": "flagged",
                "body": "Flagged request. Not sent.",
                "domains": listed.get("domains") or [],
            },
            4,
        )
    cited = tuple(message["id"] for message in messages)
    artifact = (text or "Brief") + "\n" + " ".join(cited)
    outcome = classify_artifact(artifact, cited, frozenset(cited))
    if outcome == "SUCCEEDED" and cited:
        client.tool(
            "memory.write",
            {
                "agent_id": config.id,
                "kind": "cursor",
                "body": cited[-1],
                "source_ids": [cited[-1]],
                "run_id": run_id,
            },
            5,
        )
    client.state(outcome, artifact)
    return {"state": outcome, "artifact": artifact, "model": model}


def _document(client, config: AgentConfig, run_id: str) -> dict:
    listed = client.tool("mail.list", {}, 1)
    attachment = next((message["attachment_id"] for message in listed.get("messages") or [] if message.get("attachment_id")), None)
    document = client.tool("docs.read", {"id": attachment}, 2).get("document") or {}
    code = sheet_code(document.get("body") or "")
    ran = client.tool("py.exec", {"code": code, "timeout": 5}, 3)
    total = (ran.get("stdout") or "").strip()
    text, model = _model(client, config, doc_prompt(config.instructions, document, total), 4)
    artifact = f"{text}\nfigures {total}\n{document.get('id', '')}"
    cited = (document.get("id") or "doc",)
    outcome = classify_artifact(artifact, cited, frozenset(cited))
    client.state(outcome, artifact)
    return {"state": outcome, "artifact": artifact, "figures": total, "model": model}


def _model(client, config: AgentConfig, messages, step_no: int) -> tuple[str, str]:
    model = config.models[0]
    errors = 0
    throttled = False
    while True:
        result = client.complete(messages, model, step_no, config.limits.max_usd)
        status = result.get("status", 200)
        if status == 402:
            kind, artifact = stop_with_progress("STOPPED_BUDGET", "saved progress")
            client.state(kind, artifact)
            return "", model
        if status in (503, 429):
            if status == 503:
                errors += 1
            throttled = throttled or status == 429
            action, nxt = after_provider_pressure(errors, throttled, config.models, model)
            if action == "fallback":
                client.event("provider_fallback", {"model": nxt, "simulated": True})
                model = nxt
            elif action == "escalate":
                client.state("ESCALATED", "provider")
                return "", model
            continue
        if status == 409:
            raise RuntimeError(result.get("digest") or "replay_miss")
        return result.get("text") or "", model


def _escape(client, run_id: str) -> dict:
    probes = (
        "print(open('/etc/passwd').read())",
        "import urllib.request; urllib.request.urlopen('http://example.com')",
        "print(open('/tenants/t-other/mail').read())",
    )
    denials = 0
    for index, code in enumerate(probes, start=1):
        result = client.tool("py.exec", {"code": code, "timeout": 2}, index)
        if result.get("denied"):
            denials += 1
    state = "ESCALATED" if denials >= 3 else "FAILED"
    client.state(state, f"denials {denials}")
    return {"state": state, "denials": denials}


def _budget(client, config: AgentConfig, run_id: str) -> dict:
    class Tight:
        models = config.models
        limits = type(config.limits)(config.limits.max_steps, config.limits.max_tokens, 0.00001, config.limits.wall_s)
        instructions = config.instructions
        id = config.id

    messages = [{"role": "user", "content": "budget probe"}]
    _model(client, Tight(), messages, 1)
    return {"state": "STOPPED_BUDGET", "artifact": "saved progress"}


def _hang(client, run_id: str) -> dict:
    attempts = 0
    while True:
        result = client.tool("py.exec", {"code": "import time; time.sleep(30)", "timeout": 1}, attempts + 1)
        if result.get("error"):
            action = after_hang(attempts)
            attempts += 1
            client.event("hang_" + action, {"attempt": attempts})
            if action == "cancel":
                client.state("FAILED", "hang cancelled")
                return {"state": "FAILED", "artifact": "hang cancelled"}
            continue
        client.state("SUCCEEDED", "hang clear")
        return {"state": "SUCCEEDED"}


def _loop(client, config: AgentConfig, run_id: str) -> dict:
    messages = [{"role": "user", "content": "loop probe"}]
    digests = []
    text = ""
    for step in (1, 2, 3):
        digests.append("loop-probe")
        result = client.complete(messages, config.models[0], step, config.limits.max_usd)
        text = result.get("text") or text
        if is_loop(tuple(digests), 0):
            kind, artifact = stop_with_progress("STOPPED_LOOP", text or "saved progress")
            client.state(kind, artifact)
            return {"state": kind, "artifact": artifact}
    return {"state": "FAILED", "artifact": text}
