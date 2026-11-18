from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django_service_specs import (
    DataclassPresenter,
    DataclassValidator,
    PermissionCheck,
    ServiceSpec,
)
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.headless.types.run_stop import RunStop
from django_spec_agents.policy.approvals.granted_approvals import GrantedApprovals
from django_spec_agents.routines.abstract_routine import AbstractRoutine
from django_spec_agents.routines.preapproved_exempt import preapproved_exempt
from django_spec_agents.routines.types.routine_firing import RoutineFiring
from django_spec_agents.routines.types.routine_grant_key import ROUTINE_GRANT_KEY

_AUTHORING = frozenset({"create_routine", "update_routine"})


class _CanRun(PermissionCheck):
    """The routine exists, the caller is its principal, and its policy's `can_run` agrees.
    Tests per conjunct: `..._does_not_exist`, `..._not_the_routines_principal`,
    `..._its_policy_may_not_run`, `..._asked_without_arguments_refuses`."""

    message = "This routine may not fire as this user."

    def __init__(self, model: type[AbstractRoutine]) -> None:
        self.model = model

    def has_permission(
        self, principal: Any, spec: Any, *, arguments: Mapping[str, Any] | None = None
    ) -> bool:
        if arguments is None:
            return False
        routine = self.model._default_manager.filter(pk=arguments["routine_id"]).first()
        if routine is None or routine.principal_id != getattr(principal, "pk", None):
            return False
        policy = self.model.routine_policy()
        assert policy is not None
        return policy.can_run(principal, routine)


class _FiringRecord(AbstractCapability[Any]):
    """Hides the routine-authoring tools, and stamps the grant on the firing's first request."""

    def __init__(self, grant: dict[str, Any]) -> None:
        super().__init__()
        self.grant = grant

    async def prepare_tools(
        self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        return [tool_def for tool_def in tool_defs if tool_def.name not in _AUTHORING]

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        first = next(m for m in ctx.messages if isinstance(m, ModelRequest))
        first.metadata = {**(first.metadata or {}), ROUTINE_GRANT_KEY: self.grant}
        return request_context


def run_agent_spec(
    model: type[AbstractRoutine], runner: HeadlessRunner, *, timeout: float
) -> ServiceSpec:
    """The kernel spec a routine's firing runs: `model`'s routine, to its first stop.

    Register it in the queue's registry under the
    [`RoutineSweep`][django_spec_agents.routines.RoutineSweep]'s `spec_name`. It takes the
    sweep's `{"routine_id", "thread_id"}` and runs the routine's prompt on its agent, as its
    principal, into that thread, within `timeout` seconds. It presents the
    [`RunStop`][django_spec_agents.RunStop]: `completed`, `waiting` when a gated call parked
    the thread, `timed_out` or `error`, so a parked firing ends `SUCCEEDED` with `waiting`
    and the thread's approval card takes it from there.

    Its permission check refuses a routine that does not exist, a caller who is not the
    routine's principal, and a routine the model's policy `can_run` refuses, at enqueue and
    again on the worker, so a routine disabled in between lapses its firing.

    The run gets [`GrantedApprovals`][django_spec_agents.GrantedApprovals] over
    [`preapproved_exempt`][django_spec_agents.routines.preapproved_exempt], marked
    `routine:<pk>`: a pre-approved tool that was gated when granted runs without parking, its
    result marked, and the kernel's own authorization still runs on the call. Any other gated
    call parks. Its first request's `metadata` carries the grant under
    [`ROUTINE_GRANT_KEY`][django_spec_agents.routines.ROUTINE_GRANT_KEY], and the
    routine-authoring tools, `create_routine` and `update_routine`, are not offered.

    It declares `atomic=False`, so no transaction spans the run, and `idempotent=False`, so a
    worker that dies mid-run is not run again: the queue reports the firing `lapsed`.
    """

    async def service(user: Any, data: RoutineFiring) -> RunStop:
        routine = await model._default_manager.aget(pk=data.routine_id)
        answered_by = f"routine:{routine.pk}"
        grant = {
            "routine_id": routine.pk,
            "updated_at": routine.updated_at.isoformat(),
            "answered_by": answered_by,
            "tools": [
                {"name": tool.name, "destructive": tool.destructive}
                for tool in routine.get_preapproved_tools()
            ],
        }
        return await runner.run_to_first_stop(
            routine.agent_id,
            routine.prompt,
            user,
            timeout=timeout,
            thread_id=data.thread_id,
            capabilities=[
                GrantedApprovals(preapproved_exempt(routine), answered_by),
                _FiringRecord(grant),
            ],
        )

    return ServiceSpec(
        service=service,
        permissions=[_CanRun(model)],
        validator=DataclassValidator(RoutineFiring),
        presenter=DataclassPresenter(RunStop),
        atomic=False,
        idempotent=False,
    )


__all__ = ["run_agent_spec"]
