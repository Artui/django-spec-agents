"""``AuditCapability`` — one record per tool execution, describing the tool's own."""

from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    CapabilityOrdering,
    WrapToolExecuteHandler,
)
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, SkipToolExecution
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from django_spec_agents.policy.audit.types.audit_event import AuditEvent
from django_spec_agents.policy.audit.types.audit_logger import AuditLogger

_fallback_logger = logging.getLogger("django_pydantic_agent.audit")

# What pydantic-ai itself treats as a call that did not execute: a veto, and the
# two deferrals. Each skips every error and result hook, so audit sees one only
# in its wrapper, as the outcome of a call that never ran.
_NOT_EXECUTED = (SkipToolExecution, CallDeferred, ApprovalRequired)


class AuditCapability(AbstractCapability[Any]):
    """Records every tool execution to an
    [`AuditLogger`][django_spec_agents.AuditLogger] sink.

    A Pydantic-AI capability on the tool-execution hooks, so it records
    **every** tool the agent runs: registry tools, the drf-mcp and spec
    bridges, attachment and skill tools alike.

    **One record per execution, describing the tool's own execution**, whatever
    other capabilities sort ahead of it and on every supported pydantic-ai.
    That is everything composed through ``AgentConfig.capabilities`` unless
    its own ordering places it inside audit. A capability that sorts after
    audit is the exception, below.

    - ``arguments_repr`` holds the arguments the tool received, after every
      other capability's ``before_tool_execute`` has rewritten them.
    - ``success`` and ``error`` describe what the tool did. ``error`` is
      ``Type: message`` of the exception the tool raised, before any capability
      converts it (as [`ToolFailurePolicy`][django_spec_agents.ToolFailurePolicy]
      does) or recovers from it with an ``on_tool_execute_error`` that returns a
      value. A recovered failure is still recorded as the failure: recovering is
      a decision about the run, not about the tool. On an ordinary call the
      exceptions pydantic-ai routes past every error hook are recorded as they
      arrive: a tool's own ``ToolFailed`` as the ``ToolFailedError``
      pydantic-ai converts it into, with the same message, and ``ModelRetry``
      as ``ToolRetryError``. From a code-mode sandbox, whose nested tool
      manager converts neither, they are recorded as raised:
      ``ToolFailed: <message>`` and ``ModelRetry: <message>``. A
      ``ToolFailed`` keeps its own message either way; its cause and context
      are never read. A ``ModelRetry`` on an ordinary call with the tool's
      retry budget spent is different: pydantic-ai raises
      ``UnexpectedModelBehavior`` in its place, which does reach the error
      hooks, and that is what is recorded.
    - ``result_size`` is the length of the tool's own result, before any
      ``after_tool_execute`` rewrites it.
    - ``duration_ms`` spans the tool alone: from the last moment audit sees
      before the tool runs, which is after every other capability's
      ``before_tool_execute``, to the first moment it sees the outcome, which
      is before every other capability's ``on_tool_execute_error`` or
      ``after_tool_execute``. Other capabilities' hooks are not in it.

    **A call that never runs the tool produces no record.** That is a call
    stopped before audit's ``before_tool_execute``, by any exception, and one
    whose outcome is what pydantic-ai treats as not executed:
    ``SkipToolExecution``, ``CallDeferred`` or ``ApprovalRequired``, from
    wherever it is raised. So a veto such as pydantic-ai-harness's guardrails
    and tool-call judge raise, a destructive call the
    [`ToolGuard`][django_spec_agents.ToolGuard] holds for approval, a call a
    tool or a toolset such as pydantic-ai's ``approval_required()`` defers for
    approval, and a call deferred to external execution are not executions. A
    call held for approval is recorded when it is resumed and runs, like any
    other; a call deferred to external execution never runs in this process,
    so it has no record here.

    **How it gets the tool's own record.** pydantic-ai runs every capability's
    ``before_tool_execute``, ``on_tool_execute_error`` and
    ``after_tool_execute`` inside the ``wrap_tool_execute`` chain, so no
    wrapper position sees the raw outcome. Audit is pinned **innermost**,
    which makes its ``before_tool_execute`` the last to run and its
    ``on_tool_execute_error`` and ``after_tool_execute`` the first, and
    observes the tool from all four hooks. Its wrapper is the only one that
    writes the record, once, as it exits, preferring what the hooks captured
    over what it saw itself. The wrapper encloses every hook, and for what
    sorts ahead of audit the captures are what keep the record the tool's.

    **A capability that sorts after audit runs between audit and the tool.**
    One does when it is innermost and passed to a single run, since
    pydantic-ai sorts those after the agent's own capabilities whatever
    ``build_agent`` did; when it is composed by hand after audit in the
    innermost tier; and wherever it is composed, when its own ordering places
    it inside audit, as an innermost ``CapabilityOrdering`` with
    ``wrapped_by=[AuditCapability]`` does. What such a capability does can
    reach the record, through any of its hooks, and
    the record is then not the tool's own. These are measured examples, not
    a list of the only ways:

    - Through its ``before_tool_execute``,
      ``on_tool_execute_error`` and ``after_tool_execute``: a record misses
      its argument rewrite, records a ``ModelRetry`` its
      ``before_tool_execute`` raises as a failure though the tool never ran,
      records its recovery as a success and an exception it raises in place
      of the tool's, records a ``ModelRetry`` its ``after_tool_execute``
      raises as a failure though the tool succeeded, measures its result
      rewrite, and times its hooks with the tool.
    - Through its ``wrap_tool_execute``, when the tool raised
      ``ModelRetry`` or ``ToolFailed``, which pydantic-ai routes past every
      error and result hook, audit's included: a record carries its rewrite
      of the message, as pydantic-ai-harness's ``result_guard`` does,
      records its recovery as a success, and records an exception it raises
      in their place. If it runs the tool again, the record describes the
      later run alone and the failed run has none; with that rerun refused
      ahead of audit, the record pairs the first run's arguments with the
      refusal. Run again after a failure audit's error hook saw, or after a
      success, the tool gets one record, of its first run. Reruns it makes
      concurrently share one record, which can pair one run's arguments with
      another's result.

    A ``SkipToolExecution`` veto from it is still not recorded. Everything
    composed through ``AgentConfig.capabilities`` is unaffected unless its own
    ordering places it inside audit: ``build_agent`` appends audit after all
    of it, so audit sorts last among the innermost capabilities there.
    pydantic-ai-harness's tool guardrail and tool-call judge are innermost,
    so passed to a single run they sort after audit like any other.

    Recording is **non-raising**. A sink that throws is caught and logged to the
    ``django_pydantic_agent.audit`` Python logger, so a broken audit backend
    costs audit records rather than the run. Audit's own error hook re-raises
    what it is handed, and the exception the run sees is never changed by it.

    Args:
        logger: The sink each [`AuditEvent`][django_spec_agents.AuditEvent]
            is recorded to.
        ip_address: Fallback client IP, used only when the run's deps carry no
            ``ip_address``. Per-run deps come first because a constructor
            argument is per-agent: taking the IP from it alone forces a fresh
            agent per request, and building once anyway fails silently, with
            every record carrying the IP of whoever arrived first.
        organization_id: Org scope stamped onto every event, for a multi-tenant
            host.
    """

    def __init__(
        self,
        logger: AuditLogger,
        *,
        ip_address: str | None = None,
        organization_id: str | None = None,
    ) -> None:
        self._logger = logger
        self._ip_address = ip_address
        self._organization_id = organization_id
        # What this capability has seen of the tool call in flight. A context
        # variable, because pydantic-ai runs every tool call in its own asyncio
        # task, so parallel calls in one run and concurrent runs of one agent
        # each read their own value with no shared table to clean up: the state
        # goes with the call's task, including when a hook never runs. Not a
        # table keyed by ``tool_call_id``, which the model chooses and
        # ``TestModel`` repeats in every run. One per instance, so two audit
        # capabilities in one chain never read each other's.
        self._execution: ContextVar[_Execution | None] = ContextVar(
            "django_spec_agents.audit.execution", default=None
        )

    def get_ordering(self) -> CapabilityOrdering:
        """Pin audit as an **innermost** capability.

        pydantic-ai runs ``before_tool_execute`` hooks outermost first and
        ``on_tool_execute_error`` / ``after_tool_execute`` hooks innermost
        first, so innermost is the one position whose hooks sit directly either
        side of the tool: after every other capability's argument rewrite and
        veto, and before any other capability converts, recovers or rewrites
        the outcome, unless a capability sorts after audit even so (see the
        class). Declared here rather than left to list order at the
        ``build_agent`` call site, since pydantic-ai sorts by these
        constraints.
        """
        return CapabilityOrdering(position="innermost")

    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        handler: WrapToolExecuteHandler,
    ) -> Any:
        """Write the call's one record on the way out, and hand the result back as is.

        This wrapper sees every capability's hooks around the tool. The hooks
        below have recorded the tool's side first where they could, and
        ``settle`` keeps the first account it is given, so what is written is
        the tool's whenever nothing sorts between audit and the tool (see the
        class). A call whose ``before_tool_execute`` never ran here never
        reached the tool, and is not recorded.

        Every entry starts a fresh execution, because pydantic-ai lets an outer
        wrapper call its handler more than once and each call runs the tool
        again. ``before_tool_execute`` runs again inside each entry and says
        so itself, unless a capability ahead of audit stops that entry first.
        The reset on the way out is what keeps the previous entry's "reached"
        from carrying into one stopped that way, which would record a call
        that never ran. Coverage cannot see it go missing, since the line runs
        either way; ``test_a_rerun_refused_ahead_of_audit_is_not_recorded``
        fails without it.
        """
        execution = _Execution(call, reached=self._current(call).reached)
        token = self._execution.set(execution)
        execution.begin(args)
        ip_address = self._resolve_ip_address(ctx)
        try:
            try:
                result = await handler(args)
            except Exception as error:
                execution.settle(error=error)
                self._record(tool_def.name, execution, ip_address=ip_address)
                raise
            execution.settle(result=result)
            self._record(tool_def.name, execution, ip_address=ip_address)
            return result
        finally:
            self._execution.reset(token)

    async def before_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Note that the call reached the tool, with the arguments it gets.

        Innermost, so this runs after every other capability's
        ``before_tool_execute``, and nothing else stands between it and the
        tool, unless a capability sorts after audit (see the class).
        """
        execution = self._current(call)
        self._execution.set(execution)
        execution.reached = True
        execution.begin(args)
        return args

    async def on_tool_execute_error(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,
        error: Exception,
    ) -> Any:
        """Keep the tool's own exception, then pass it on unchanged.

        Innermost, so this is the first error hook to run, before any other
        capability converts or recovers, unless one sorts after audit (see the
        class). Re-raising is what keeps the chain going: the next
        capability's hook is handed the same exception.
        """
        self._current(call).settle(error=error)
        raise error

    async def after_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        result: Any,
    ) -> Any:
        """Keep the tool's own result, then pass it on unchanged.

        Innermost, so this is the first result hook to run, before any other
        capability rewrites the result, unless one sorts after audit (see the
        class). After a recovery it is handed the recovered value instead,
        which ``settle`` ignores: the error hook settled the call first.
        """
        self._current(call).settle(result=result)
        return result

    def _current(self, call: ToolCallPart) -> _Execution:
        """The execution this context holds for ``call``, else a fresh one.

        Both conditions are one branch arc, so coverage cannot see either go
        missing. These tests do:

        - ``execution is not None``: every recording test, starting with
          ``test_records_toolset_tools_not_just_registry_tools``, which would
          read ``.call`` off ``None`` in a fresh context.
        - ``execution.call is call``: a tool that runs an agent composed with
          this same capability, whose inner calls start in a context that
          already holds the outer call's execution. An inner call that a
          capability ahead of audit refuses with anything but a veto, such as a
          ``ModelRetry``, would take the outer call's "reached" and be recorded:
          ``test_a_nested_call_refused_ahead_of_audit_is_not_recorded``.
          Identity rather than ``tool_call_id``, which the model chooses.
        """
        execution = self._execution.get()
        if execution is not None and execution.call is call:
            return execution
        return _Execution(call)

    def _resolve_ip_address(self, ctx: RunContext[Any]) -> str | None:
        """This run's client IP: ``deps.ip_address``, else the constructed one.

        ``getattr`` because the deps type is the host's to choose: a project's
        own deps class, or ``None``, has no such field, and that is the fallback
        case rather than an error.
        """
        from_deps = getattr(ctx.deps, "ip_address", None)
        return from_deps if from_deps is not None else self._ip_address

    def _record(self, name: str, execution: _Execution, *, ip_address: str | None) -> None:
        if _did_not_execute(execution):
            return
        error = execution.error
        event = AuditEvent(
            tool_name=name,
            arguments_repr=json.dumps(execution.arguments, default=str, sort_keys=True),
            duration_ms=execution.duration_ms,
            success=error is None,
            error=None if error is None else f"{type(error).__name__}: {error}",
            result_size=len(str(execution.result)) if error is None else None,
            organization_id=self._organization_id,
            ip_address=ip_address,
        )
        try:
            self._logger.record(event)
        except Exception:
            _fallback_logger.exception(
                "audit logger %r raised while recording %r; event dropped",
                type(self._logger).__name__,
                name,
            )


def _did_not_execute(execution: _Execution) -> bool:
    """Whether the call never ran the tool, and so has no record.

    Two ways, either of which is enough:

    - **Nothing reached audit's ``before_tool_execute``.** A capability ahead
      of audit stopped the call with something other than a veto, such as a
      ``ModelRetry`` asking for different arguments, or any exception.
    - **The outcome is one of ``_NOT_EXECUTED``.** A deferral can be raised
      from inside the call, by the tool or by a toolset such as pydantic-ai's
      ``FunctionToolset.approval_required()``, so it reaches audit's wrapper.
      So does a veto from an innermost capability sorted after audit, such as
      one passed to a single run. A call deferred
      to external execution never runs in this process; one held for approval
      is recorded when it is resumed and runs.

    Each condition is one branch arc, the ``isinstance`` tuple included, so
    coverage cannot see any of them go missing. These tests fail without each:

    - ``not execution.reached``:
      ``test_a_retry_asked_for_before_the_tool_runs_is_not_recorded`` and
      ``test_a_before_or_after_hook_reaches_the_record_only_through_the_tool[before-reject]``.
    - ``SkipToolExecution``:
      ``test_a_veto_from_a_capability_passed_per_run_is_not_recorded`` and
      ``test_audit_composed_ahead_of_an_innermost_veto_does_not_record_it``.
    - ``CallDeferred``:
      ``test_a_call_deferred_to_external_execution_has_no_record``.
    - ``ApprovalRequired``:
      ``test_a_call_an_approval_required_toolset_defers_is_recorded_once_it_runs``
      and ``test_a_tool_asking_for_approval_itself_is_recorded_once_it_runs``.
    """
    return not execution.reached or isinstance(execution.error, _NOT_EXECUTED)


class _Execution:
    """What one audit capability has observed of one tool execution.

    Each hook adds what it saw, and only the wrapper reads it, so the order the
    installed pydantic-ai runs them in decides nothing but which observation is
    nearest the tool.
    """

    def __init__(self, call: ToolCallPart, *, reached: bool = False) -> None:
        self.call = call
        # Whether audit's ``before_tool_execute`` ran for this call, which is
        # the last moment before the tool: a veto ahead of it means no record.
        self.reached = reached
        # The run about to start, as ``begin`` last saw it.
        self.args: dict[str, Any] = {}
        self.started = 0.0
        # What the record holds, all of it fixed by ``settle`` at once.
        self.settled = False
        self.arguments: dict[str, Any] = {}
        self.duration_ms = 0.0
        self.error: Exception | None = None
        self.result: Any = None

    def begin(self, args: dict[str, Any]) -> None:
        """The tool is about to run with ``args``.

        The wrapper and ``before_tool_execute`` both call this, and the later
        of the two is nearer the tool, so each call replaces the last. A call
        after ``settle`` changes nothing the record holds, which ``settle``
        has already taken.
        """
        self.args = args
        self.started = time.perf_counter()

    def settle(self, *, error: Exception | None = None, result: Any = None) -> None:
        """The tool's outcome, if no earlier observation has settled it.

        The first account is the one nearest the tool: audit's own error or
        result hook where pydantic-ai calls one, else the wrapper.

        The arguments and the duration are taken here with the outcome, so a
        run audit's own hooks settled is recorded with its own arguments and
        time. A wrapper sorted after audit that runs the tool again
        runs audit's ``before_tool_execute`` again, which calls ``begin`` after
        this: read when the wrapper writes the record, they would pair the
        first run's outcome with the second run's arguments and a start later
        than its end, a negative duration. Where audit's hooks never see the
        first run's outcome, as with a tool's own ``ModelRetry``, nothing has
        settled when the rerun begins, and the record is of the later run.
        ``test_a_capability_sorted_after_audit_reaches_the_record`` fails
        without it, on ``[wrapper-reruns-per-run]``,
        ``[wrapper-reruns-a-success-per-run]`` and their
        ``config-ordered-inside-audit`` twins.
        """
        if self.settled:
            return
        self.settled = True
        self.arguments = self.args
        self.duration_ms = (time.perf_counter() - self.started) * 1000.0
        self.error = error
        self.result = result


__all__ = ["AuditCapability"]
