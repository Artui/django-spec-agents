from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.db import DataError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness.spend_limits import (
    BatchSpendStore,
    InMemorySpendStore,
    SpendEntry,
    SpendLimitExceeded,
)

from django_spec_agents.agent.agent_factory import build_agent
from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.contrib.store.default_spend_store import DefaultSpendStore
from django_spec_agents.contrib.store.models import StoredSpendCounter, StoredSpendMarker
from django_spec_agents.policy.spend.types.spend_budget import SpendBudget
from django_spec_agents.policy.spend.types.spend_limits_config import SpendLimitsConfig
from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry

pytestmark = pytest.mark.django_db(transaction=True)

_HOUR = timedelta(hours=1)
_LONG_KEY = "conversation:" + "x" * 5000


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 24, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _e(key: str, usd: str = "0", **fields: Any) -> SpendEntry:
    return SpendEntry(key=key, usd=Decimal(usd), **fields)


_TWICE = _e("d", "1", token="t3")

# Each step is a batch to apply, or a clock advance; every step is followed by a read.
_STEPS: list[Any] = [
    [
        _e("a", "1.5", tokens=10, requests=1, ttl=_HOUR, token="t1"),
        _e("b", "2", tokens=3, requests=1, unpriced=1, token="t1"),
        _e(_LONG_KEY, "0.000000001", requests=1, token="t1"),
    ],
    [_e("a", "1.5", tokens=10, requests=1, ttl=_HOUR, token="t1")],
    [_e("a", "0.25"), _e("a", "1", token="t2", ttl=_HOUR), _e("a", "1", token="t2", ttl=_HOUR)],
    timedelta(minutes=30),
    [_e("b", "-0.5")],
    timedelta(minutes=45),
    [_e("a", "3", requests=1, ttl=_HOUR)],
    [_e("c", "1", ttl=timedelta(minutes=10)), _e("c", "1")],
    [_TWICE, _TWICE],
    timedelta(hours=2),
    [_e("b", "2", tokens=3, requests=1, unpriced=1, token="t1")],
    [],
]
_KEYS = ["a", "b", "c", "d", "never", _LONG_KEY]


async def _replay(store: Any, clock: _Clock) -> list[Any]:
    seen: list[Any] = []
    for step in _STEPS:
        if isinstance(step, timedelta):
            clock.now += step
        else:
            seen.append(dict(await store.add_many(step)))
        seen.append(dict(await store.get_many(_KEYS)))
    return seen


async def test_it_counts_as_the_harness_in_memory_store_does() -> None:
    ours, theirs = _Clock(), _Clock()

    expected = await _replay(InMemorySpendStore(clock=theirs), theirs)
    actual = await _replay(DefaultSpendStore(clock=ours), ours)

    assert actual == expected
    assert expected[1]["a"].usd == Decimal("1.5")
    assert expected[3]["a"].usd == Decimal("1.5")
    assert expected[5]["a"].usd == Decimal("2.75")


async def test_it_is_the_harness_batch_store() -> None:
    assert isinstance(DefaultSpendStore(), BatchSpendStore)


async def test_without_dedup_retain_a_replayed_entry_counts_again() -> None:
    store = DefaultSpendStore(dedup_retain=None)
    entry = _e("a", "1", token="t1")

    await store.add_many([entry])
    totals = await store.add_many([entry])

    assert totals["a"].usd == Decimal("2")
    assert not await StoredSpendMarker.objects.aexists()


async def test_a_marker_never_outlives_its_window() -> None:
    clock = _Clock()
    store = DefaultSpendStore(clock=clock)

    await store.add_many([_e("a", "1", token="t1", ttl=timedelta(minutes=5))])
    clock.now += timedelta(minutes=6)
    totals = await store.add_many([_e("a", "1", token="t1", ttl=timedelta(minutes=5))])

    assert totals["a"].usd == Decimal("1")


async def test_an_empty_batch_writes_nothing() -> None:
    assert await DefaultSpendStore().add_many([]) == {}
    assert not await StoredSpendCounter.objects.aexists()


async def test_the_sweep_deletes_what_has_expired() -> None:
    clock = _Clock()
    store = DefaultSpendStore(clock=clock, sweep_every=3)

    await store.add_many([_e("a", "1", token="t1", ttl=_HOUR), _e("b", "1")])
    clock.now += timedelta(hours=2)
    await store.add_many([_e("c", "1")])

    keys = [row.key async for row in StoredSpendCounter.objects.order_by("key")]
    assert keys == ["b", "c"]
    assert not await StoredSpendMarker.objects.aexists()


async def test_a_failed_sweep_is_logged_and_the_write_still_counts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the database went away")

    store = DefaultSpendStore(sweep_every=1)
    monkeypatch.setattr("django_spec_agents.contrib.store.default_spend_store._sweep_rows", _broken)

    with caplog.at_level(logging.WARNING):
        totals = await store.add_many([_e("a", "1")])

    assert totals["a"].usd == Decimal("1")
    assert "could not delete expired rows" in caplog.text


def look() -> str:
    """Look around."""
    return "nothing yet"


def _looks_then_answers(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    if len(messages) > 1:
        return ModelResponse(parts=[TextPart("done")])
    return ModelResponse(parts=[ToolCallPart("look", {})])


def _agent(store: DefaultSpendStore) -> Any:
    reg = ToolRegistry()
    tool(reg)(look)
    limits = SpendLimitsConfig(
        store=store,
        budgets=[SpendBudget("daily", usd=Decimal("2"))],
        price=lambda _response: Decimal("1"),
    )
    return build_agent(
        reg, AgentConfig(model=FunctionModel(_looks_then_answers), spend_limits=limits)
    )


async def test_a_budget_spent_on_one_worker_refuses_the_next_on_another() -> None:
    first = await _agent(DefaultSpendStore()).run("Go.", deps=AgentDeps(user=None))

    with pytest.raises(SpendLimitExceeded):
        await _agent(DefaultSpendStore()).run("Go.", deps=AgentDeps(user=None))

    assert first.output == "done"
    [row] = [row async for row in StoredSpendCounter.objects.all()]
    assert (row.usd_nanos, row.requests) == (2_000_000_000, 2)


async def test_a_batch_that_fails_part_way_counts_none_of_its_windows() -> None:
    store = DefaultSpendStore()

    with pytest.raises((OverflowError, DataError)):
        await store.add_many([_e("a", "1", token="t1"), _e("b", tokens=2**63)])

    assert (await store.get_many(["a"]))["a"].usd == Decimal("0")
    assert not await StoredSpendMarker.objects.aexists()


async def test_the_rows_read_as_their_key_and_marker() -> None:
    await DefaultSpendStore().add_many([_e("a", "1", token="t1")])

    counter = await StoredSpendCounter.objects.aget()
    marker = await StoredSpendMarker.objects.aget()
    assert (str(counter), str(marker)) == ("a", marker.marker_sha256)
