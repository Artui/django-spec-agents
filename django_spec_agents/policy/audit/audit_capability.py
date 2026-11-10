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
    """Records every tool execution to an [`AuditLogger`][django_spec_agents.AuditLogger] sink."""

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
        """Pin audit as an **innermost** capability."""
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
        """Write the call's one record on the way out, and hand the result back as is."""
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
        """Note that the call reached the tool, with the arguments it gets."""
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
        """Keep the tool's own exception, then pass it on unchanged."""
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
        """Keep the tool's own result, then pass it on unchanged."""
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
