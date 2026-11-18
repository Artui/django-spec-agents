from __future__ import annotations


class RunNotParkedError(ValueError):
    """Raised by ``claim(..., resumes=...)`` when the thread is not waiting on the run it resumes.

    The thread is running (another resume may have won it), idle, failed,
    parked on another run, or its run's lease lapsed. Nothing was written;
    ``run_id`` names the run the claim meant to resume, and ``active_run_id``
    the thread's live holder, if any. A transport maps it to a conflict
    answer, not a server error.
    """

    def __init__(self, message: str, *, run_id: str, active_run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.active_run_id = active_run_id


__all__ = ["RunNotParkedError"]
