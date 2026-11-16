from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreapprovedTool:
    """A tool a routine's firings may call without parking, and whether it was gated when granted.

    `destructive` is the guard's `requires_approval` answer at grant time, kept as it was:
    a tool granted while ungated and gated later is not covered, and its call parks.
    """

    name: str
    destructive: bool
