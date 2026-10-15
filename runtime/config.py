from dataclasses import dataclass
from pathlib import Path

import yaml

KNOWN_TOOLS = frozenset({
    "mail.list",
    "mail.read",
    "docs.read",
    "py.exec",
    "memory.write",
    "draft.create",
})


class ConfigError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}")


class UnknownTool(ConfigError):
    def __init__(self, tool: str):
        self.tool = tool
        super().__init__("unknown_tool", tool)


class MissingTenant(ConfigError):
    def __init__(self):
        super().__init__("missing_tenant", "tenant_id")


@dataclass(frozen=True)
class Limits:
    max_steps: int
    max_tokens: int
    max_usd: float
    wall_s: int


@dataclass(frozen=True)
class AgentConfig:
    id: str
    tenant_id: str
    instructions: str
    tools: tuple[str, ...]
    cadence: str
    deadline: str
    models: tuple[str, ...]
    limits: Limits
    approval_policy: str
    egress_allowlist: tuple[str, ...]


def load_agent(path: Path) -> AgentConfig:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ConfigError("invalid_config", path.name)
    tenant = raw.get("tenant_id")
    if not isinstance(tenant, str) or tenant.strip() == "":
        raise MissingTenant()
    tools = raw.get("tools")
    if not isinstance(tools, list):
        raise ConfigError("invalid_config", "tools")
    for tool in tools:
        if tool not in KNOWN_TOOLS:
            raise UnknownTool(str(tool))
    schedule = raw.get("schedule") or {}
    limits = raw.get("limits") or {}
    models = raw.get("models")
    if not isinstance(models, list) or not models:
        raise ConfigError("invalid_config", "models")
    return AgentConfig(
        id=_text(raw, "id"),
        tenant_id=tenant,
        instructions=_text(raw, "instructions"),
        tools=tuple(tools),
        cadence=_text(schedule, "cadence"),
        deadline=_text(schedule, "deadline"),
        models=tuple(models),
        limits=Limits(
            max_steps=int(limits["max_steps"]),
            max_tokens=int(limits["max_tokens"]),
            max_usd=float(limits["max_usd"]),
            wall_s=int(limits["wall_s"]),
        ),
        approval_policy=_text(raw, "approval_policy"),
        egress_allowlist=tuple(raw.get("egress_allowlist") or ()),
    )


def load_agents(root: Path) -> tuple[AgentConfig, ...]:
    paths = sorted(root.glob("*/*.yaml"))
    return tuple(load_agent(path) for path in paths)


def _text(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise ConfigError("invalid_config", key)
    return value
