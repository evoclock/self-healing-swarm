from dataclasses import dataclass
from decimal import Decimal

from runtime.config import AgentConfig


@dataclass(frozen=True)
class RunPolicy:
    max_usd: Decimal
    tools: frozenset[str]

    @staticmethod
    def from_config(config: AgentConfig) -> "RunPolicy":
        return RunPolicy(Decimal(str(config.limits.max_usd)), frozenset(config.tools))

    def with_max(self, max_usd: Decimal) -> "RunPolicy":
        return RunPolicy(Decimal(max_usd), self.tools)
