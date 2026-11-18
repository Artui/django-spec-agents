from __future__ import annotations


class RunNotParkedError(ValueError):
    """Raised by ``claim(..., resumes=...)`` when the thread is not waiting on the run it resumes.

    The thread is idle, failed, parked on another run, or its run's lease
    lapsed; no live run holds it (a live holder is answered as busy instead).
    Nothing was written; ``run_id`` names the run the claim meant to resume.
    A transport maps it to a gone or conflict answer, not a server error.
    """

    def __init__(self, message: str, *, run_id: str) -> None:
        super().__init__(message)
        self.run_id = run_id


__all__ = ["RunNotParkedError"]
