from __future__ import annotations

import logging
import sys
import types
from collections.abc import Callable
from typing import Any
from unittest import mock

import pytest
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django_service_specs import NotPermitted, PrincipalUnavailable
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai.exceptions import ToolFailed, UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai_harness.step_persistence import StepPersistence

from django_spec_agents.agent.agent_factory import build_agent
from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.contrib.store.default_step_store import DefaultStepStore
from django_spec_agents.policy.failure import tool_failure_policy as failure_policy_module
from django_spec_agents.policy.failure.tool_failure_policy import ToolFailurePolicy
from django_spec_agents.policy.failure.types.tool_failure_config import ToolFailureConfig
from django_spec_agents.policy.failure.utils import PolicyToolFailed
from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry
from tests.policy.audit.capturing_audit_logger import CapturingAuditLogger
from tests.request_helpers import authed_request
from tests.tool_retries import tool_answers, tool_retries


def _raising_registry() -> ToolRegistry:
    reg = ToolRegistry()

    @tool(reg)
    def boom(target: str) -> str:
        """Raise on purpose."""
        raise RuntimeError("credentials=hunter2")

    return reg


def _agent(
    *,
    tool_failure: ToolFailureConfig | None = None,
    audit: Any = None,
) -> Agent[AgentDeps, Any]:
    config = AgentConfig(
        model=TestModel(call_tools=["boom"]),
        audit_logger=audit,
        tool_failure=tool_failure if tool_failure is not None else ToolFailureConfig(),
    )
    return build_agent(_raising_registry(), config)


def _asks_again(name: str = "boom", *, executions: list[str] | None = None) -> ToolRegistry:
    """A registry whose one tool answers every call with a ``ModelRetry``."""
    reg = ToolRegistry()

    async def asks(target: str) -> str:
        """Always asks again."""
        if executions is not None:
            executions.append(target)
        raise ModelRetry("try another target")

    tool(reg, name=name)(asks)
    return reg


def _tool_returns(result: Any) -> list[Any]:
    return tool_answers(result.all_messages())


# What the model reads for a failure the policy converted with the default config.
_FAILED_RESULT = (
    "The boom tool failed and returned no result. "
    "The failure has been recorded; do not retry the same call."
)


async def test_a_raising_tool_does_not_end_the_run() -> None:
    """What the policy exists for."""
    result = await _agent().run("go", deps=AgentDeps(user=None))

    returns = _tool_returns(result)
    assert len(returns) == 1
    assert returns[0].outcome == "failed"
    assert "boom" in returns[0].content


async def test_the_default_message_does_not_carry_the_exception_text() -> None:
    # An exception message is written for an operator.
    result = await _agent().run("go", deps=AgentDeps(user=None))

    assert "hunter2" not in _tool_returns(result)[0].content


async def test_include_detail_opts_into_the_exception_text() -> None:
    result = await _agent(tool_failure=ToolFailureConfig(include_detail=True)).run(
        "go", deps=AgentDeps(user=None)
    )

    content = _tool_returns(result)[0].content
    assert "RuntimeError" in content
    assert "hunter2" in content


async def test_disabled_restores_the_failing_run() -> None:
    with pytest.raises(RuntimeError, match="hunter2"):
        await _agent(tool_failure=ToolFailureConfig(enabled=False)).run(
            "go", deps=AgentDeps(user=None)
        )


async def test_the_operator_copy_is_never_redacted() -> None:
    """Audit still records the real failure against the tool that caused it."""
    audit = CapturingAuditLogger()

    await _agent(audit=audit).run("go", deps=AgentDeps(user=None))

    assert [(e.tool_name, e.success, e.error) for e in audit.events] == [
        ("boom", False, "RuntimeError: credentials=hunter2")
    ]


class _ErrorHook(AbstractCapability[Any]):
    """An ``on_tool_execute_error`` of a project's own, recording what it is handed."""

    def __init__(self, answer: Any = None) -> None:
        self.seen: list[Exception] = []
        self._answer = answer

    async def on_tool_execute_error(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,
        error: Exception,
    ) -> Any:
        self.seen.append(error)
        if self._answer is None:
            raise error
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def _composed(
    *capabilities: AbstractCapability[Any],
    error: Exception,
    retries: int = 1,
    audit: Any = None,
) -> Agent[AgentDeps, Any]:
    """An agent whose one tool raises ``error``, with capabilities as a project adds them."""
    reg = ToolRegistry()
    calls: list[int] = []

    @tool(reg)
    def boom(target: str) -> str:
        """Raise on purpose, the first time."""
        calls.append(1)
        if len(calls) == 1:
            raise error
        return "ok"

    return build_agent(
        reg,
        AgentConfig(
            model=TestModel(call_tools=["boom"]),
            capabilities=list(capabilities),
            retries=retries,
            audit_logger=audit,
        ),
    )


def test_the_policy_declares_outermost_ordering() -> None:
    """Outermost, so its error hook is the last of every capability's to run."""
    ordering = ToolFailurePolicy().get_ordering()
    assert ordering is not None
    assert ordering.position == "outermost"


async def test_every_other_error_hook_sees_the_tools_exception() -> None:
    """The policy converts last, so a capability beside it sees the raw exception."""
    hook = _ErrorHook()
    error = RuntimeError("credentials=hunter2")

    result = await _composed(hook, error=error).run("go", deps=AgentDeps(user=None))

    assert hook.seen == [error]
    returns = _tool_returns(result)
    assert [(r.outcome, r.content) for r in returns] == [("failed", _FAILED_RESULT)]


class _OutermostErrorHook(_ErrorHook):
    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="outermost")


async def test_an_outermost_error_hook_still_sees_the_tools_exception() -> None:
    """List order breaks ties, so ``build_agent`` also places the policy first."""
    hook = _OutermostErrorHook()
    error = RuntimeError("credentials=hunter2")

    result = await _composed(hook, error=error).run("go", deps=AgentDeps(user=None))

    assert hook.seen == [error]
    assert [r.outcome for r in _tool_returns(result)] == ["failed"]


async def test_the_policy_raises_its_own_tool_failed_subclass() -> None:
    """What the policy raises is ``PolicyToolFailed``, from the tool's exception."""
    hook = _OutermostErrorHook()
    error = RuntimeError("credentials=hunter2")

    def boom(target: str) -> str:
        """Raise on purpose."""
        raise error

    agent = Agent(
        TestModel(call_tools=["boom"]),
        tools=[boom],
        capabilities=[hook, ToolFailurePolicy()],
    )
    await agent.run("go")

    [raised] = hook.seen
    assert type(raised) is PolicyToolFailed
    assert raised.__cause__ is error
    assert raised != ToolFailed(raised.message)


class _Wrapper(AbstractCapability[Any]):
    """Keeps the exception that reaches its ``wrap_tool_execute``."""

    def __init__(self) -> None:
        self.seen: list[Exception] = []

    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: Callable[[dict[str, Any]], Any],
    ) -> Any:
        try:
            return await handler(args)
        except Exception as error:
            self.seen.append(error)
            raise


async def test_a_wrapper_sees_the_policys_subclass() -> None:
    """Which exception a failed tool span names."""
    wrapper = _Wrapper()
    error = RuntimeError("credentials=hunter2")

    await _composed(wrapper, error=error).run("go", deps=AgentDeps(user=None))

    [seen] = wrapper.seen
    assert type(seen) is PolicyToolFailed
    assert seen.__cause__ is error


@pytest.mark.django_db(transaction=True)
async def test_a_step_store_logs_the_tools_exception_not_the_policys() -> None:
    """harness's ``StepPersistence`` logs ``tool_call_failed`` from its error hook."""
    request = authed_request()
    store = DefaultStepStore(request)

    await _composed(
        StepPersistence(store=store, run_id="run-1"), error=RuntimeError("credentials=hunter2")
    ).run("go", deps=AgentDeps(user=None))

    failed = [e for e in await store.list_events(run_id="run-1") if e.kind == "tool_call_failed"]
    assert [(e.tool_name, e.error) for e in failed] == [
        ("boom", "RuntimeError('credentials=hunter2')")
    ]


async def test_a_recovering_capability_pre_empts_the_conversion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A capability that recovers answers first, so the policy never converts."""
    hook = _ErrorHook(answer="recovered")
    error = RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="django_pydantic_agent.failure"):
        result = await _composed(hook, error=error).run("go", deps=AgentDeps(user=None))

    assert hook.seen == [error]
    assert [r.content for r in _tool_returns(result)] == ["recovered"]
    assert logging.getLogger("django_pydantic_agent.failure") is failure_policy_module._logger
    assert not [r for r in caplog.records if r.name == "django_pydantic_agent.failure"]


async def test_another_capabilitys_retry_is_not_converted() -> None:
    """``ModelRetry`` from an error hook is pydantic-ai's documented way to ask for a redo."""
    hook = _ErrorHook(answer=ModelRetry("try another target"))

    result = await _composed(hook, error=RuntimeError("boom")).run("go", deps=AgentDeps(user=None))

    assert [part.content for part in tool_retries(result.all_messages())] == ["try another target"]
    assert [r.content for r in _tool_returns(result)] == ["ok"]


async def test_another_capabilitys_retry_spends_the_tools_retry_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Passed through, it spends the tool's retry budget, and with none left the run ends."""
    hook = _ErrorHook(answer=ModelRetry("try another target"))
    audit = CapturingAuditLogger()

    with (
        caplog.at_level(logging.ERROR, logger="django_pydantic_agent.failure"),
        pytest.raises(UnexpectedModelBehavior, match="exceeded max retries count of 0"),
    ):
        await _composed(hook, error=RuntimeError("boom"), retries=0, audit=audit).run(
            "go", deps=AgentDeps(user=None)
        )

    assert logging.getLogger("django_pydantic_agent.failure") is failure_policy_module._logger
    assert not [r for r in caplog.records if r.name == "django_pydantic_agent.failure"]
    assert [(e.success, e.error) for e in audit.events] == [(False, "RuntimeError: boom")]


async def test_a_tools_own_retry_with_no_budget_left_is_converted_into_a_failed_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With retries left a tool's own ``ModelRetry`` never reaches an error hook."""
    reg = _asks_again()
    audit = CapturingAuditLogger()
    agent = build_agent(
        reg, AgentConfig(model=TestModel(call_tools=["boom"]), retries=0, audit_logger=audit)
    )
    with caplog.at_level(logging.ERROR, logger="django_pydantic_agent.failure"):
        result = await agent.run("go", deps=AgentDeps(user=None))

    assert [(r.outcome, r.content) for r in _tool_returns(result)] == [("failed", _FAILED_RESULT)]
    assert [
        r.getMessage() for r in caplog.records if r.name == "django_pydantic_agent.failure"
    ] == ["django-spec-agents: tool 'boom' failed; the run continues with a failed result"]
    [event] = audit.events
    assert event.success is False
    assert event.error.startswith(
        "UnexpectedModelBehavior: Tool 'boom' exceeded max retries count of 0."
    )


async def test_a_tool_keeps_executing_after_its_retry_budget_is_spent() -> None:
    """Converting a spent budget does not stop the model calling the tool again."""
    executions: list[str] = []
    reg = _asks_again(executions=executions)

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(executions) < 3:
            return ModelResponse(parts=[ToolCallPart("boom", {"target": "x"})])
        return ModelResponse(parts=[TextPart("giving up")])

    agent = build_agent(reg, AgentConfig(model=FunctionModel(model_fn), retries=0))

    result = await agent.run("go", deps=AgentDeps(user=None))

    assert len(executions) == 3
    assert [r.outcome for r in _tool_returns(result)] == ["failed", "failed", "failed"]


async def test_reraising_unexpected_model_behavior_ends_the_run_on_a_spent_retry_budget() -> None:
    """The opt-out the docs describe for a tool's own ``ModelRetry``."""
    agent = build_agent(
        _asks_again(),
        AgentConfig(
            model=TestModel(call_tools=["boom"]),
            retries=0,
            tool_failure=ToolFailureConfig(
                reraise=(UnexpectedModelBehavior, DjangoPermissionDenied)
            ),
        ),
    )

    with pytest.raises(UnexpectedModelBehavior, match="exceeded max retries count of 0") as info:
        await agent.run("go", deps=AgentDeps(user=None))

    assert isinstance(info.value.__cause__, ModelRetry)


async def test_reraising_unexpected_model_behavior_also_ends_the_run_for_a_sub_agents_budget() -> (
    None
):
    """The same entry catches every ``UnexpectedModelBehavior``, not only the tool's own budget."""
    sub_agent = build_agent(
        _asks_again("inner"),
        AgentConfig(
            model=TestModel(call_tools=["inner"]),
            retries=0,
            tool_failure=ToolFailureConfig(enabled=False),
        ),
    )
    reg = ToolRegistry()

    @tool(reg)
    async def delegate(target: str) -> str:
        """Delegates to a sub-agent and lets its failure propagate."""
        await sub_agent.run("go", deps=AgentDeps(user=None))
        return "unreachable"

    def agent_with(reraise: tuple[type[BaseException], ...] | None) -> Agent[AgentDeps, str]:
        return build_agent(
            reg,
            AgentConfig(
                model=TestModel(call_tools=["delegate"]),
                tool_failure=ToolFailureConfig(reraise=reraise),
            ),
        )

    converted = await agent_with(None).run("go", deps=AgentDeps(user=None))
    assert [r.outcome for r in _tool_returns(converted)] == ["failed"]

    with pytest.raises(UnexpectedModelBehavior, match="Tool 'inner'") as info:
        await agent_with((UnexpectedModelBehavior,)).run("go", deps=AgentDeps(user=None))

    assert isinstance(info.value.__cause__, ModelRetry)


async def test_another_capabilitys_failed_result_is_not_converted() -> None:
    """A ``ToolFailed`` from an error hook is that capability's answer, not the policy's."""
    hook = _ErrorHook(answer=ToolFailed("The upstream is down for maintenance."))

    result = await _composed(hook, error=RuntimeError("boom")).run("go", deps=AgentDeps(user=None))

    assert [(r.outcome, r.content) for r in _tool_returns(result)] == [
        ("failed", "The upstream is down for maintenance.")
    ]


async def test_a_denial_passes_every_error_hook_untouched() -> None:
    """Running last does not change that a refusal still ends the run."""
    hook = _ErrorHook()
    error = DjangoPermissionDenied("not yours")

    with pytest.raises(DjangoPermissionDenied) as raised:
        await _composed(hook, error=error).run("go", deps=AgentDeps(user=None))

    assert raised.value is error
    assert hook.seen == [error]


@pytest.mark.parametrize(
    "answer",
    [ModelRetry("try another target"), ToolFailed("The upstream is down."), "recovered"],
    ids=["retry", "failed-result", "recovered"],
)
async def test_the_failure_logger_hears_nothing_the_policy_did_not_convert(
    answer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Another capability answered the call, so the policy converted and logged nothing."""
    with caplog.at_level(logging.ERROR, logger="django_pydantic_agent.failure"):
        await _composed(_ErrorHook(answer=answer), error=RuntimeError("boom")).run(
            "go", deps=AgentDeps(user=None)
        )

    assert logging.getLogger("django_pydantic_agent.failure") is failure_policy_module._logger
    assert not [r for r in caplog.records if r.name == "django_pydantic_agent.failure"]


async def test_a_refusal_passing_through_is_not_logged_as_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.ERROR, logger="django_pydantic_agent.failure"),
        pytest.raises(DjangoPermissionDenied),
    ):
        await _composed(error=DjangoPermissionDenied("not yours")).run(
            "go", deps=AgentDeps(user=None)
        )

    # Non-vacuous only while this literal is the policy's logger name.
    assert logging.getLogger("django_pydantic_agent.failure") is failure_policy_module._logger
    assert not [r for r in caplog.records if r.name == "django_pydantic_agent.failure"]


async def test_the_failure_is_logged_with_its_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="django_pydantic_agent.failure"):
        await _agent().run("go", deps=AgentDeps(user=None))

    assert any(record.exc_info is not None for record in caplog.records)


async def test_a_model_retry_still_reaches_the_model_as_a_retry() -> None:
    """Control flow must pass through untouched."""
    reg = ToolRegistry()
    calls: list[int] = []

    @tool(reg)
    def flaky(target: str) -> str:
        """Fail once, then succeed."""
        calls.append(1)
        if len(calls) == 1:
            raise ModelRetry("try again")
        return "ok"

    agent = build_agent(reg, AgentConfig(model=TestModel(call_tools=["flaky"])))

    await agent.run("go", deps=AgentDeps(user=None))

    assert len(calls) == 2


class TestAuthorizationIsNotAToolFailure:
    """A denial is the one failure the policy must not absorb."""

    def _denying_agent(
        self, error: BaseException, *, tool_failure: ToolFailureConfig | None = None
    ) -> Agent[AgentDeps, Any]:
        reg = ToolRegistry()

        @tool(reg)
        def get_invoice(invoice_id: int) -> str:
            """Read an invoice."""
            raise error

        return build_agent(
            reg,
            AgentConfig(
                model=TestModel(call_tools=["get_invoice"]),
                tool_failure=tool_failure if tool_failure is not None else ToolFailureConfig(),
            ),
        )

    @pytest.mark.parametrize(
        ("error", "tool_failure"),
        [
            pytest.param(DjangoPermissionDenied("not yours"), None, id="django-denial"),
            # The kernel's refusals are not Django's class, so a spec must still end the run.
            pytest.param(NotPermitted("not yours"), None, id="kernel-not-permitted"),
            pytest.param(
                PrincipalUnavailable("deactivated"), None, id="kernel-principal-unavailable"
            ),
            pytest.param(
                LookupError("tenant missing"),
                ToolFailureConfig(reraise=(LookupError,)),
                id="project-reraise",
            ),
        ],
    )
    async def test_a_reraised_failure_aborts_the_run(
        self, error: BaseException, tool_failure: ToolFailureConfig | None
    ) -> None:
        agent = self._denying_agent(error, tool_failure=tool_failure)

        with pytest.raises(type(error)):
            await agent.run("go", deps=AgentDeps(user=None))

    @pytest.mark.parametrize(
        ("error", "tool_failure"),
        [
            # The escape hatch for a project that wants every failure converted.
            pytest.param(
                DjangoPermissionDenied("not yours"),
                ToolFailureConfig(reraise=()),
                id="empty-reraise-set",
            ),
            pytest.param(RuntimeError("boom"), None, id="ordinary-failure"),
        ],
    )
    async def test_a_failure_outside_the_reraise_set_is_absorbed(
        self, error: BaseException, tool_failure: ToolFailureConfig | None
    ) -> None:
        result = await self._denying_agent(error, tool_failure=tool_failure).run(
            "go", deps=AgentDeps(user=None)
        )

        assert _tool_returns(result)[0].outcome == "failed"


async def _handle(policy: ToolFailurePolicy, error: Exception) -> None:
    await policy.on_tool_execute_error(
        None,  # type: ignore[arg-type]
        call=ToolCallPart(tool_name="t", args={}),
        tool_def=ToolDefinition(name="t", parameters_json_schema={"type": "object"}),
        args=None,
        error=error,
    )


def test_the_default_set_is_djangos_denial_and_the_kernels() -> None:
    """A host with another refusal class names it in ``reraise=``."""
    exceptions = types.ModuleType("rest_framework.exceptions")
    exceptions.PermissionDenied = type("PermissionDenied", (Exception,), {})
    stack = {
        "rest_framework": types.ModuleType("rest_framework"),
        "rest_framework.exceptions": exceptions,
    }
    with mock.patch.dict(sys.modules, stack):
        assert failure_policy_module._denial_types() == (
            DjangoPermissionDenied,
            NotPermitted,
            PrincipalUnavailable,
        )


async def test_the_default_set_survives_the_kernel_not_being_installed() -> None:
    """A host-only install has no kernel, so the set must resolve without its imports."""
    with mock.patch.dict(sys.modules, {"django_service_specs": None}):
        policy = ToolFailurePolicy()

    with pytest.raises(DjangoPermissionDenied):
        await _handle(policy, DjangoPermissionDenied("nope"))

    with pytest.raises(ToolFailed):
        await _handle(policy, NotPermitted("nope"))

    with pytest.raises(ToolFailed):
        await _handle(policy, PrincipalUnavailable("nope"))
