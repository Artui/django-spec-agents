from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any

import pytest
from pydantic_ai.capabilities import AbstractCapability

from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.headless.types.run_stop import RunStop
from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from django_spec_agents.persistence.types.thread_ref import ThreadRef


class _Runner:
    async def start(
        self,
        agent_id: str,
        prompt: str,
        principal: Any,
        *,
        timeout: float,
        thread_id: str | None = None,
        parent: ThreadRef | None = None,
        lineage: ChildLineageRecord | None = None,
        capabilities: Sequence[AbstractCapability[Any]] = (),
    ) -> HeadlessStart:
        return HeadlessStart(thread_id=thread_id or "t1", run_id="r1")

    async def run_to_first_stop(
        self,
        agent_id: str,
        prompt: str,
        principal: Any,
        *,
        timeout: float,
        thread_id: str | None = None,
        parent: ThreadRef | None = None,
        lineage: ChildLineageRecord | None = None,
        capabilities: Sequence[AbstractCapability[Any]] = (),
    ) -> RunStop:
        return RunStop(thread_id=thread_id or "t1", run_id="r1", outcome=RunOutcome.COMPLETED)

    async def cancel(self, thread_id: str, principal: Any) -> bool:
        return True


class _NoCancel:
    async def start(self, *args: Any, **kwargs: Any) -> HeadlessStart: ...
    async def run_to_first_stop(self, *args: Any, **kwargs: Any) -> RunStop: ...


async def test_a_runner_with_the_three_methods_satisfies_it() -> None:
    runner = _Runner()

    assert isinstance(runner, HeadlessRunner)
    assert await runner.start("a", "hi", object(), timeout=60.0) == HeadlessStart(
        thread_id="t1", run_id="r1"
    )
    assert (await runner.run_to_first_stop("a", "hi", object(), timeout=60.0)).waiting == ()
    assert await runner.cancel("t1", object())


def test_a_runner_that_cannot_cancel_does_not() -> None:
    assert not isinstance(_NoCancel(), HeadlessRunner)


@pytest.mark.parametrize("method", ["start", "run_to_first_stop"])
def test_a_headless_run_requires_a_timeout(method: str) -> None:
    parameter = inspect.signature(getattr(HeadlessRunner, method)).parameters["timeout"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("method", ["start", "run_to_first_stop"])
def test_answers_are_an_optional_keyword_a_runner_may_lack(method: str) -> None:
    parameter = inspect.signature(getattr(HeadlessRunner, method)).parameters["answers"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None
