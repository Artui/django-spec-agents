from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunClaim:
    """The answer to ``RunStore.claim``: whether the run won the thread.

    ``active_run_id`` is the run holding the thread afterwards: the claimant on
    a win, the run that kept it on a loss, so a transport's 409 can name it.
    """

    claimed: bool
    active_run_id: str


__all__ = ["RunClaim"]
