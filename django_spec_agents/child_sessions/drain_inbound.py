from __future__ import annotations

from dataclasses import KW_ONLY, dataclass, field, replace
from datetime import timedelta
from typing import Any

from django.http import HttpRequest
from pydantic_ai import DeferredToolRequests
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.capabilities.abstract import AgentNode, NodeResult, WrapRunHandler
from pydantic_ai.framing import register_tag, wrap
from pydantic_ai.messages import ModelResponse, TextContent, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import RunContext
from pydantic_graph import End

from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.types.inbound_message import InboundMessage
from django_spec_agents.child_sessions.types.inbound_queue import InboundQueue
from django_spec_agents.child_sessions.types.message_origin import MessageOrigin
from django_spec_agents.child_sessions.utils import task_metadata
from django_spec_agents.persistence.types.thread_ref import ThreadRef

_TASK_QUESTION = register_tag("task_question")


@dataclass
class DrainInbound(AbstractCapability[Any]):
    """Hands a thread's inbound messages to its model: at each model request, and before it ends.

    Compose it into every run of a mount whose threads receive reports, questions, answers or
    steering, with the mount's ``agent_id`` as the thread's scope. A report arrives as the
    ``<task_output>`` text its child framed, a question as ``<task_question>`` data naming the
    asking thread, an answer or steering as the parent's plain words. A report's and a
    question's ``TextContent.metadata`` holds ``origin`` and ``task``; an answer and steering
    carry none. A message that arrives after the last request is delivered by a further
    request, unless the run parks: then it waits for the resume.

    The run takes the thread's inbox with ``queue.take`` under its ``run_id``, renewing its
    claim for ``lease`` at each take, so one run holds a thread's inbox at a time and sees
    each message once. It acks each message once the model request carrying it is answered,
    and releases the inbox when it ends, parks or fails, handing on what it took and never
    acked; the messages of a run that dies wait until its lease lapses. A run with no
    signed-in user in ``call_context["request"]`` reads nothing.
    """

    queue: InboundQueue
    agent_id: str
    _: KW_ONLY
    id: str | None = "child_session_inbound"
    lease: timedelta = timedelta(minutes=5)
    _unacked: list[int] = field(default_factory=list, init=False, repr=False)

    async def for_run(self, ctx: RunContext[Any]) -> DrainInbound:
        return replace(self)

    async def wrap_run(
        self, ctx: RunContext[Any], *, handler: WrapRunHandler
    ) -> AgentRunResult[Any]:
        try:
            return await handler()
        finally:
            if (request := _request(ctx)) is not None:
                await self.queue.release(self._here(ctx), str(ctx.run_id), request=request)

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if parts := await self._take(ctx):
            ctx.enqueue(*parts, priority="asap")
        return request_context

    async def after_model_request(
        self, ctx: RunContext[Any], *, request_context: ModelRequestContext, response: ModelResponse
    ) -> ModelResponse:
        request = _request(ctx)
        if self._unacked and request is not None:
            await self.queue.ack(self._here(ctx), self._unacked, request=request)
            self._unacked = []
        return response

    async def after_node_run(
        self, ctx: RunContext[Any], *, node: AgentNode[Any], result: NodeResult[Any]
    ) -> NodeResult[Any]:
        ending = isinstance(result, End) and not isinstance(
            result.data.output, DeferredToolRequests
        )
        if ending and (parts := await self._take(ctx)):
            ctx.enqueue(*parts, priority="asap")
        return result

    async def _take(self, ctx: RunContext[Any]) -> list[UserPromptPart]:
        request = _request(ctx)
        if request is None:
            return []
        here, run_id = self._here(ctx), str(ctx.run_id)
        fresh = await self.queue.take(here, run_id, request=request, lease=self.lease)
        self._unacked.extend(message.id for message in fresh)
        return [_part(message) for message in fresh]

    def _here(self, ctx: RunContext[Any]) -> ThreadRef:
        return ThreadRef(str(ctx.conversation_id), self.agent_id)


def _request(ctx: RunContext[Any]) -> HttpRequest | None:
    request = getattr(ctx.deps, "call_context", {}).get("request")
    if not isinstance(request, HttpRequest):
        return None
    user = getattr(request, "user", None)
    return request if getattr(user, "is_authenticated", False) else None


def _part(message: InboundMessage) -> UserPromptPart:
    text = str(message.body.get("text", ""))
    if message.kind is InboundKind.QUESTION:
        sender = message.sender or ThreadRef("")
        text = wrap(
            _TASK_QUESTION, f"\n{text}\n", task_id=sender.thread_id, agent_name=sender.scope
        )
    return UserPromptPart([TextContent(text, metadata=_metadata(message))])


def _metadata(message: InboundMessage) -> dict[str, Any] | None:
    sender = message.sender
    agent_name = None if sender is None else sender.scope
    if message.kind is InboundKind.REPORT:
        return task_metadata(
            MessageOrigin.TASK_OUTPUT,
            sender,
            agent_name=agent_name,
            body=message.body.get("output"),
            outcome=message.body.get("outcome"),
        )
    if message.kind is InboundKind.QUESTION:
        return task_metadata(
            MessageOrigin.TASK_QUESTION,
            sender,
            agent_name=agent_name,
            body=message.body.get("text"),
            question_id=message.body.get("question_id"),
        )
    return None


__all__ = ["DrainInbound"]
