from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django_spec_agents.headless.types.firing_queue import FiringQueue


@dataclass(frozen=True)
class RoutineSweep:
    """What `run_due_routines` sweeps a routine model with: the queue, the spec it enqueues.

    `spec_name` is the name the queue's registry holds the firing spec under. `grace` is how
    late a missed firing may still run; `None` is one schedule period, read at the missed firing.
    """

    queue: FiringQueue
    spec_name: str
    grace: timedelta | None = None
