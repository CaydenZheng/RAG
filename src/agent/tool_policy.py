"""Exact-name host policy for native and MCP tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

ToolPolicyReason = Literal[
    "allowlist",
    "default_allow",
    "denylist",
    "not_allowlisted",
]


@dataclass(frozen=True)
class ToolPolicyDecision:
    """One deterministic policy result safe to include in audit metadata."""

    allowed: bool
    reason: ToolPolicyReason


@dataclass(frozen=True, init=False)
class ToolAccessPolicy:
    """Apply exact registered-name rules, with deny entries taking precedence."""

    allowlist: frozenset[str]
    denylist: frozenset[str]

    def __init__(
        self,
        allowlist: Iterable[str] = (),
        denylist: Iterable[str] = (),
    ) -> None:
        object.__setattr__(self, "allowlist", frozenset(allowlist))
        object.__setattr__(self, "denylist", frozenset(denylist))

    @property
    def configured(self) -> bool:
        """Return whether an operator supplied either policy list."""

        return bool(self.allowlist or self.denylist)

    def evaluate(self, tool_name: str) -> ToolPolicyDecision:
        """Evaluate one registered name without wildcard ambiguity."""

        if tool_name in self.denylist:
            return ToolPolicyDecision(False, "denylist")
        if self.allowlist:
            if tool_name in self.allowlist:
                return ToolPolicyDecision(True, "allowlist")
            return ToolPolicyDecision(False, "not_allowlisted")
        return ToolPolicyDecision(True, "default_allow")
