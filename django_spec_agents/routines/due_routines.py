from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from django_spec_agents.headless.types.firing_state import FiringState
from django_spec_agents.routines.abstract_routine import AbstractRoutine
from django_spec_agents.routines.next_fire_at import next_fire_at
from django_spec_agents.routines.types.routine_policy import RoutinePolicy
from django_spec_agents.routines.types.routine_sweep import RoutineSweep
from django_spec_agents.routines.types.routine_t import RoutineT
from django_spec_agents.routines.types.schedule import Schedule
from django_spec_agents.routines.types.sweep_result import SweepResult

logger = logging.getLogger(__name__)

_LAPSED = (
    "The last firing (run {run_id}) lapsed: its worker died, its principal lost the right "
    "to run it, or its spec is gone."
)
_REFUSED = "Its principal may no longer run it."
_FIELDS = ["enabled", "paused_reason", "next_fire_at", "last_run_id", "updated_at"]


def _following(schedule: Schedule, zone: ZoneInfo, after: datetime, now: datetime) -> datetime:
    """The first firing after `now`, stepped from `after` so an interval keeps its cadence."""
    fire = after
    while fire <= now:
        fire = next_fire_at(schedule, zone, fire)
    return fire


def _pause(routine: AbstractRoutine, reason: str) -> None:
    routine.enabled = False
    routine.next_fire_at = None
    routine.paused_reason = reason


def _sweep_one(
    routine: RoutineT,
    sweep: RoutineSweep,
    policy: RoutinePolicy[RoutineT],
    now: datetime,
    result: SweepResult,
) -> list[Any]:
    """Handle one claimed routine and save it; the list of `result` it belongs in."""
    run_id = routine.last_run_id
    state = None if run_id is None else sweep.queue.state(run_id)
    due = routine.next_fire_at
    assert due is not None
    schedule, zone = routine.get_schedule(), routine.get_timezone()
    period_end = next_fire_at(schedule, zone, due)
    grace = period_end - due if sweep.grace is None else sweep.grace
    routine.next_fire_at = _following(schedule, zone, due, now)
    if state is FiringState.LAPSED:
        _pause(routine, _LAPSED.format(run_id=run_id))
        routine.last_run_id = None
        outcome = result.paused
    elif now - due > grace:
        logger.info("Routine %s missed its firing at %s, past its grace.", routine.pk, due)
        outcome = result.missed
    elif state is FiringState.PENDING:
        outcome = result.busy
    elif not policy.can_run(routine.principal, routine):
        _pause(routine, _REFUSED)
        outcome = result.paused
    else:
        params = {"routine_id": routine.pk}
        routine.last_run_id = sweep.queue.enqueue(sweep.spec_name, params, actor=routine.principal)
        outcome = result.fired
    routine.save(update_fields=_FIELDS)
    return outcome


def due_routines(
    model: type[RoutineT], sweep: RoutineSweep, *, now: datetime | None = None, limit: int = 100
) -> SweepResult:
    """Fire every enabled routine of `model` whose `next_fire_at` has come, at most `limit` of them.

    The due rows are claimed in one query with `select_for_update(skip_locked=True)`, so a
    second sweep running at the same time passes over them, and each is moved to its next
    firing in the same transaction that enqueues it: a sweep run twice fires once. The queue
    sends what was enqueued when that transaction commits. Run it on a database that honours
    `skip_locked`, such as PostgreSQL; SQLite ignores the lock.

    Each claimed routine, in this order:

    - its last firing `lapsed` (its worker died, or its principal or spec went away): the
      routine is paused, `enabled` off and `paused_reason` naming the run, and `last_run_id`
      is cleared so the routine fires again once turned back on;
    - it is later than its grace (`sweep.grace`, by default one schedule period): it is
      skipped, logged and counted as missed, so downtime runs a missed firing at most once;
    - its last firing is still `pending`: it is skipped;
    - the policy's `can_run` refuses its principal: it is paused with the reason;
    - otherwise the spec `sweep.spec_name` is enqueued as the principal with
      `{"routine_id": pk}`, and the run id it returns becomes `last_run_id`.

    However late the sweep, `next_fire_at` moves to the first firing after `now`, stepped
    from the one claimed. A routine whose handling raises is rolled back, logged and left
    for the next sweep, and the others go on.
    """
    clock = timezone.now() if now is None else now
    policy = model.routine_policy()
    assert policy is not None
    result = SweepResult()
    with transaction.atomic():
        claimed = (
            model._default_manager.select_for_update(skip_locked=True)
            .filter(enabled=True, next_fire_at__lte=clock)
            .order_by("next_fire_at")[:limit]
        )
        for routine in claimed:
            try:
                with transaction.atomic():
                    outcome = _sweep_one(routine, sweep, policy, clock, result)
            except Exception:
                logger.exception("Routine %s could not be swept.", routine.pk)
                outcome = result.failed
            outcome.append(routine.pk)
    return result
