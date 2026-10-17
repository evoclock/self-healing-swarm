import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

OUTPUT_CAP = 512
KNOWN_MODELS = frozenset({"primary", "local"})

GATEWAY_BASE_URL = "https://api-gateway.merge.dev/v1/openai"
LOCAL_BASE_URL = "http://127.0.0.1:18894/v1"
GATEWAY_MODELS = {
    "primary": "zai/glm-5.3-flash",
    "local": "GLM-5.3-Flash-EXL3-TF-FNC7",
}


# Dollars per token, input or output, keyed by the real model id, for the pre-call estimate of a live call.
# Set at or above the recorded usage.cost per output token (0.00005844 for 231 output tokens), so it does not undercount.
# (input, output) price per token for each real model id.
PRICES = {"zai/glm-5.3-flash": (Decimal("0.00000026"), Decimal("0.00000026")), "GLM-5.3-Flash-EXL3-TF-FNC7": (Decimal(0), Decimal(0))}
PRICE_PER_TOKEN = {model: max(prices) for model, prices in PRICES.items()}


# Quota is per provider, not per model name: every gateway model shares the gateway's limits.
PROVIDER_OF = {"primary": "gateway", "local": "local"}


class UnknownPrice(Exception):
    def __init__(self, model: str):
        self.model = model
        super().__init__(f"no_price: {model}")


def prompt_tokens(messages, tools) -> int:
    """About 4 characters per token, rounded up. Used for the runner's own token limit."""
    return -(-len(json.dumps({"messages": messages, "tools": tools})) // 4)


def prompt_token_ceiling(messages, tools) -> int:
    """A hard upper bound on prompt tokens: the request's UTF-8 bytes (a token is at least one byte)."""
    return len(json.dumps({"messages": messages, "tools": tools}).encode())


def reserve_tokens(messages, tools, max_tokens: int = OUTPUT_CAP) -> int:
    return prompt_token_ceiling(messages, tools) + max_tokens


def reserve_cost(messages, tools, model: str, max_tokens: int = OUTPUT_CAP) -> Decimal:
    """What a live call reserves against the budget before it goes out: the prompt-token ceiling plus max_tokens
    (the call is sent with that cap), all at the model's higher per-token price. It can only over-reserve. The step is
    settled to the returned usage.cost when the call finishes. No price, no call."""
    model_id = GATEWAY_MODELS.get(model, model)
    if model_id not in PRICE_PER_TOKEN:
        raise UnknownPrice(model_id)
    return reserve_tokens(messages, tools, max_tokens) * PRICE_PER_TOKEN[model_id]


def settle_cost(model: str, tokens_in: int, tokens_out: int, reported: Decimal | None) -> Decimal:
    """The real charge: usage.cost when the provider reports it, else tokens at the listed prices."""
    if reported is not None:
        return reported
    price_in, price_out = PRICES.get(GATEWAY_MODELS.get(model, model), (Decimal(0), Decimal(0)))
    return tokens_in * price_in + tokens_out * price_out


class ReplayMiss(Exception):
    def __init__(self, digest: str):
        self.digest = digest
        super().__init__(f"replay_miss: {digest}")


_TRACE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")


def _trace_value(value: str, limit: int) -> str | None:
    if 1 <= len(value) <= limit and all(char in _TRACE_CHARS for char in value):
        return value
    return None


def merge_trace_headers(tenant_id: str, run_id: str, step_no: int, model: str, parent_id: str | None = None) -> dict:
    """Headers that opt one cloud call into Gateway Logs. Invalid values are omitted, not rewritten."""
    headers = {}
    trace_id = _trace_value(f"{tenant_id}.{run_id}", 128)
    thread_id = _trace_value(tenant_id, 128)
    turn_id = _trace_value(f"step-{int(step_no)}", 128)
    span = _trace_value(model, 64)
    if trace_id:
        headers["X-Merge-Trace-Id"] = trace_id
    if thread_id:
        headers["X-Merge-Thread-Id"] = thread_id
    if turn_id:
        headers["X-Merge-Turn-Id"] = turn_id
    if span:
        headers["X-Merge-Span-Name"] = span
    parent = _trace_value(parent_id or "", 128)
    if parent:
        headers["X-Merge-Parent-Span-Id"] = parent
    return headers


@dataclass(frozen=True)
class Completion:
    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal | None = None
    request_id: str | None = None

    def to_json(self) -> bytes:
        payload = {
            "text": self.text,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
        }
        if self.cost_usd is not None:
            payload["cost_usd"] = str(self.cost_usd)
        return json.dumps(payload, sort_keys=True).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "Completion":
        data = json.loads(raw)
        cost = data.get("cost_usd")
        return cls(
            data["text"],
            int(data["tokens_in"]),
            int(data["tokens_out"]),
            None if cost is None else Decimal(str(cost)),
        )


def step_digest(messages, tools) -> str:
    blob = json.dumps(
        {"messages": messages, "tools": tools},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(blob).hexdigest()


class Cassette:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, digest: str) -> bytes:
        path = self.root / digest
        if not path.is_file():
            raise ReplayMiss(digest)
        return path.read_bytes()

    def lookup(self, messages, tools) -> bytes:
        """The recording for these messages. Recordings made before quoting was applied to every line of a quoted
        block are found under the unquoted form of the same messages (see legacy_quoting). Nothing is re-recorded or
        rewritten: the same real response is served for the same request text, modulo the '> ' markers."""
        digest = step_digest(messages, tools)
        try:
            return self.get(digest)
        except ReplayMiss:
            legacy = legacy_quoting(messages)
            if legacy == messages:
                raise
            try:
                return self.get(step_digest(legacy, tools))
            except ReplayMiss:
                raise ReplayMiss(digest) from None

    def put(self, digest: str, body: bytes) -> None:
        (self.root / digest).write_bytes(body)


class Provider:
    def __init__(self, cassette: Cassette, mode: str, upstream=None):
        if mode not in ("record", "replay"):
            raise ValueError(mode)
        self.cassette = cassette
        self.mode = mode
        self.upstream = upstream

    def complete(self, messages, tools, model: str, temperature=None, max_tokens=None, project_id=None, trace=None) -> Completion:
        digest = step_digest(messages, tools)
        if self.mode == "replay":
            return Completion.from_json(self.cassette.lookup(messages, tools))
        if self.upstream is None:
            raise RuntimeError("record mode has no upstream")
        completion = self.upstream(
            messages,
            tools,
            model,
            temperature=0 if temperature is None else temperature,
            max_tokens=OUTPUT_CAP if max_tokens is None else max_tokens,
            project_id=project_id,
            trace=trace,
        )
        self.cassette.put(digest, completion.to_json())
        return completion


def gateway_model(name: str) -> str:
    return GATEWAY_MODELS.get(name, name)


def legacy_quoting(messages):
    """The document prompt as it was recorded: the document id line quoted, the body lines between it and the
    'py.exec total' line not. Only that exact shape is mapped; any other message is returned unchanged."""
    out = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str):
            lines = content.split("\n")
            if len(lines) >= 3 and lines[0].startswith("> ") and lines[-1].startswith("py.exec total ") \
                    and all(line.startswith("> ") for line in lines[1:-1]):
                body = [line[2:] for line in lines[1:-1]]
                message = {**message, "content": "\n".join([lines[0], *body, lines[-1]])}
        out.append(message)
    return out


def recorded_cost(cassette: Cassette, messages, tools) -> Decimal | None:
    try:
        raw = cassette.lookup(messages, tools)
    except ReplayMiss:
        return None
    return Completion.from_json(raw).cost_usd


def completion_from_payload(payload: dict) -> Completion:
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    usage = payload.get("usage") or {}
    cost = usage.get("cost")
    if cost is None:
        parsed = None
    elif isinstance(cost, Decimal):
        parsed = cost
    else:
        parsed = Decimal(str(cost))
    return Completion(
        message.get("content") or "",
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        parsed,
    )


def completion_from_chat(response) -> Completion:
    message = response.choices[0].message
    usage = response.usage
    cost = getattr(usage, "cost", None)
    if cost is None:
        extra = getattr(usage, "model_extra", None) or {}
        cost = extra.get("cost")
    return Completion(
        message.content or "",
        int(getattr(usage, "prompt_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or 0),
        None if cost is None else Decimal(str(cost)),
    )


def gateway_upstream(api_key: str, client=None, clients=None):
    opened = {}

    def client_for(model: str):
        if clients is not None and model in clients:
            return clients[model]
        if client is not None and model != "local":
            return client
        if model not in opened:
            from openai import OpenAI

            if model == "local":
                import os

                opened[model] = OpenAI(
                    base_url=os.environ.get("LOCAL_BASE_URL", LOCAL_BASE_URL),
                    api_key="local",
                )
            else:
                opened[model] = OpenAI(base_url=GATEWAY_BASE_URL, api_key=api_key)
        return opened[model]

    def complete(messages, tools, model: str, temperature=None, max_tokens=None, project_id=None, trace=None) -> Completion:
        kwargs = {"model": gateway_model(model), "messages": list(messages)}
        if tools:
            kwargs["tools"] = list(tools)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        headers = {}
        if model != "local":
            if project_id:
                headers["X-Project-Id"] = project_id
            if trace:
                headers.update(trace)
        if model == "local":
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        elif headers:
            kwargs["extra_headers"] = headers
        completions = client_for(model).chat.completions
        raw_create = getattr(getattr(completions, "with_raw_response", None), "create", None)
        if raw_create is None:
            return completion_from_chat(completions.create(**kwargs))
        raw = raw_create(**kwargs)
        parsed = completion_from_payload(json.loads(raw.text, parse_float=Decimal))
        request_id = _trace_value(raw.headers.get("x-request-id") or "", 128)
        if request_id is None:
            return parsed
        return Completion(parsed.text, parsed.tokens_in, parsed.tokens_out, parsed.cost_usd, request_id)

    return complete


def outbound_headers(agent_headers: dict, provider_key: str) -> dict:
    headers = {key: value for key, value in agent_headers.items() if key.lower() != "authorization"}
    headers["authorization"] = f"Bearer {provider_key}"
    return headers
