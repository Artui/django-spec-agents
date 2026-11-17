from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.http import HttpRequest
from django_service_specs import InvalidArguments, NotPermitted, PermissionCheck, kernel_spec

from django_spec_agents.contrib.store.default_run_store import DefaultRunStore
from django_spec_agents.headless.run_headless import run_headless
from django_spec_agents.headless.types.firing_queue import FiringQueue
from django_spec_agents.headless.types.firing_state import FiringState
from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.headless.types.headless_start import HeadlessStart
from django_spec_agents.headless.types.run_stop import RunStop
from django_spec_agents.persistence.types.run_end_reason import RunEndReason
from django_spec_agents.persistence.types.run_outcome import RunOutcome
from django_spec_agents.persistence.types.run_waiting import RunWaiting
from django_spec_agents.persistence.types.thread_run_status import ThreadRunStatus
from tests.headless.task_worker import TaskWorker

pytestmark = pytest.mark.django_db(transaction=True)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_LEASE = timedelta(seconds=30)
_WAITING = RunWaiting(kind="approval", summary="delete_thing", tool_call_id="c1", destructive=True)
_PARAMS = {"agent_id": "support", "prompt": "Summarise the inbox.", "timeout": 60}


class _Clock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now


class _Authenticated(PermissionCheck):
    def has_permission(self, principal: Any, spec: Any) -> bool:
        return principal is not None and principal.is_authenticated


class _Nobody(PermissionCheck):
    def has_permission(self, principal: Any, spec: Any) -> bool:
        return False


class _Runner:
    """A ``HeadlessRunner`` that claims its thread in a real run store, then stops or dies."""

    def __init__(self, store: DefaultRunStore, fault: BaseException | None = None) -> None:
        self.store, self.fault, self.calls = store, fault, []

    async def start(self, *args: Any, **kwargs: Any) -> HeadlessStart:
        raise NotImplementedError

    async def run_to_first_stop(
        self,
        agent_id: str,
        prompt: str,
        principal: Any,
        *,
        timeout: float,
        thread_id: str | None = None,
        parent: str | None = None,
        capabilities: Sequence[Any] = (),
    ) -> RunStop:
        self.calls.append((agent_id, prompt, principal, timeout, thread_id, parent, capabilities))
        request = HttpRequest()
        request.user = principal
        await self.store.claim("t1", "r1", request=request, lease=_LEASE)
        if self.fault is not None:
            raise self.fault
        await self.store.finish("t1", "r1", RunOutcome.WAITING, request=request, waiting=_WAITING)
        return RunStop("t1", "r1", RunOutcome.WAITING, _WAITING)

    async def cancel(self, thread_id: str, principal: Any) -> bool:
        raise NotImplementedError


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(clock: _Clock) -> DefaultRunStore:
    return DefaultRunStore(clock=clock)


@pytest.fixture
def user() -> Any:
    return get_user_model().objects.create_user(username="member")


def _worker(runner: _Runner, clock: _Clock, permissions: Any = None) -> TaskWorker:
    spec = run_headless(runner, permissions=permissions or [_Authenticated()])
    return TaskWorker({"run_headless": spec}, clock, lease=timedelta(minutes=5))


def test_the_doubles_have_the_shapes_they_stand_in_for(store: DefaultRunStore) -> None:
    assert isinstance(_Runner(store), HeadlessRunner)
    assert isinstance(_worker(_Runner(store), _Clock()), FiringQueue)


def test_the_spec_declares_no_transaction_and_no_idempotence(store: DefaultRunStore) -> None:
    spec = kernel_spec(run_headless(_Runner(store), permissions=[_Authenticated()]))

    assert spec.atomic is False
    assert spec.idempotent is False


def test_a_worker_runs_the_spec_to_its_first_stop(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _Runner(store)
    worker = _worker(runner, clock)
    params = {**_PARAMS, "thread_id": "t1", "parent": "t0"}

    run_id = worker.enqueue("run_headless", params, actor=user)
    assert worker.state(run_id) is FiringState.PENDING
    worker.run(run_id)

    assert worker.rows[run_id].output == {
        "thread_id": "t1",
        "run_id": "r1",
        "outcome": "waiting",
        "waiting": {
            "kind": "approval",
            "summary": "delete_thing",
            "tool_call_id": "c1",
            "destructive": True,
        },
    }
    assert worker.state(run_id) is FiringState.FINISHED
    [(agent_id, prompt, principal, timeout, thread_id, parent, capabilities)] = runner.calls
    assert (agent_id, prompt, timeout, thread_id, parent) == (
        "support",
        "Summarise the inbox.",
        60.0,
        "t1",
        "t0",
    )
    assert principal is not user and principal.pk == user.pk
    assert capabilities == ()


def test_the_thread_and_parent_default_to_none(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _Runner(store)
    worker = _worker(runner, clock)

    worker.run(worker.enqueue("run_headless", _PARAMS, actor=user))

    [(*_, thread_id, parent, _capabilities)] = runner.calls
    assert (thread_id, parent) == (None, None)


def test_a_restart_leaves_the_lease_to_lapse(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    runner = _Runner(store, SystemExit(1))
    worker = _worker(runner, clock)
    run_id = worker.enqueue("run_headless", _PARAMS, actor=user)

    with pytest.raises(SystemExit):
        worker.run(run_id)

    request = HttpRequest()
    request.user = user
    assert async_to_sync(store.get)("t1", request=request).status is ThreadRunStatus.RUNNING
    assert worker.state(run_id) is FiringState.PENDING

    clock.now = _T0 + timedelta(minutes=5, seconds=1)
    lapsed = async_to_sync(store.get)("t1", request=request)
    assert (lapsed.status, lapsed.reason) == (ThreadRunStatus.FAILED, RunEndReason.INTERRUPTED)
    worker.sweep()
    assert worker.state(run_id) is FiringState.LAPSED

    worker.run(run_id)
    assert len(runner.calls) == 1


def test_a_runner_that_raises_ends_the_firing(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    worker = _worker(_Runner(store, RuntimeError("model unreachable")), clock)
    run_id = worker.enqueue("run_headless", _PARAMS, actor=user)

    worker.run(run_id)

    assert worker.state(run_id) is FiringState.FINISHED


def test_enqueue_refuses_what_the_spec_refuses(
    store: DefaultRunStore, clock: _Clock, user: Any
) -> None:
    worker = _worker(_Runner(store), clock)

    with pytest.raises(InvalidArguments):
        worker.enqueue("run_headless", {"agent_id": "support"}, actor=user)
    with pytest.raises(NotPermitted):
        _worker(_Runner(store), clock, [_Nobody()]).enqueue("run_headless", _PARAMS, actor=user)
    assert worker.state("1") is None
