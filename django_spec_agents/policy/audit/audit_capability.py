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
    **every** tool the agent runs: registry tools, the MCP and spec
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
        # What this capability has seen of the tool call in flight.
        self._execution: ContextVar[_Execution] = ContextVar("django_spec_agents.audit.execution")

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

        Every entry starts a fresh, unreached execution, because pydantic-ai
        lets an outer wrapper call its handler more than once and each call
        runs the tool again. ``before_tool_execute`` runs again inside each
        entry and says so itself, unless a capability ahead of audit stops
        that entry first; an entry stopped that way is not recorded, whatever
        an earlier entry, or an outer call of a nested run, reached.

        The reset on the way out puts back what the context held before. Code
        mode awaits a nested call inline, in ``run_code``'s own task, when the
        nested tool is marked sequential or the run is, so that call's wrapper
        sets the variable in the context ``run_code``'s own hooks read. Without
        the reset they would settle the nested call's execution and leave
        ``run_code``'s to its wrapper, whose record then describes another
        capability's rewrite of the result and times its hooks.
        ``test_a_code_mode_call_keeps_its_own_record_around_inline_nested_calls``
        fails without it. Coverage cannot see it go missing, since the line
        runs either way.
        """
        execution = _Execution()
        token = self._execution.set(execution)
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
        tool, unless a capability sorts after audit (see the class). It runs
        inside the wrapper, so the execution is the wrapper's own for this call.
        """
        execution = self._execution.get()
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
        self._execution.get().settle(error=error)
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
        self._execution.get().settle(result=result)
        return result

    def _resolve_ip_address(self, ctx: RunContext[Any]) -> str | None:
        """This run's client IP: ``deps.ip_address``, else the constructed one."""
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
    """Whether the call never ran the tool, and so has no record."""
    return not execution.reached or isinstance(execution.error, _NOT_EXECUTED)


class _Execution:
    """What one audit capability has observed of one tool execution."""

    def __init__(self) -> None:
        # Set when audit's ``before_tool_execute`` ran; a veto ahead of it means no record.
        self.reached = False
        self.args: dict[str, Any] = {}
        self.started = 0.0
        self.settled = False
        self.arguments: dict[str, Any] = {}
        self.duration_ms = 0.0
        self.error: Exception | None = None
        self.result: Any = None

    def begin(self, args: dict[str, Any]) -> None:
        """The tool is about to run with ``args``."""
        self.args = args
        self.started = time.perf_counter()

    def settle(self, *, error: Exception | None = None, result: Any = None) -> None:
        """The tool's outcome, if no earlier observation has settled it."""
        if self.settled:
            return
        self.settled = True
        self.arguments = self.args
        self.duration_ms = (time.perf_counter() - self.started) * 1000.0
        self.error = error
        self.result = result


__all__ = ["AuditCapability"]
