import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

PRICE_PER_MILLION = {
    "primary": (Decimal("2.0"), Decimal("6.0")),
    "local": (Decimal("0"), Decimal("0")),
}
OUTPUT_CAP = 512

GATEWAY_BASE_URL = "https://api-gateway.merge.dev/v1/openai"
LOCAL_BASE_URL = "http://127.0.0.1:18894/v1"
GATEWAY_MODELS = {
    "primary": "zai/glm-5.3-flash",
    "local": "GLM-5.3-Flash-EXL3-TF-FNC7",
}


class ReplayMiss(Exception):
    def __init__(self, digest: str):
        self.digest = digest
        super().__init__(f"replay_miss: {digest}")


@dataclass(frozen=True)
class Completion:
    text: str
    tokens_in: int
    tokens_out: int

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "text": self.text,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
            },
            sort_keys=True,
        ).encode()

    @classmethod
    def from_json(cls, raw: bytes) -> "Completion":
        data = json.loads(raw)
        return cls(data["text"], int(data["tokens_in"]), int(data["tokens_out"]))


def estimate_usd(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    price_in, price_out = PRICE_PER_MILLION[model]
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


def server_estimate(model: str, messages) -> Decimal:
    """Input tokens from the serialized messages, output at the fixed cap. Payload estimates are ignored."""
    tokens_in = len(json.dumps(messages, sort_keys=True, separators=(",", ":"))) // 4
    return estimate_usd(model, tokens_in, OUTPUT_CAP)


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

    def put(self, digest: str, body: bytes) -> None:
        (self.root / digest).write_bytes(body)


class Provider:
    def __init__(self, cassette: Cassette, mode: str, upstream=None):
        if mode not in ("record", "replay"):
            raise ValueError(mode)
        self.cassette = cassette
        self.mode = mode
        self.upstream = upstream

    def complete(self, messages, tools, model: str, temperature=None, max_tokens=None) -> Completion:
        digest = step_digest(messages, tools)
        if self.mode == "replay":
            return Completion.from_json(self.cassette.get(digest))
        if self.upstream is None:
            raise RuntimeError("record mode has no upstream")
        completion = self.upstream(
            messages,
            tools,
            model,
            temperature=0 if temperature is None else temperature,
            max_tokens=OUTPUT_CAP if max_tokens is None else max_tokens,
        )
        self.cassette.put(digest, completion.to_json())
        return completion


def gateway_model(name: str) -> str:
    return GATEWAY_MODELS.get(name, name)


def completion_from_chat(response) -> Completion:
    message = response.choices[0].message
    usage = response.usage
    return Completion(
        message.content or "",
        int(getattr(usage, "prompt_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or 0),
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

    def complete(messages, tools, model: str, temperature=None, max_tokens=None) -> Completion:
        kwargs = {"model": gateway_model(model), "messages": list(messages)}
        if tools:
            kwargs["tools"] = list(tools)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if model == "local":
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        return completion_from_chat(client_for(model).chat.completions.create(**kwargs))

    return complete


def outbound_headers(agent_headers: dict, provider_key: str) -> dict:
    headers = {key: value for key, value in agent_headers.items() if key.lower() != "authorization"}
    headers["authorization"] = f"Bearer {provider_key}"
    return headers
