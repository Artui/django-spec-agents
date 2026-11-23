from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.test.utils import CaptureQueriesContext

from django_spec_agents.contrib.store import default_run_store
from django_spec_agents.contrib.store.default_run_store import DefaultRunStore
from django_spec_agents.contrib.store.models import StoredRunLog
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from tests.request_helpers import authed_request

pytestmark = pytest.mark.django_db(transaction=True)

_LEASE = timedelta(minutes=5)
_LONG = timedelta(seconds=5)


@pytest.fixture
def store() -> DefaultRunStore:
    return DefaultRunStore(poll_interval=(0.01, 0.02))


async def _claim(store: DefaultRunStore) -> None:
    assert (await store.claim("t1", "r1", request=authed_request(), lease=_LEASE)).claimed


async def _wait(store: DefaultRunStore, after: int = 0, timeout: timedelta = _LONG) -> bool:
    return await store.wait("r1", after=after, timeout=timeout, request=authed_request())


async def _timed(waiter: asyncio.Task[bool]) -> tuple[bool, float]:
    started = time.monotonic()
    answer = await waiter
    return answer, time.monotonic() - started


async def test_wait_returns_when_an_entry_is_appended(store: DefaultRunStore) -> None:
    await _claim(store)
    waiter = asyncio.ensure_future(_wait(store))
    await asyncio.sleep(0.05)
    assert not waiter.done()

    await store.append("r1", [{"n": 1}], request=authed_request())

    answer, elapsed = await _timed(waiter)
    assert answer is True
    assert elapsed < 1


async def test_wait_returns_when_the_run_finishes(store: DefaultRunStore) -> None:
    await _claim(store)
    waiter = asyncio.ensure_future(_wait(store))
    await asyncio.sleep(0.05)
    assert not waiter.done()

    await store.finish("t1", "r1", RunOutcome.COMPLETED, request=authed_request())

    answer, elapsed = await _timed(waiter)
    assert answer is True
    assert elapsed < 1


async def test_wait_answers_false_at_its_timeout(store: DefaultRunStore) -> None:
    await _claim(store)
    await store.append("r1", [{"n": 1}], request=authed_request())
    started = time.monotonic()

    assert await _wait(store, after=1, timeout=timedelta(seconds=0.2)) is False
    assert 0.2 <= time.monotonic() - started < 1


async def test_wait_answers_at_once_for_an_entry_already_past_after(
    store: DefaultRunStore,
) -> None:
    await _claim(store)
    await store.append("r1", [{"n": 1}, {"n": 2}], request=authed_request())

    assert await _wait(store, after=1, timeout=timedelta(0)) is True
    assert await _wait(store, after=2, timeout=timedelta(0)) is False


async def test_wait_answers_at_once_for_an_ended_run(store: DefaultRunStore) -> None:
    await _claim(store)
    await store.append("r1", [{"n": 1}], request=authed_request())
    await store.finish("t1", "r1", RunOutcome.COMPLETED, request=authed_request())

    assert await _wait(store, after=1, timeout=timedelta(0)) is True


async def test_wait_answers_at_once_for_a_run_with_no_log(store: DefaultRunStore) -> None:
    assert await _wait(store, timeout=timedelta(0)) is True


async def test_another_owner_waits_on_no_log(store: DefaultRunStore) -> None:
    await _claim(store)
    other = authed_request("8")

    assert await store.wait("r1", after=0, timeout=timedelta(0), request=other) is True


async def test_wait_does_not_end_a_run_whose_owner_died(store: DefaultRunStore) -> None:
    await store.claim("t1", "r1", request=authed_request(), lease=timedelta(0))

    assert await _wait(store, timeout=timedelta(seconds=0.05)) is False


@pytest.mark.parametrize("poll_interval", [(0.0, 1.0), (-1.0, 1.0), (2.0, 1.0)])
def test_a_poll_interval_that_cannot_back_off_is_refused(
    poll_interval: tuple[float, float],
) -> None:
    with pytest.raises(ValueError, match="poll_interval"):
        DefaultRunStore(poll_interval=poll_interval)


def test_an_idle_tick_is_one_query_on_the_log(store: DefaultRunStore) -> None:
    async_to_sync(_claim)(store)

    with CaptureQueriesContext(connection) as queries:
        assert async_to_sync(_wait)(store, 0, timedelta(0)) is False

    assert len(queries.captured_queries) == 1
    sql = queries.captured_queries[0]["sql"]
    assert sql.startswith("SELECT")
    assert StoredRunLog._meta.db_table in sql
    assert '"owner_id" = ' in sql
    assert '"run_id" = ' in sql


def test_the_wait_backs_off_while_idle_and_never_sleeps_past_its_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    pauses: list[float] = []

    async def sleep(delay: float) -> None:
        pauses.append(delay)
        now[0] += delay

    monkeypatch.setattr(default_run_store, "monotonic", lambda: now[0])
    monkeypatch.setattr(default_run_store, "sleep", sleep)
    store = DefaultRunStore()
    async_to_sync(_claim)(store)

    assert async_to_sync(_wait)(store, 0, timedelta(seconds=6)) is False
    assert pauses == [0.5, 1.0, 2.0, 2.0, 0.5]
    pauses.clear()
    assert async_to_sync(_wait)(store, 0, timedelta(seconds=1)) is False
    assert pauses == [0.5, 0.5]
