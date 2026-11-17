from __future__ import annotations

import pytest

from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.contrib.store.default_inbound_queue import DefaultInboundQueue
from django_spec_agents.contrib.store.models import StoredInboundMessage
from django_spec_agents.persistence.anonymous_operation_error import AnonymousOperationError
from django_spec_agents.persistence.identifier_too_long_error import IdentifierTooLongError
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from tests.contrib.store.rows import too_long
from tests.request_helpers import anonymous_request, authed_request

pytestmark = pytest.mark.django_db(transaction=True)

_PARENT = ThreadRef(thread_id="t1", scope="a")
_CHILD = ThreadRef(thread_id="c1", scope="b")


async def test_a_message_survives_a_restart() -> None:
    request = authed_request("7")
    sent = await DefaultInboundQueue().enqueue(
        _PARENT, InboundKind.REPORT, {"text": "done"}, request=request, sender=_CHILD
    )

    [message] = await DefaultInboundQueue().pending(_PARENT, request=request)

    assert message.id == sent
    assert message.kind is InboundKind.REPORT
    assert message.body == {"text": "done"}
    assert message.sender == _CHILD
    assert message.created_at is not None


async def test_pending_is_oldest_first_and_reads_without_removing() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    first = await queue.enqueue(_PARENT, InboundKind.REPORT, 1, request=request)
    second = await queue.enqueue(_PARENT, InboundKind.STEER, 2, request=request)

    assert [m.id for m in await queue.pending(_PARENT, request=request)] == [first, second]
    assert [m.body for m in await queue.pending(_PARENT, request=request)] == [1, 2]
    assert [m.sender for m in await queue.pending(_PARENT, request=request)] == [None, None]


async def test_after_and_limit_page_the_queue() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    ids = [await queue.enqueue(_PARENT, InboundKind.REPORT, n, request=request) for n in range(3)]

    page = await queue.pending(_PARENT, request=request, after=ids[0], limit=1)

    assert [m.id for m in page] == [ids[1]]


async def test_ack_removes_through_the_id_and_no_further() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    ids = [await queue.enqueue(_PARENT, InboundKind.REPORT, n, request=request) for n in range(3)]

    removed = await queue.ack(_PARENT, ids[1], request=request)

    assert removed == 2
    assert [m.id for m in await queue.pending(_PARENT, request=request)] == [ids[2]]


async def test_threads_of_one_id_in_two_scopes_have_two_queues() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    other = ThreadRef(thread_id=_PARENT.thread_id, scope="b")
    unscoped = ThreadRef(thread_id=_PARENT.thread_id)
    await queue.enqueue(_PARENT, InboundKind.REPORT, "a", request=request)
    await queue.enqueue(other, InboundKind.REPORT, "b", request=request)
    await queue.enqueue(unscoped, InboundKind.REPORT, "none", request=request)

    assert [m.body for m in await queue.pending(other, request=request)] == ["b"]
    assert await queue.ack(unscoped, 10**9, request=request) == 1
    assert [m.body for m in await queue.pending(_PARENT, request=request)] == ["a"]


async def test_an_owner_never_reads_or_acks_another_owners_queue() -> None:
    queue = DefaultInboundQueue()
    sent = await queue.enqueue(_PARENT, InboundKind.QUESTION, "?", request=authed_request("7"))

    assert await queue.pending(_PARENT, request=authed_request("8")) == []
    assert await queue.ack(_PARENT, sent, request=authed_request("8")) == 0
    assert len(await queue.pending(_PARENT, request=authed_request("7"))) == 1


async def test_an_anonymous_request_is_refused_unless_allowed() -> None:
    with pytest.raises(AnonymousOperationError):
        await DefaultInboundQueue().enqueue(
            _PARENT, InboundKind.ANSWER, "yes", request=anonymous_request()
        )
    assert not await StoredInboundMessage.objects.aexists()


@pytest.mark.parametrize("field", ["thread_id", "scope", "sender_thread_id", "sender_scope"])
async def test_an_over_long_id_is_refused_with_nothing_written(field: str) -> None:
    value = too_long(StoredInboundMessage, field)
    thread = ThreadRef(
        thread_id=value if field == "thread_id" else "t1",
        scope=value if field == "scope" else "a",
    )
    sender = ThreadRef(
        thread_id=value if field == "sender_thread_id" else "c1",
        scope=value if field == "sender_scope" else "b",
    )

    with pytest.raises(IdentifierTooLongError) as error:
        await DefaultInboundQueue().enqueue(
            thread, InboundKind.REPORT, None, request=authed_request("7"), sender=sender
        )

    assert error.value.field == field
    assert not await StoredInboundMessage.objects.aexists()
