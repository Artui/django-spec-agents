from __future__ import annotations

import pytest

from django_spec_agents.headless.types.run_stop import RunStop
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from django_spec_agents.persistence.types.run_waiting import RunWaiting

_WAITING = RunWaiting(kind="approval", summary="delete_thing", tool_call_id="c1", destructive=True)


def test_a_parked_stop_carries_what_it_waits_on() -> None:
    stop = RunStop(thread_id="t1", run_id="r1", outcome=RunOutcome.WAITING, waiting=_WAITING)

    assert stop.waiting == _WAITING


@pytest.mark.parametrize(
    ("outcome", "waiting"),
    [(RunOutcome.WAITING, None), (RunOutcome.COMPLETED, _WAITING)],
    ids=["waiting-without-what", "what-without-waiting"],
)
def test_waiting_goes_with_the_waiting_outcome_only(
    outcome: RunOutcome, waiting: RunWaiting | None
) -> None:
    with pytest.raises(ValueError, match="exactly when"):
        RunStop(thread_id="t1", run_id="r1", outcome=outcome, waiting=waiting)
