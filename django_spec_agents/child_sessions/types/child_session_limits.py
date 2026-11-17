from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChildSessionLimits:
    """How far child sessions may go: their depth, how many run at once, and each run's deadline.

    ``timeout`` is each child run's deadline in seconds, required as a routine's is. A
    root thread is depth 0, so the default ``max_depth`` of 2 allows a child and a
    grandchild. The running limits count children that run or wait: per parent thread,
    and per user across every thread.
    """

    timeout: float
    max_depth: int = 2
    max_running_per_thread: int = 3
    max_running_per_user: int = 10

    def __post_init__(self) -> None:
        if not self.timeout > 0:
            raise ValueError(f"a child session needs a positive timeout, not {self.timeout!r}")
        for name in ("max_depth", "max_running_per_thread", "max_running_per_user"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


__all__ = ["ChildSessionLimits"]
