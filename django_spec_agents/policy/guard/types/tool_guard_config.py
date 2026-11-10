from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolGuardConfig:
    """Resolved policy for the server-side tool-approval gate."""

    enabled: bool = False
    """Whether the ``ToolGuard`` capability is composed into the agent."""

    exempt: frozenset[str] = field(default_factory=frozenset)
    """Tool names **never** gated, even if flagged destructive: the escape hatch
    for a mutation a project has decided is safe to auto-run."""

    require_approval: frozenset[str] = field(default_factory=frozenset)
    """Tool names **always** gated, even if not flagged destructive, for a read
    tool a project treats as sensitive. ``exempt`` wins on a name in both."""


__all__ = ["ToolGuardConfig"]
