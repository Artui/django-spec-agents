from __future__ import annotations

from enum import StrEnum


class RunOutcome(StrEnum):
    """How a run ended, as its owner reports it to ``RunStore.finish``.

    | Outcome | Thread status | Reason |
    | --- | --- | --- |
    | ``completed`` | ``idle`` | none |
    | ``waiting`` | ``waiting`` | none |
    | ``cancelled`` | ``idle`` | none |
    | ``timed_out`` | ``idle`` | ``timed_out`` |
    | ``error`` | ``failed`` | ``error`` |
    """

    COMPLETED = "completed"
    WAITING = "waiting"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    ERROR = "error"


__all__ = ["RunOutcome"]
