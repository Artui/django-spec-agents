from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RunLogEntry:
    """One numbered entry in a run's append-only log.

    ``seq`` counts from 1 within the run, so a reader that has seen nothing
    asks for entries ``after=0``. ``event`` is opaque JSON: the store never
    reads it, and a transport decides what it holds.
    """

    seq: int
    event: Any
    created_at: datetime


__all__ = ["RunLogEntry"]
