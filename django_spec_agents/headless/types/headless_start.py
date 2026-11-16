from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HeadlessStart:
    """A headless run that has claimed its thread: where it runs, and as which run."""

    thread_id: str
    run_id: str


__all__ = ["HeadlessStart"]
