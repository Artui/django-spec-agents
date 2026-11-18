from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.http import HttpRequest

from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.child_sessions.types.inbound_message import InboundMessage
from django_spec_agents.child_sessions.types.inbound_queue import InboundQueue
from django_spec_agents.child_sessions.wake_thread import wake_thread
from django_spec_agents.headless.types.headless_runner import HeadlessRunner
from django_spec_agents.persistence.types.conversation_store import ConversationStore
from django_spec_agents.persistence.types.run_store import RunStore
from django_spec_agents.persistence.types.thread_ref import ThreadRef

logger = logging.getLogger(__name__)


@dataclass
class WakingInboundQueue:
    """An [`InboundQueue`][django_spec_agents.child_sessions.types.inbound_queue.InboundQueue]
    that wakes the thread it enqueues for, through
    [`wake_thread`][django_spec_agents.child_sessions.wake_thread.wake_thread].

    Pass it where the toolset and ``child_lineage_for`` take a queue: a child's report and
    question then wake an idle parent, and ``message_task`` or ``answer_task`` wakes a
    settled child. ``conversations`` maps an agent id to the store its mount reads, so a
    child's thread wakes with its stored ``parent`` and ``lineage``. A wake that fails is
    logged and the message stays queued for the thread's next run.
    """

    queue: InboundQueue
    runner: HeadlessRunner
    runs: RunStore
    timeout: float
    conversations: Mapping[str, ConversationStore] = field(default_factory=dict)

    async def enqueue(
        self,
        thread: ThreadRef,
        kind: InboundKind,
        body: Any,
        *,
        request: HttpRequest,
        sender: ThreadRef | None = None,
    ) -> int:
        queued = await self.queue.enqueue(thread, kind, body, request=request, sender=sender)
        try:
            await wake_thread(
                thread,
                request=request,
                runner=self.runner,
                runs=self.runs,
                queue=self.queue,
                timeout=self.timeout,
                conversations=self.conversations.get(thread.scope or ""),
            )
        except Exception:
            logger.exception("Could not wake %s after enqueueing message %s.", thread, queued)
        return queued

    async def pending(
        self, thread: ThreadRef, *, request: HttpRequest, after: int = 0, limit: int | None = None
    ) -> list[InboundMessage]:
        return await self.queue.pending(thread, request=request, after=after, limit=limit)

    async def ack(self, thread: ThreadRef, ids: Iterable[int], *, request: HttpRequest) -> int:
        return await self.queue.ack(thread, ids, request=request)

    async def take(
        self, thread: ThreadRef, run_id: str, *, request: HttpRequest, lease: timedelta
    ) -> list[InboundMessage]:
        return await self.queue.take(thread, run_id, request=request, lease=lease)

    async def release(self, thread: ThreadRef, run_id: str, *, request: HttpRequest) -> bool:
        return await self.queue.release(thread, run_id, request=request)


__all__ = ["WakingInboundQueue"]
