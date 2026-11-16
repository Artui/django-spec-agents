from __future__ import annotations

from enum import StrEnum


class RunEndReason(StrEnum):
    """Why a thread's last run ended badly, kept beside its status.

    ``error`` (the run raised) and ``interrupted`` (its lease lapsed, as after a
    restart) come with ``failed``. ``timed_out`` comes with ``idle``: a timeout
    is a cancel on the wire, and the reason is kept server-side.
    """

    ERROR = "error"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


__all__ = ["RunEndReason"]
