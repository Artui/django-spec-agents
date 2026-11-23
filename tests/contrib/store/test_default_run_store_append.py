from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.test.utils import CaptureQueriesContext

from django_spec_agents.contrib.store import default_run_store
from django_spec_agents.contrib.store.default_run_store import DefaultRunStore
from django_spec_agents.contrib.store.models import StoredRunEvent, StoredRunLog
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from tests.request_helpers import authed_request

pytestmark = pytest.mark.django_db(transaction=True)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_LEASE = timedelta(seconds=30)


class _Clock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture(params=["returning", "re-read"])
def store(
    request: pytest.FixtureRequest, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> DefaultRunStore:
    if request.param == "re-read":
        monkeypatch.setattr(default_run_store, "_returns_from_update", lambda _: False)
    return DefaultRunStore(clock=clock)


def _append(store: DefaultRunStore, events: list, run_id: str = "r1") -> int | None:
    return async_to_sync(store.append)(run_id, events, request=authed_request())


def _claim(store: DefaultRunStore) -> None:
    claim = async_to_sync(store.claim)("t1", "r1", request=authed_request(), lease=_LEASE)
    assert claim.claimed


def _seqs() -> list[int]:
    return list(StoredRunEvent.objects.order_by("seq").values_list("seq", flat=True))


def test_batches_number_on_from_one(store: DefaultRunStore) -> None:
    _claim(store)

    assert _append(store, [{"n": 1}, {"n": 2}]) == 2
    assert _append(store, [{"n": 3}]) == 3

    assert _seqs() == [1, 2, 3]
    assert StoredRunLog.objects.get().next_seq == 4


def test_an_empty_batch_answers_the_last_number(store: DefaultRunStore) -> None:
    _claim(store)
    assert _append(store, []) == 0
    _append(store, [{"n": 1}])

    assert _append(store, []) == 1
    assert _seqs() == [1]


def test_the_append_bumps_the_log_s_write_stamp(store: DefaultRunStore, clock: _Clock) -> None:
    _claim(store)
    clock.now = _T0 + timedelta(seconds=5)

    _append(store, [{"n": 1}])

    assert StoredRunLog.objects.get().updated_at == clock.now


def test_an_append_at_the_lease_s_last_instant_is_kept(
    store: DefaultRunStore, clock: _Clock
) -> None:
    _claim(store)
    clock.now = _T0 + _LEASE

    assert _append(store, [{"n": 1}]) == 1


def test_a_lapsed_owner_s_append_returns_none_and_writes_nothing(
    store: DefaultRunStore, clock: _Clock
) -> None:
    _claim(store)
    _append(store, [{"n": 1}])
    clock.now = _T0 + _LEASE + timedelta(seconds=1)

    assert _append(store, [{"n": 2}]) is None

    assert _seqs() == [1]
    log = StoredRunLog.objects.get()
    assert (log.next_seq, log.updated_at) == (2, _T0)


def test_a_finished_run_s_append_returns_none(store: DefaultRunStore) -> None:
    _claim(store)
    async_to_sync(store.finish)("t1", "r1", RunOutcome.COMPLETED, request=authed_request())

    assert _append(store, [{"n": 1}]) is None
    assert _seqs() == []


def test_an_unknown_run_s_append_returns_none(store: DefaultRunStore) -> None:
    assert _append(store, [{"n": 1}], run_id="nope") is None


def test_a_failed_insert_leaves_the_number_where_it_was(store: DefaultRunStore) -> None:
    _claim(store)
    _append(store, [{"n": 1}])

    with pytest.raises(TypeError):
        _append(store, [{"n": object()}])

    assert StoredRunLog.objects.get().next_seq == 2
    assert _append(store, [{"n": 2}]) == 2


def _statements(store: DefaultRunStore) -> list[str]:
    _claim(store)
    with CaptureQueriesContext(connection) as queries:
        assert _append(store, [{"n": 1}, {"n": 2}]) == 2
    controls = ("BEGIN", "COMMIT", "SAVEPOINT", "RELEASE", "ROLLBACK")
    return [q["sql"] for q in queries.captured_queries if not q["sql"].startswith(controls)]


def test_an_append_is_one_conditional_update_and_the_insert(clock: _Clock) -> None:
    statements = _statements(DefaultRunStore(clock=clock))

    assert [sql.split()[0] for sql in statements] == ["UPDATE", "INSERT"]
    update, insert = statements
    assert StoredRunLog._meta.db_table in update.split("WHERE")[0]
    assert '"lease_expires_at" >= ' in update
    assert "RETURNING" in update
    assert StoredRunEvent._meta.db_table in insert


def test_without_returning_the_append_re_reads_the_number(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(default_run_store, "_returns_from_update", lambda _: False)

    statements = _statements(DefaultRunStore(clock=clock))

    assert [sql.split()[0] for sql in statements] == ["UPDATE", "SELECT", "INSERT"]
    assert "RETURNING" not in statements[0]


@pytest.mark.skipif(connection.vendor != "postgresql", reason="SQLite serialises its writers")
def test_two_connections_appending_to_one_log_never_share_a_number() -> None:
    store = DefaultRunStore()
    async_to_sync(store.claim)("t1", "r1", request=authed_request(), lease=timedelta(minutes=5))
    start = threading.Barrier(2)

    def append(n: int) -> None:
        start.wait(timeout=5)
        for _ in range(10):
            _append(store, [{"n": n}, {"n": n}])
        connection.close()

    racers = [threading.Thread(target=append, args=(n,)) for n in (1, 2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    assert _seqs() == list(range(1, 41))
    assert StoredRunLog.objects.get().next_seq == 41


@pytest.mark.parametrize(
    ("vendor", "bulk_returning", "expected"),
    [
        ("postgresql", True, True),
        ("sqlite", True, True),
        ("sqlite", False, False),
        ("mysql", True, False),
        ("oracle", False, False),
    ],
)
def test_only_postgresql_and_a_recent_sqlite_take_update_returning(
    vendor: str, bulk_returning: bool, expected: bool
) -> None:
    features = SimpleNamespace(can_return_rows_from_bulk_insert=bulk_returning)
    database = SimpleNamespace(vendor=vendor, features=features)

    assert default_run_store._returns_from_update(database) is expected
