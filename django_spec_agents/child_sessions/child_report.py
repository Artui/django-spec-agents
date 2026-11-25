from __future__ import annotations

import asyncio
import logging
from dataclasses import KW_ONLY, dataclass
from typing import Any

from django.http import HttpRequest
from pydantic_ai import DeferredToolRequests
from pydantic_ai.capabilities import AbstractCapability, WrapRunHandler
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import RunContext

from django_spec_agents.child_sessions.report_frame import report_frame
from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.types.inbound_queue import InboundQueue
from django_spec_agents.child_sessions.types.report_outcome import ReportOutcome
from django_spec_agents.persistence.types.thread_ref import ThreadRef

logger = logging.getLogger(__name__)

_USER_JOINED = "The user joined this session, so this result may reflect their direction."


@dataclass
class ChildReport(AbstractCapability[Any]):
    """Enqueues a child run's report on its parent's inbound queue when the run settles.

    Needs ``[harness]``, checked when built. A run that parks, or is cancelled before its
    deadline, sends nothing; a failed enqueue is logged and never replaces the run's own.
    A spend limit reports ``budget``, as a usage limit does, and a ``DetectLoops`` stop
    ``failed``, its text naming the loop.
    With ``user_joined``, the report's framed body ends with a line telling the parent that
    the user joined the child's session, so its result may reflect the user's direction.
    """

    parent: ThreadRef
    agent_id: str
    queue: InboundQueue
    _: KW_ONLY
    id: str | None = "child_session_report"
    user_joined: bool = False

    def __post_init__(self) -> None:
        import pydantic_ai_harness.sub_agents  # noqa: F401

    async def wrap_run(
        self, ctx: RunContext[Any], *, handler: WrapRunHandler
    ) -> AgentRunResult[Any]:
        try:
            result = await handler()
        except asyncio.CancelledError:
            remaining = ctx.remaining_time()
            if remaining is not None and remaining <= 0:
                await self._report(ctx, "", "timeout")
            raise
        except UsageLimitExceeded as error:
            await self._report(ctx, str(error), "budget")
            raise
        except Exception as error:
            await self._report(ctx, _failure(error), "failed")
            raise
        if not isinstance(result.output, DeferredToolRequests):
            await self._report(ctx, str(result.output), "ok")
        return result

    async def _report(self, ctx: RunContext[Any], output: str, outcome: ReportOutcome) -> None:
        child = ThreadRef(str(ctx.conversation_id), self.agent_id)
        try:
            if self.user_joined:
                output = f"{output}\n\n{_USER_JOINED}"
            frame = report_frame(child, agent_id=self.agent_id, output=output, outcome=outcome)
            request = getattr(ctx.deps, "call_context", {}).get("request")
            if not isinstance(request, HttpRequest):
                raise RuntimeError('no request in call_context["request"]')
            await self.queue.enqueue(
                self.parent,
                InboundKind.REPORT,
                {"text": _text(frame.content), "outcome": outcome, "output": output},
                request=request,
                sender=child,
            )
        except Exception:
            logger.exception("Child %s could not report to %s.", child, self.parent)


def _failure(error: Exception) -> str:
    from pydantic_ai_harness.detect_loops import LoopDetectedError

    if isinstance(error, LoopDetectedError):
        return f"The run was stopped in a loop ({error.loop.pattern}): {error}"
    return str(error)


def _text(content: Any) -> str:
    return "".join(part.content for part in content)


__all__ = ["ChildReport"]
