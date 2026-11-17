from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HeadlessFiring:
    """The arguments [`run_headless`][django_spec_agents.headless.run_headless.run_headless] takes.

    They are those of
    [`HeadlessRunner.run_to_first_stop`][django_spec_agents.HeadlessRunner.run_to_first_stop]
    that cross a queue as JSON. ``capabilities`` cannot, so a queued run carries none.
    """

    agent_id: str
    prompt: str
    timeout: float
    thread_id: str | None = None
    parent: str | None = None


__all__ = ["HeadlessFiring"]
