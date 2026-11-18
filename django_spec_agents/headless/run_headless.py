from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django_service_specs import (
    DataclassPresenter,
    DataclassValidator,
    PermissionCheck,
    ServiceSpec,
)

from django_spec_agents.headless.types.headless_firing import HeadlessFiring
from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.headless.types.run_stop import RunStop


def run_headless(runner: HeadlessRunner, *, permissions: Sequence[PermissionCheck]) -> ServiceSpec:
    """The kernel spec a worker runs a queued headless start with, over ``runner``.

    Register it in the registry a
    [`FiringQueue`][django_spec_agents.headless.types.firing_queue.FiringQueue]'s
    worker reads. Its body is ``runner.run_to_first_stop`` with the arguments of a
    [`HeadlessFiring`][django_spec_agents.headless.types.headless_firing.HeadlessFiring]
    and the worker's principal, so the run's owner lives in the worker, and the
    firing presents the [`RunStop`][django_spec_agents.RunStop]. A firing's ``run_id`` is
    passed only when set, so a runner without the parameter still serves the rest.

    It declares ``atomic=False``, since one transaction must not span a whole agent
    run, and ``idempotent=False``, since a second call is a second paid run. A worker
    that dies mid-run is not run again: the run's lease lapses, so its thread reads
    ``failed`` with reason ``interrupted``, and the queue reports the firing ``lapsed``.

    ``permissions`` are the spec's, checked at enqueue and again on the worker.
    """

    async def service(user: Any, data: HeadlessFiring) -> RunStop:
        adopt: dict[str, Any] = {} if data.run_id is None else {"run_id": data.run_id}
        return await runner.run_to_first_stop(
            data.agent_id,
            data.prompt,
            user,
            timeout=data.timeout,
            thread_id=data.thread_id,
            parent=data.parent,
            lineage=data.lineage,
            **adopt,
        )

    return ServiceSpec(
        service=service,
        permissions=permissions,
        validator=DataclassValidator(HeadlessFiring),
        presenter=DataclassPresenter(RunStop),
        atomic=False,
        idempotent=False,
    )


__all__ = ["run_headless"]
