from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SweepResult:
    """What one sweep did with each routine it claimed, by primary key.

    `fired` were enqueued; `missed` were past their grace and skipped; `busy` were skipped because
    the last firing is still queued or running, or its thread still waits; `paused` were turned off;
    `failed` raised, were left as they were for the next sweep, and were logged.
    """

    fired: list[Any] = field(default_factory=list)
    missed: list[Any] = field(default_factory=list)
    busy: list[Any] = field(default_factory=list)
    paused: list[Any] = field(default_factory=list)
    failed: list[Any] = field(default_factory=list)
