from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any

from pydantic_ai.capabilities import AbstractCapability

from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.headless.types.run_stop import RunStop
from django_spec_agents.persistence.types.run_outcome import RunOutcome


class _Runner:
    async def start(
        self,
        agent_id: str,
        prompt: str,
        principal: Any,
        *,
        timeout: float,
        thread_id: str | None = None,
        parent: str | None = None,
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
        parent: str | None = None,
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
    assert await runner.start("a", "hi", object(), timeout=30.0) == HeadlessStart(
        thread_id="t1", run_id="r1"
    )
    assert (await runner.run_to_first_stop("a", "hi", object(), timeout=30.0)).waiting is None
    assert await runner.cancel("t1", object())


def test_a_runner_that_cannot_cancel_does_not() -> None:
    assert not isinstance(_NoCancel(), HeadlessRunner)


def test_start_and_first_stop_require_a_timeout() -> None:
    for name in ("start", "run_to_first_stop"):
        timeout = inspect.signature(getattr(HeadlessRunner, name)).parameters["timeout"]
        assert (timeout.kind, timeout.default) == (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.empty,
        )
