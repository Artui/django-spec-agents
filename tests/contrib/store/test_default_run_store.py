from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.test.utils import CaptureQueriesContext

from django_spec_agents.contrib.store.default_run_store import DefaultRunStore
from django_spec_agents.contrib.store.models import StoredRunEvent, StoredRunLog, StoredThreadRun
from django_spec_agents.persistence.anonymous_operation_error import AnonymousOperationError
from django_spec_agents.persistence.identifier_too_long_error import IdentifierTooLongError
from django_spec_agents.persistence.run_id_reused_error import RunIdReusedError
from django_spec_agents.persistence.run_not_parked_error import RunNotParkedError
from django_spec_agents.persistence.types.resumable_run_store import ResumableRunStore
from django_spec_agents.persistence.types.run_claim import RunClaim
from django_spec_agents.persistence.types.run_end_reason import RunEndReason
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from django_spec_agents.persistence.types.run_store import RunStore
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus
from tests.contrib.store.rows import too_long
from tests.request_helpers import anonymous_request, authed_request

pytestmark = pytest.mark.django_db(transaction=True)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_LEASE = timedelta(seconds=30)
_WAITING = RunWaiting(kind="approval", summary="delete_thing", tool_call_id="c1", destructive=True)


class _Clock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(clock: _Clock) -> DefaultRunStore:
    return DefaultRunStore(clock=clock)


async def _claim(store: DefaultRunStore, run_id: str = "r1", pk: str = "7") -> RunClaim:
    return await store.claim("t1", run_id, request=authed_request(pk), lease=_LEASE)


def test_it_satisfies_the_protocol(store: DefaultRunStore) -> None:
    assert isinstance(store, RunStore)


async def test_a_claim_takes_the_thread(store: DefaultRunStore) -> None:
    assert await _claim(store) == RunClaim(claimed=True, active_run_id="r1")

    state = await store.get("t1", request=authed_request())

    assert state is not None
    assert (state.status, state.active_run_id, state.last_run_id) == (
        ThreadRunStatus.RUNNING,
        "r1",
        "r1",
    )
    assert state.lease_expires_at == _T0 + _LEASE


async def test_a_second_claim_loses_and_names_the_holder(store: DefaultRunStore) -> None:
    await _claim(store)

    assert await _claim(store, "r2") == RunClaim(claimed=False, active_run_id="r1")
    assert not await StoredRunLog.objects.filter(run_id="r2").aexists()


def test_the_claim_is_one_conditional_update(store: DefaultRunStore) -> None:
    async_to_sync(_claim)(store)
    async_to_sync(store.finish)("t1", "r1", RunOutcome.COMPLETED, request=authed_request())

    with CaptureQueriesContext(connection) as queries:
        assert async_to_sync(_claim)(store, "r2").claimed

    table = StoredThreadRun._meta.db_table
    updates = [q["sql"] for q in queries.captured_queries if q["sql"].startswith("UPDATE")]
    assert len(updates) == 1
    assert table in updates[0]
    assert '"active_run_id" IS NULL' in updates[0]
    assert '"lease_expires_at" <' in updates[0]


async def test_a_claim_over_a_lapsed_lease_wins(store: DefaultRunStore, clock: _Clock) -> None:
    await _claim(store)
    clock.now = _T0 + _LEASE + timedelta(seconds=1)

    assert (await _claim(store, "r2")).claimed


async def test_a_claim_at_the_lease_s_last_instant_loses(
    store: DefaultRunStore, clock: _Clock
) -> None:
    await _claim(store)
    clock.now = _T0 + _LEASE

    assert not (await _claim(store, "r2")).claimed


async def test_threads_and_owners_are_claimed_apart(store: DefaultRunStore) -> None:
    await _claim(store)

    assert (await store.claim("t2", "r2", request=authed_request(), lease=_LEASE)).claimed
    assert (await _claim(store, "r3", pk="8")).claimed
    assert await store.get("t1", request=authed_request("9")) is None


async def test_a_lapsed_lease_reads_as_interrupted(store: DefaultRunStore, clock: _Clock) -> None:
    await _claim(store)
    clock.now = _T0 + _LEASE + timedelta(seconds=1)

    state = await store.get("t1", request=authed_request())

    assert state is not None
    assert (state.status, state.reason) == (ThreadRunStatus.FAILED, RunEndReason.INTERRUPTED)
    assert (state.active_run_id, state.last_run_id) == (None, "r1")


async def test_renew_extends_only_the_live_holder(store: DefaultRunStore, clock: _Clock) -> None:
    await _claim(store)
    clock.now = _T0 + _LEASE

    assert await store.renew("t1", "r1", request=authed_request(), lease=_LEASE)
    assert not await store.renew("t1", "r2", request=authed_request(), lease=_LEASE)

    log = await StoredRunLog.objects.aget(run_id="r1")
    state = await store.get("t1", request=authed_request())
    assert state is not None
    assert state.lease_expires_at == _T0 + 2 * _LEASE
    assert log.updated_at == clock.now


async def test_an_owner_presumed_dead_cannot_touch_its_successor(
    store: DefaultRunStore, clock: _Clock
) -> None:
    await _claim(store)
    clock.now = _T0 + _LEASE + timedelta(seconds=1)
    await _claim(store, "r2")

    assert not await store.renew("t1", "r1", request=authed_request(), lease=_LEASE)
    assert await store.append("r1", [{"late": True}], request=authed_request()) is None
    assert not await store.finish("t1", "r1", RunOutcome.ERROR, request=authed_request())

    state = await store.get("t1", request=authed_request())
    assert state is not None
    assert (state.status, state.active_run_id) == (ThreadRunStatus.RUNNING, "r2")
    assert not await StoredRunEvent.objects.aexists()


@pytest.mark.parametrize(
    ("outcome", "status", "reason"),
    [
        (RunOutcome.COMPLETED, ThreadRunStatus.IDLE, None),
        (RunOutcome.CANCELLED, ThreadRunStatus.IDLE, None),
        (RunOutcome.TIMED_OUT, ThreadRunStatus.IDLE, RunEndReason.TIMED_OUT),
        (RunOutcome.ERROR, ThreadRunStatus.FAILED, RunEndReason.ERROR),
    ],
)
async def test_finish_records_how_the_run_ended(
    store: DefaultRunStore,
    clock: _Clock,
    outcome: RunOutcome,
    status: ThreadRunStatus,
    reason: RunEndReason | None,
) -> None:
    await _claim(store)
    clock.now = _T0 + timedelta(seconds=5)

    assert await store.finish("t1", "r1", outcome, request=authed_request())

    state = await store.get("t1", request=authed_request())
    assert state is not None
    assert (state.status, state.reason, state.active_run_id, state.last_run_id) == (
        status,
        reason,
        None,
        "r1",
    )
    assert (await StoredRunLog.objects.aget(run_id="r1")).ended_at == clock.now


async def test_a_parked_run_keeps_what_it_waits_on(store: DefaultRunStore) -> None:
    await _claim(store)

    await store.finish("t1", "r1", RunOutcome.WAITING, request=authed_request(), waiting=_WAITING)

    state = await store.get("t1", request=authed_request())
    assert state is not None
    assert (state.status, state.waiting, state.last_run_id) == (
        ThreadRunStatus.WAITING,
        _WAITING,
        "r1",
    )


async def test_a_waiting_row_written_before_kind_was_typed_still_loads(
    store: DefaultRunStore,
) -> None:
    await _claim(store)
    await store.finish("t1", "r1", RunOutcome.WAITING, request=authed_request(), waiting=_WAITING)
    raw = {"kind": "approval", "summary": "drop", "tool_call_id": "c9", "destructive": False}
    await StoredThreadRun.objects.filter(thread_id="t1").aupdate(waiting=raw)

    state = await store.get("t1", request=authed_request())

    assert state is not None
    assert state.waiting == RunWaiting(**raw)


async def test_the_next_claim_clears_the_park(store: DefaultRunStore) -> None:
    await _claim(store)
    await store.finish("t1", "r1", RunOutcome.WAITING, request=authed_request(), waiting=_WAITING)

    await _claim(store, "r2")

    state = await store.get("t1", request=authed_request())
    assert state is not None
    assert (state.status, state.waiting) == (ThreadRunStatus.RUNNING, None)


@pytest.mark.parametrize(
    ("outcome", "waiting"),
    [(RunOutcome.WAITING, None), (RunOutcome.COMPLETED, _WAITING)],
    ids=["waiting-without-what", "what-without-waiting"],
)
async def test_waiting_goes_with_the_waiting_outcome_only(
    store: DefaultRunStore, outcome: RunOutcome, waiting: RunWaiting | None
) -> None:
    with pytest.raises(ValueError, match="exactly when"):
        await store.finish("t1", "r1", outcome, request=authed_request(), waiting=waiting)


async def test_the_log_numbers_batches_on_from_one(store: DefaultRunStore) -> None:
    await _claim(store)

    assert await store.append("r1", [{"n": 1}, {"n": 2}], request=authed_request()) == 2
    assert await store.append("r1", [{"n": 3}], request=authed_request()) == 3

    entries = await store.read("r1", request=authed_request())
    assert [(e.seq, e.event) for e in entries] == [(1, {"n": 1}), (2, {"n": 2}), (3, {"n": 3})]
    assert entries[0].created_at == _T0


async def test_read_pages_after_a_number(store: DefaultRunStore) -> None:
    await _claim(store)
    await store.append("r1", [{"n": n} for n in range(1, 6)], request=authed_request())

    page = await store.read("r1", request=authed_request(), after=2, limit=2)

    assert [e.seq for e in page] == [3, 4]


async def test_an_unknown_run_appends_nothing(store: DefaultRunStore) -> None:
    assert await store.append("nope", [{"n": 1}], request=authed_request()) is None


async def test_a_finished_run_appends_nothing(store: DefaultRunStore) -> None:
    await _claim(store)
    await store.finish("t1", "r1", RunOutcome.COMPLETED, request=authed_request())

    assert await store.append("r1", [{"n": 1}], request=authed_request()) is None


async def test_another_owner_reads_no_log(store: DefaultRunStore) -> None:
    await _claim(store)
    await store.append("r1", [{"n": 1}], request=authed_request())

    assert await store.read("r1", request=authed_request("8")) == []
    assert await store.append("r1", [{"n": 2}], request=authed_request("8")) is None


async def test_a_cancel_is_recorded_for_the_live_run(store: DefaultRunStore) -> None:
    assert not await store.request_cancel("t1", request=authed_request())
    await _claim(store)

    assert await store.request_cancel("t1", request=authed_request())

    state = await store.get("t1", request=authed_request())
    assert state is not None
    assert state.cancel_requested


async def test_no_cancel_after_the_run_ends(store: DefaultRunStore, clock: _Clock) -> None:
    await _claim(store)
    clock.now = _T0 + _LEASE + timedelta(seconds=1)

    assert not await store.request_cancel("t1", request=authed_request())


async def test_mark_read_stamps_a_known_thread(store: DefaultRunStore, clock: _Clock) -> None:
    await store.mark_read("t1", request=authed_request())
    await _claim(store)
    clock.now = _T0 + timedelta(minutes=1)

    await store.mark_read("t1", request=authed_request())

    state = await store.get("t1", request=authed_request())
    assert state is not None
    assert state.last_read_at == clock.now


async def test_get_many_reads_only_the_owner_s_threads(store: DefaultRunStore) -> None:
    await _claim(store)
    await store.claim("t2", "r2", request=authed_request("8"), lease=_LEASE)

    states = await store.get_many(["t1", "t2", "t3"], request=authed_request())

    assert list(states) == ["t1"]


async def test_prune_drops_logs_a_window_after_their_run(clock: _Clock) -> None:
    store = DefaultRunStore(clock=clock, log_retention=timedelta(hours=1))
    await _claim(store)
    await store.append("r1", [{"n": 1}], request=authed_request())
    await store.finish("t1", "r1", RunOutcome.COMPLETED, request=authed_request())

    clock.now = _T0 + timedelta(hours=1)
    assert await store.prune() == 0
    clock.now = _T0 + timedelta(hours=1, seconds=1)
    assert await store.prune() == 1

    assert not await StoredRunLog.objects.aexists()
    assert not await StoredRunEvent.objects.aexists()


async def test_the_default_window_is_a_day(clock: _Clock, store: DefaultRunStore) -> None:
    await _claim(store)
    await store.finish("t1", "r1", RunOutcome.COMPLETED, request=authed_request())

    clock.now = _T0 + timedelta(days=1)
    assert await store.prune() == 0
    clock.now = _T0 + timedelta(days=1, seconds=1)
    assert await store.prune() == 1


async def test_prune_drops_an_abandoned_run_from_its_last_write(clock: _Clock) -> None:
    store = DefaultRunStore(clock=clock, log_retention=timedelta(hours=1))
    await _claim(store)

    clock.now = _T0 + timedelta(hours=1, seconds=1)

    assert await store.prune() == 1


async def test_prune_spares_a_run_under_a_live_lease(clock: _Clock) -> None:
    store = DefaultRunStore(clock=clock, log_retention=timedelta(hours=1))
    await store.claim("t1", "r1", request=authed_request(), lease=timedelta(hours=3))

    clock.now = _T0 + timedelta(hours=2)

    assert await store.prune() == 0


async def test_prune_spares_another_owner_s_live_run_of_the_same_id(clock: _Clock) -> None:
    store = DefaultRunStore(clock=clock, log_retention=timedelta(hours=1))
    await store.claim("t1", "r1", request=authed_request(), lease=timedelta(minutes=1))
    await store.claim("t1", "r1", request=authed_request("8"), lease=timedelta(hours=3))

    clock.now = _T0 + timedelta(hours=2)

    assert await store.prune() == 1
    assert await StoredRunLog.objects.filter(owner_id="8").aexists()


async def test_an_anonymous_request_is_refused_by_default(store: DefaultRunStore) -> None:
    with pytest.raises(AnonymousOperationError):
        await store.claim("t1", "r1", request=anonymous_request(), lease=_LEASE)


async def test_an_over_long_id_is_refused_before_any_write(store: DefaultRunStore) -> None:
    with pytest.raises(IdentifierTooLongError):
        await _claim(store, too_long(StoredThreadRun, "active_run_id"))

    assert not await StoredThreadRun.objects.aexists()


def test_check_identifiers_names_the_thread(store: DefaultRunStore) -> None:
    with pytest.raises(IdentifierTooLongError) as raised:
        store.check_identifiers(thread_id=too_long(StoredThreadRun, "thread_id"), run_id="r1")

    assert raised.value.field == "thread_id"


async def test_a_holder_whose_lease_lapsed_cannot_revive_it(
    store: DefaultRunStore, clock: _Clock
) -> None:
    await _claim(store)
    clock.now = _T0 + _LEASE + timedelta(seconds=1)

    assert not await store.renew("t1", "r1", request=authed_request(), lease=_LEASE)
    assert not await store.finish("t1", "r1", RunOutcome.COMPLETED, request=authed_request())


def test_check_identifiers_takes_either_id_alone(store: DefaultRunStore) -> None:
    store.check_identifiers(thread_id="t1")

    with pytest.raises(IdentifierTooLongError):
        store.check_identifiers(run_id=too_long(StoredThreadRun, "active_run_id"))


async def test_a_used_run_id_is_refused_and_the_thread_left_as_it_was(
    store: DefaultRunStore,
) -> None:
    await _claim(store)
    await store.finish("t1", "r1", RunOutcome.WAITING, request=authed_request(), waiting=_WAITING)

    with pytest.raises(RunIdReusedError) as raised:
        await _claim(store)

    assert raised.value.run_id == "r1"
    state = await store.get("t1", request=authed_request())
    assert state is not None
    assert (state.status, state.waiting, state.active_run_id) == (
        ThreadRunStatus.WAITING,
        _WAITING,
        None,
    )


async def test_a_used_run_id_is_refused_on_another_thread(store: DefaultRunStore) -> None:
    await _claim(store)

    with pytest.raises(RunIdReusedError):
        await store.claim("t2", "r1", request=authed_request(), lease=_LEASE)

    assert await store.get("t2", request=authed_request()) is None


async def test_another_owner_may_use_the_same_run_id(store: DefaultRunStore) -> None:
    await _claim(store)

    assert (await _claim(store, pk="8")).claimed


async def test_prune_drops_an_ended_log_while_its_owner_runs_another_thread(
    clock: _Clock,
) -> None:
    store = DefaultRunStore(clock=clock, log_retention=timedelta(hours=1))
    await _claim(store)
    await store.finish("t1", "r1", RunOutcome.COMPLETED, request=authed_request())
    await store.claim("t2", "r2", request=authed_request(), lease=timedelta(hours=3))

    clock.now = _T0 + timedelta(hours=2)

    assert await store.prune() == 1
    assert not await StoredRunLog.objects.filter(run_id="r1").aexists()


async def test_a_claim_over_a_lapsed_lease_does_not_inherit_its_cancel(
    store: DefaultRunStore, clock: _Clock
) -> None:
    await _claim(store)
    await store.request_cancel("t1", request=authed_request())
    clock.now = _T0 + _LEASE + timedelta(seconds=1)

    await _claim(store, "r2")

    state = await store.get("t1", request=authed_request())
    assert state is not None
    assert not state.cancel_requested


async def _parked(store: DefaultRunStore, run_id: str = "r1") -> None:
    await _claim(store, run_id)
    await store.finish("t1", run_id, RunOutcome.WAITING, request=authed_request(), waiting=_WAITING)


async def _resume(store: DefaultRunStore, run_id: str, resumes: str = "r1") -> RunClaim:
    return await store.claim("t1", run_id, request=authed_request(), lease=_LEASE, resumes=resumes)


async def _status(store: DefaultRunStore) -> tuple:
    state = await store.get("t1", request=authed_request())
    assert state is not None
    return (state.status, state.reason, state.waiting, state.active_run_id, state.last_run_id)


def test_it_is_a_resumable_run_store(store: DefaultRunStore) -> None:
    assert isinstance(store, ResumableRunStore)


class _StoreOfTheLastRelease:
    async def claim(self, thread_id, run_id, *, request, lease): ...  # noqa: ANN001, ANN201
    async def renew(self, thread_id, run_id, *, request, lease): ...  # noqa: ANN001, ANN201
    async def finish(self, thread_id, run_id, outcome, *, request, waiting=None): ...  # noqa: ANN001, ANN201
    async def append(self, run_id, events, *, request): ...  # noqa: ANN001, ANN201
    async def read(self, run_id, *, request, after=0, limit=None): ...  # noqa: ANN001, ANN201
    async def get(self, thread_id, *, request): ...  # noqa: ANN001, ANN201
    async def get_many(self, thread_ids, *, request): ...  # noqa: ANN001, ANN201
    async def request_cancel(self, thread_id, *, request): ...  # noqa: ANN001, ANN201
    async def mark_read(self, thread_id, *, request): ...  # noqa: ANN001, ANN201
    async def prune(self): ...  # noqa: ANN201


def test_a_store_without_release_is_still_a_run_store() -> None:
    assert isinstance(_StoreOfTheLastRelease(), RunStore)
    assert not isinstance(_StoreOfTheLastRelease(), ResumableRunStore)


async def test_a_resume_claims_the_thread_parked_on_its_run(store: DefaultRunStore) -> None:
    await _parked(store)

    assert await _resume(store, "r2") == RunClaim(claimed=True, active_run_id="r2")
    assert await _status(store) == (ThreadRunStatus.RUNNING, None, None, "r2", "r2")


async def test_of_two_resumes_of_one_park_the_second_is_busy(store: DefaultRunStore) -> None:
    await _parked(store)

    assert (await _resume(store, "r2")).claimed
    assert await _resume(store, "r3") == RunClaim(claimed=False, active_run_id="r2")
    assert not await StoredRunLog.objects.filter(run_id="r3").aexists()


async def test_a_resume_after_the_first_resume_ended_is_not_parked(
    store: DefaultRunStore,
) -> None:
    await _parked(store)
    await _resume(store, "r2")
    await store.finish("t1", "r2", RunOutcome.COMPLETED, request=authed_request())

    with pytest.raises(RunNotParkedError) as raised:
        await _resume(store, "r3")

    assert raised.value.run_id == "r1"
    assert await _status(store) == (ThreadRunStatus.IDLE, None, None, None, "r2")


async def test_a_resume_of_an_idle_thread_is_refused(store: DefaultRunStore) -> None:
    await _claim(store)
    await store.finish("t1", "r1", RunOutcome.COMPLETED, request=authed_request())

    with pytest.raises(RunNotParkedError):
        await _resume(store, "r2")

    assert await _status(store) == (ThreadRunStatus.IDLE, None, None, None, "r1")


async def test_a_resume_of_a_failed_thread_whose_last_run_is_the_one_resumed_is_refused(
    store: DefaultRunStore,
) -> None:
    await _claim(store)
    await store.finish("t1", "r1", RunOutcome.ERROR, request=authed_request())

    with pytest.raises(RunNotParkedError):
        await _resume(store, "r2")


async def test_a_resume_of_a_running_thread_is_busy(store: DefaultRunStore) -> None:
    await _claim(store)

    assert await _resume(store, "r2") == RunClaim(claimed=False, active_run_id="r1")


async def test_a_resume_of_a_thread_parked_on_another_run_is_refused(
    store: DefaultRunStore,
) -> None:
    await _parked(store)

    with pytest.raises(RunNotParkedError):
        await _resume(store, "r2", resumes="r0")

    assert await _status(store) == (ThreadRunStatus.WAITING, None, _WAITING, None, "r1")


async def test_a_resume_over_a_lapsed_lease_is_refused(
    store: DefaultRunStore, clock: _Clock
) -> None:
    await _claim(store)
    clock.now = _T0 + _LEASE + timedelta(seconds=1)

    with pytest.raises(RunNotParkedError):
        await _resume(store, "r2")


async def test_a_resume_of_an_unknown_thread_leaves_it_unknown(store: DefaultRunStore) -> None:
    with pytest.raises(RunNotParkedError):
        await _resume(store, "r2")

    assert await store.get("t1", request=authed_request()) is None


def test_the_resume_is_one_conditional_update(store: DefaultRunStore) -> None:
    async_to_sync(_parked)(store)

    with CaptureQueriesContext(connection) as queries:
        assert async_to_sync(_resume)(store, "r2").claimed

    updates = [q["sql"] for q in queries.captured_queries if q["sql"].startswith("UPDATE")]
    assert len(updates) == 1
    assert StoredThreadRun._meta.db_table in updates[0]
    assert '"active_run_id" IS NULL' in updates[0]
    assert '"status" = ' in updates[0].split("WHERE")[1]
    assert '"last_run_id" = ' in updates[0].split("WHERE")[1]


@pytest.mark.skipif(connection.vendor != "postgresql", reason="SQLite serialises its writers")
def test_two_connections_resuming_one_park_one_wins() -> None:
    store = DefaultRunStore()
    async_to_sync(_parked)(store)
    start = threading.Barrier(2)
    won: dict[str, bool] = {}

    def resume(run_id: str) -> None:
        start.wait(timeout=5)
        won[run_id] = async_to_sync(_resume)(store, run_id).claimed
        connection.close()

    racers = [threading.Thread(target=resume, args=(run_id,)) for run_id in ("r2", "r3")]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    assert sorted(won.values()) == [False, True]


async def test_release_puts_a_resumed_park_back(store: DefaultRunStore) -> None:
    await _parked(store)
    await _resume(store, "r2")

    assert await store.release("t1", "r2", request=authed_request())

    assert await _status(store) == (ThreadRunStatus.WAITING, None, _WAITING, None, "r1")
    assert not await StoredRunLog.objects.filter(run_id="r2").aexists()
    assert (await _resume(store, "r3")).claimed


async def test_release_puts_an_idle_thread_back(store: DefaultRunStore) -> None:
    await _claim(store)
    await store.finish("t1", "r1", RunOutcome.COMPLETED, request=authed_request())
    await _claim(store, "r2")

    assert await store.release("t1", "r2", request=authed_request())

    assert await _status(store) == (ThreadRunStatus.IDLE, None, None, None, "r1")


async def test_release_puts_a_failed_thread_back_with_its_reason(store: DefaultRunStore) -> None:
    await _claim(store)
    await store.finish("t1", "r1", RunOutcome.TIMED_OUT, request=authed_request())
    await _claim(store, "r2")

    assert await store.release("t1", "r2", request=authed_request())

    assert await _status(store) == (ThreadRunStatus.IDLE, RunEndReason.TIMED_OUT, None, None, "r1")


async def test_release_over_a_lapsed_lease_reads_interrupted(
    store: DefaultRunStore, clock: _Clock
) -> None:
    await _claim(store)
    clock.now = _T0 + _LEASE + timedelta(seconds=1)
    await _claim(store, "r2")

    assert await store.release("t1", "r2", request=authed_request())

    assert await _status(store) == (
        ThreadRunStatus.FAILED,
        RunEndReason.INTERRUPTED,
        None,
        None,
        "r1",
    )


async def test_release_of_a_first_claim_forgets_the_thread(store: DefaultRunStore) -> None:
    await _claim(store)

    assert await store.release("t1", "r1", request=authed_request())

    assert await store.get("t1", request=authed_request()) is None
    assert (await _claim(store)).claimed


async def test_release_clears_the_cancel_and_drops_the_log(store: DefaultRunStore) -> None:
    await _parked(store)
    await _resume(store, "r2")
    await store.append("r2", [{"type": "x"}], request=authed_request())
    await store.request_cancel("t1", request=authed_request())

    assert await store.release("t1", "r2", request=authed_request())

    state = await store.get("t1", request=authed_request())
    assert state is not None and not state.cancel_requested
    assert not await StoredRunEvent.objects.aexists()
    row = await StoredThreadRun.objects.aget()
    assert (row.prior_status, row.prior_reason, row.prior_waiting, row.prior_last_run_id) == (
        None,
        None,
        None,
        None,
    )


async def test_release_by_a_run_that_does_not_hold_the_claim_changes_nothing(
    store: DefaultRunStore,
) -> None:
    await _parked(store)
    await _resume(store, "r2")

    assert not await store.release("t1", "r1", request=authed_request())
    assert not await store.release("t1", "r2", request=authed_request("8"))

    assert await _status(store) == (ThreadRunStatus.RUNNING, None, None, "r2", "r2")
    assert await StoredRunLog.objects.filter(run_id="r2").aexists()


async def test_release_after_finish_changes_nothing(store: DefaultRunStore) -> None:
    await _parked(store)
    await _resume(store, "r2")
    await store.finish("t1", "r2", RunOutcome.COMPLETED, request=authed_request())

    assert not await store.release("t1", "r2", request=authed_request())

    assert await _status(store) == (ThreadRunStatus.IDLE, None, None, None, "r2")


async def test_release_of_a_claim_taken_before_the_prior_columns_changes_nothing(
    store: DefaultRunStore,
) -> None:
    await _claim(store)
    await StoredThreadRun.objects.aupdate(prior_status=None)

    assert not await store.release("t1", "r1", request=authed_request())

    assert await _status(store) == (ThreadRunStatus.RUNNING, None, None, "r1", "r1")
