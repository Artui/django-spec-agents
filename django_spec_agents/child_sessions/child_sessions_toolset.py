from __future__ import annotations

from typing import Any

from django.http import HttpRequest
from pydantic_ai import CallDeferred
from pydantic_ai.tools import RunContext, ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from django_spec_agents.child_sessions.child_lineage import ChildLineage
from django_spec_agents.child_sessions.child_session_budgets import child_session_budgets
from django_spec_agents.child_sessions.types.child_session import ChildSession
from django_spec_agents.child_sessions.types.child_session_index import ChildSessionIndex
from django_spec_agents.child_sessions.types.child_session_limits import ChildSessionLimits
from django_spec_agents.child_sessions.types.child_session_receipt import ChildSessionReceipt
from django_spec_agents.child_sessions.types.child_session_refusal import ChildSessionRefusal
from django_spec_agents.child_sessions.types.child_session_spend import ChildSessionSpend
from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.types.inbound_queue import InboundQueue
from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
from django_spec_agents.persistence.types.thread_ref import ThreadRef

_LINEAGE_ID = "child_session_lineage"
_START_DESCRIPTION = (
    "Start a child session on another agent, which runs in its own thread and reports back."
)


class ChildSessionsToolset(FunctionToolset[Any]):
    """Tools to start, list, message, answer and cancel child sessions, and for a child to ask.

    ``start_session`` starts a child on one of ``agents`` (an id to a description the
    model reads) through ``runner``, as the run's own user, and returns a
    [`ChildSessionReceipt`][django_spec_agents.child_sessions.types.child_session_receipt.ChildSessionReceipt]
    naming its thread. ``list_tasks``, ``message_task``, ``answer_task`` and
    ``cancel_task`` keep upstream ``TaskControl``'s names, so a mount carrying both is
    refused by pydantic-ai's duplicate-name check; ``ask_parent`` is offered in a child
    only, and parks it as waiting on a question.

    Needs the ``[harness]`` extra. Starts run one at a time, so each reads the index
    after the last; an anonymous run is refused. A start passes the runner ``lineage=``
    alone, from which it composes
    [`child_lineage_for`][django_spec_agents.child_lineage_for]; with ``spend``, an
    exhausted budget refuses it first. Each limit in ``limits`` refuses a start with a
    [`ChildSessionRefusal`][django_spec_agents.child_sessions.types.child_session_refusal.ChildSessionRefusal].

    The run's thread is ``ctx.conversation_id`` and its request is
    ``ctx.deps.call_context["request"]``, as a transport's default deps put it there.
    """

    def __init__(
        self,
        agent_id: str,
        *,
        agents: dict[str, str],
        runner: HeadlessRunner,
        index: ChildSessionIndex,
        queue: InboundQueue,
        limits: ChildSessionLimits,
        spend: ChildSessionSpend | None = None,
    ) -> None:
        import pydantic_ai_harness.sub_agents  # noqa: F401

        super().__init__(id="child_sessions")
        self._agent_id = agent_id
        self._agents = dict(agents)
        self._runner = runner
        self._index = index
        self._queue = queue
        self._limits = limits
        self._spend = spend
        listed = "\n".join(f"- {key}: {text}" for key, text in sorted(self._agents.items()))
        self.add_function(
            self.start_session,
            name="start_session",
            description=f"{_START_DESCRIPTION}\n\nThe agents:\n{listed}",
            sequential=True,
        )
        self.add_function(self.list_tasks, name="list_tasks")
        self.add_function(self.message_task, name="message_task")
        self.add_function(self.answer_task, name="answer_task")
        self.add_function(self.cancel_task, name="cancel_task")
        self.add_function(self.ask_parent, name="ask_parent", prepare=_offer_to_children)

    async def start_session(
        self, ctx: RunContext[Any], agent: str, prompt: str
    ) -> ChildSessionReceipt | ChildSessionRefusal:
        """Start a child session; its description is set where the toolset is built.

        Args:
            agent: The id of the agent to start, one of those listed in this tool's description.
            prompt: The child's task, complete in itself: it sees nothing of this conversation.
        """
        if agent not in self._agents:
            known = ", ".join(sorted(self._agents)) or "none"
            return ChildSessionRefusal("agent", f"no agent {agent!r}; the agents are: {known}")
        if not getattr(ctx.deps.user, "is_authenticated", False):
            return ChildSessionRefusal("principal", "a child session needs a signed-in user")
        request, here = _request(ctx), self._here(ctx)
        lineage = _lineage(ctx)
        depth = 1 if lineage is None else lineage.depth + 1
        if depth > self._limits.max_depth:
            return ChildSessionRefusal("max_depth", f"a child here would be {depth} deep")
        siblings = await self._index.children(here, request=request)
        if sum(child.live for child in siblings) >= self._limits.max_running_per_thread:
            return ChildSessionRefusal("max_running_per_thread", "this thread's children are busy")
        if await self._index.live_count(request=request) >= self._limits.max_running_per_user:
            return ChildSessionRefusal("max_running_per_user", "your child sessions are busy")
        root = here if lineage is None else lineage.root
        tools, gated = _tool_names(ctx), _tool_names(ctx, "unapproved")
        if self._spend is not None:
            budgets = child_session_budgets(self._spend, root, _owner(ctx))
            if refused := await _exhausted(budgets):
                return ChildSessionRefusal(refused, "the spend budget is exhausted")
        started = await self._runner.start(
            agent,
            prompt,
            ctx.deps.user,
            timeout=self._limits.timeout,
            parent=here,
            lineage=ChildLineageRecord(root, depth, sorted(tools), sorted(gated)),
        )
        return ChildSessionReceipt(
            agent_id=agent, thread_id=started.thread_id, run_id=started.run_id
        )

    async def list_tasks(self, ctx: RunContext[Any]) -> list[ChildSession]:
        """List the child sessions this conversation started, and where each stands."""
        return await self._index.children(self._here(ctx), request=_request(ctx))

    async def message_task(
        self, ctx: RunContext[Any], thread_id: str, message: str
    ) -> str | ChildSessionRefusal:
        """Send a child session more instructions, which it reads at its next step.

        Args:
            thread_id: The child's thread id, from its receipt or ``list_tasks``.
            message: What to tell it.
        """
        return await self._send(ctx, thread_id, InboundKind.STEER, message)

    async def answer_task(
        self, ctx: RunContext[Any], thread_id: str, answer: str
    ) -> str | ChildSessionRefusal:
        """Answer the question a child session asked, so it can go on.

        Args:
            thread_id: The child's thread id, from its question or ``list_tasks``.
            answer: The answer.
        """
        return await self._send(ctx, thread_id, InboundKind.ANSWER, answer)

    async def cancel_task(self, ctx: RunContext[Any], thread_id: str) -> str | ChildSessionRefusal:
        """Cancel a child session and every session it started in turn.

        Args:
            thread_id: The child's thread id, from its receipt or ``list_tasks``.
        """
        request = _request(ctx)
        child = await self._child(ctx, thread_id, request)
        if child is None:
            return _not_a_child(thread_id)
        cancelled = await self._cancel_tree(child.thread, ctx.deps.user, request)
        return f"cancelled {cancelled} running session(s)"

    async def ask_parent(self, ctx: RunContext[Any], question: str) -> str:
        """Ask the conversation that started this one a question, and wait for its answer.

        Args:
            question: The question, complete in itself.
        """
        lineage = _lineage(ctx)
        assert lineage is not None
        await self._queue.enqueue(
            lineage.parent,
            InboundKind.QUESTION,
            {"text": question},
            request=_request(ctx),
            sender=self._here(ctx),
        )
        raise CallDeferred({"question": question}, kind="question")

    def _here(self, ctx: RunContext[Any]) -> ThreadRef:
        return ThreadRef(str(ctx.conversation_id), self._agent_id)

    async def _child(
        self, ctx: RunContext[Any], thread_id: str, request: HttpRequest
    ) -> ChildSession | None:
        children = await self._index.children(self._here(ctx), request=request)
        return next((c for c in children if c.thread.thread_id == thread_id), None)

    async def _send(
        self, ctx: RunContext[Any], thread_id: str, kind: InboundKind, text: str
    ) -> str | ChildSessionRefusal:
        request = _request(ctx)
        child = await self._child(ctx, thread_id, request)
        if child is None:
            return _not_a_child(thread_id)
        await self._queue.enqueue(
            child.thread, kind, {"text": text}, request=request, sender=self._here(ctx)
        )
        return f"sent to {thread_id}"

    async def _cancel_tree(self, thread: ThreadRef, user: Any, request: HttpRequest) -> int:
        cancelled = 0
        for child in await self._index.children(thread, request=request):
            cancelled += await self._cancel_tree(child.thread, user, request)
        return cancelled + await self._runner.cancel(thread.thread_id, user)


async def _offer_to_children(
    ctx: RunContext[Any], tool_def: ToolDefinition
) -> ToolDefinition | None:
    return tool_def if _lineage(ctx) is not None else None


def _lineage(ctx: RunContext[Any]) -> ChildLineage | None:
    found = ctx.capabilities.get(_LINEAGE_ID)
    return found if isinstance(found, ChildLineage) else None


def _request(ctx: RunContext[Any]) -> HttpRequest:
    request = getattr(ctx.deps, "call_context", {}).get("request")
    if not isinstance(request, HttpRequest):
        raise RuntimeError('child sessions need the run\'s request in call_context["request"]')
    return request


def _tool_names(ctx: RunContext[Any], kind: str | None = None) -> frozenset[str]:
    tools = (ctx.tool_manager.tools if ctx.tool_manager is not None else None) or {}
    return frozenset(n for n, t in tools.items() if kind in (None, t.tool_def.kind))


def _owner(ctx: RunContext[Any]) -> str:
    return str(ctx.deps.user.pk)


def _not_a_child(thread_id: str) -> ChildSessionRefusal:
    return ChildSessionRefusal("thread_id", f"{thread_id!r} is not a child of this conversation")


async def _exhausted(budgets: list[Any]) -> str | None:
    for limits in budgets:
        scope = limits.budgets[0].scope(None)
        if await limits.exhausted(scope=scope):
            return limits.id
    return None


__all__ = ["ChildSessionsToolset"]
