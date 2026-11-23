from __future__ import annotations

from django_spec_agents.contrib.store.default_run_store import DefaultRunStore
from django_spec_agents.persistence.types.run_store import RunStore
from django_spec_agents.persistence.types.watchable_run_store import WatchableRunStore


class _StoreWithoutWait:
    async def claim(self, thread_id, run_id, *, request, lease): ...  # noqa: ANN001, ANN201
    async def renew(self, thread_id, run_id, *, request, lease): ...  # noqa: ANN001, ANN201
    async def finish(self, thread_id, run_id, outcome, *, request, waiting=()): ...  # noqa: ANN001, ANN201
    async def append(self, run_id, events, *, request): ...  # noqa: ANN001, ANN201
    async def read(self, run_id, *, request, after=0, limit=None): ...  # noqa: ANN001, ANN201
    async def get(self, thread_id, *, request): ...  # noqa: ANN001, ANN201
    async def get_many(self, thread_ids, *, request): ...  # noqa: ANN001, ANN201
    async def request_cancel(self, thread_id, *, request): ...  # noqa: ANN001, ANN201
    async def mark_read(self, thread_id, *, request): ...  # noqa: ANN001, ANN201
    async def prune(self): ...  # noqa: ANN201


def test_a_store_without_wait_is_still_a_run_store() -> None:
    assert isinstance(_StoreWithoutWait(), RunStore)
    assert not isinstance(_StoreWithoutWait(), WatchableRunStore)


def test_the_default_run_store_is_watchable() -> None:
    assert isinstance(DefaultRunStore(), WatchableRunStore)
