from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
from asgiref.sync import async_to_sync
from django.db import IntegrityError, connection
from django.test.utils import CaptureQueriesContext

from django_spec_agents.child_sessions.types.inbound_kind import InboundKind
from django_spec_agents.contrib.store.default_inbound_queue import DefaultInboundQueue
from django_spec_agents.contrib.store.models import StoredInboundMessage, StoredInboxClaim
from django_spec_agents.persistence.anonymous_operation_error import AnonymousOperationError
from django_spec_agents.persistence.identifier_too_long_error import IdentifierTooLongError
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from tests.contrib.store.rows import too_long
from tests.request_helpers import anonymous_request, authed_request

pytestmark = pytest.mark.django_db(transaction=True)

_PARENT = ThreadRef(thread_id="t1", scope="a")
_CHILD = ThreadRef(thread_id="c1", scope="b")
_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_LEASE = timedelta(seconds=30)


class _Clock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now


async def _bodies(queue: DefaultInboundQueue, run_id: str, thread: ThreadRef = _PARENT) -> list:
    taken = await queue.take(thread, run_id, request=authed_request("7"), lease=_LEASE)
    return [message.body for message in taken]


async def _send(queue: DefaultInboundQueue, body: str, thread: ThreadRef = _PARENT) -> int:
    return await queue.enqueue(thread, InboundKind.REPORT, body, request=authed_request("7"))


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


async def test_ack_removes_the_ids_it_is_given_and_no_others() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    ids = [await queue.enqueue(_PARENT, InboundKind.REPORT, n, request=request) for n in range(3)]

    removed = await queue.ack(_PARENT, ids[:2], request=request)

    assert removed == 2
    assert [m.id for m in await queue.pending(_PARENT, request=request)] == [ids[2]]


async def test_a_lower_id_that_commits_after_pending_survives_an_ack_of_what_was_read() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    row = {"owner_id": "7", "scope": "a", "thread_id": "t1", "kind": "report"}
    await StoredInboundMessage.objects.acreate(id=100, **row)
    read = await queue.pending(_PARENT, request=request)
    late = await StoredInboundMessage.objects.acreate(id=50, **row)

    removed = await queue.ack(_PARENT, [m.id for m in read], request=request)

    assert (removed, [m.id for m in read]) == (1, [100])
    assert [m.id for m in await queue.pending(_PARENT, request=request)] == [late.pk]


async def test_acking_no_ids_removes_nothing() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    await queue.enqueue(_PARENT, InboundKind.REPORT, 1, request=request)

    assert await queue.ack(_PARENT, [], request=request) == 0
    assert len(await queue.pending(_PARENT, request=request)) == 1


async def test_threads_of_one_id_in_two_scopes_have_two_queues() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    other = ThreadRef(thread_id=_PARENT.thread_id, scope="b")
    unscoped = ThreadRef(thread_id=_PARENT.thread_id)
    await queue.enqueue(_PARENT, InboundKind.REPORT, "a", request=request)
    await queue.enqueue(other, InboundKind.REPORT, "b", request=request)
    none = await queue.enqueue(unscoped, InboundKind.REPORT, "none", request=request)

    assert [m.body for m in await queue.pending(other, request=request)] == ["b"]
    assert await queue.ack(unscoped, [none], request=request) == 1
    assert [m.body for m in await queue.pending(_PARENT, request=request)] == ["a"]


async def test_two_threads_of_one_scope_and_owner_have_two_queues() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    first, second = ThreadRef(thread_id="t1", scope="a"), ThreadRef(thread_id="t2", scope="a")
    one = await queue.enqueue(first, InboundKind.REPORT, "one", request=request)
    kept = await queue.enqueue(second, InboundKind.REPORT, "two", request=request)

    assert [m.body for m in await queue.pending(first, request=request)] == ["one"]
    assert [m.body for m in await queue.pending(second, request=request)] == ["two"]
    assert await queue.ack(first, [one, kept], request=request) == 1
    assert [m.id for m in await queue.pending(second, request=request)] == [kept]


async def test_an_absent_scope_and_an_empty_scope_have_two_queues() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    absent, empty = ThreadRef(thread_id="t1"), ThreadRef(thread_id="t1", scope="")
    first = await queue.enqueue(absent, InboundKind.REPORT, "absent", request=request)
    second = await queue.enqueue(empty, InboundKind.REPORT, "empty", request=request)

    assert [m.body for m in await queue.pending(absent, request=request)] == ["absent"]
    assert [m.body for m in await queue.pending(empty, request=request)] == ["empty"]
    assert await queue.ack(absent, [first, second], request=request) == 1
    assert [m.body for m in await queue.pending(empty, request=request)] == ["empty"]


async def test_an_owner_never_reads_or_acks_another_owners_queue() -> None:
    queue = DefaultInboundQueue()
    sent = await queue.enqueue(_PARENT, InboundKind.QUESTION, "?", request=authed_request("7"))

    assert await queue.pending(_PARENT, request=authed_request("8")) == []
    assert await queue.ack(_PARENT, [sent], request=authed_request("8")) == 0
    assert len(await queue.pending(_PARENT, request=authed_request("7"))) == 1


async def test_an_anonymous_request_is_refused() -> None:
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


async def test_take_claims_the_inbox_and_hands_a_run_each_message_once() -> None:
    queue = DefaultInboundQueue()
    await _send(queue, "first")
    await _send(queue, "second")

    assert await _bodies(queue, "r1") == ["first", "second"]
    assert await _bodies(queue, "r1") == []
    await _send(queue, "third")
    assert await _bodies(queue, "r1") == ["third"]
    pending = await queue.pending(_PARENT, request=authed_request("7"))
    assert [m.body for m in pending] == ["first", "second", "third"]
    claim = await StoredInboxClaim.objects.aget()
    assert (claim.owner_id, claim.scope, claim.thread_id, claim.run_id) == ("7", "a", "t1", "r1")


async def test_a_second_run_takes_nothing_while_the_first_holds_the_inbox() -> None:
    queue = DefaultInboundQueue()
    await _bodies(queue, "r1")
    await _send(queue, "news")

    assert await _bodies(queue, "r2") == []
    assert await _bodies(queue, "r1") == ["news"]


async def test_two_runs_racing_for_one_inbox_one_wins() -> None:
    queue = DefaultInboundQueue()
    await _send(queue, "news")

    first, second = await asyncio.gather(_bodies(queue, "r1"), _bodies(queue, "r2"))

    assert sorted([first, second]) == [[], ["news"]]
    assert await StoredInboxClaim.objects.acount() == 1


@pytest.mark.skipif(connection.vendor != "postgresql", reason="SQLite serialises its writers")
def test_two_connections_racing_for_one_inbox_one_wins() -> None:
    queue = DefaultInboundQueue()
    async_to_sync(_send)(queue, "news")
    start = threading.Barrier(2)
    won: dict[str, list] = {}

    def take(run_id: str) -> None:
        start.wait(timeout=5)
        won[run_id] = async_to_sync(_bodies)(queue, run_id)
        connection.close()

    racers = [threading.Thread(target=take, args=(run_id,)) for run_id in ("r1", "r2")]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join()

    assert sorted(won.values()) == [[], ["news"]]


def test_taking_a_held_inbox_is_a_conditional_update() -> None:
    queue = DefaultInboundQueue()
    async_to_sync(_bodies)(queue, "r1")

    with CaptureQueriesContext(connection) as queries:
        assert async_to_sync(_bodies)(queue, "r2") == []

    table = StoredInboxClaim._meta.db_table
    updates = [q["sql"] for q in queries.captured_queries if q["sql"].startswith("UPDATE")]
    assert all(table in update for update in updates) and len(updates) == 2
    assert '"run_id" = ' in updates[0] and '"lease_expires_at" < ' in updates[1]


async def test_a_claim_past_its_lease_is_taken_over_with_the_dead_runs_messages() -> None:
    clock = _Clock()
    queue = DefaultInboundQueue(clock=clock)
    await _send(queue, "news")
    assert await _bodies(queue, "dead") == ["news"]

    clock.now = _T0 + _LEASE + timedelta(seconds=1)

    assert await _bodies(queue, "r2") == ["news"]
    assert await _bodies(queue, "dead") == []
    claim = await StoredInboxClaim.objects.aget()
    assert (claim.run_id, claim.claimed_at) == ("r2", clock.now)
    assert claim.lease_expires_at == clock.now + _LEASE


async def test_a_claim_at_its_lease_s_last_instant_holds() -> None:
    clock = _Clock()
    queue = DefaultInboundQueue(clock=clock)
    await _bodies(queue, "r1")

    clock.now = _T0 + _LEASE

    assert await _bodies(queue, "r2") == []


async def test_each_take_renews_the_holders_lease_and_keeps_its_claim_time() -> None:
    clock = _Clock()
    queue = DefaultInboundQueue(clock=clock)
    await _bodies(queue, "r1")
    clock.now = _T0 + _LEASE
    await _bodies(queue, "r1")

    clock.now = _T0 + _LEASE + timedelta(seconds=1)
    await _send(queue, "news")

    assert await _bodies(queue, "r2") == []
    assert await _bodies(queue, "r1") == ["news"]
    claim = await StoredInboxClaim.objects.aget()
    assert (claim.claimed_at, claim.lease_expires_at) == (_T0, clock.now + _LEASE)


async def test_release_hands_the_runs_unacked_messages_to_the_next_run() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    acked = await _send(queue, "acked")
    await _send(queue, "unacked")
    await _bodies(queue, "r1")
    await queue.ack(_PARENT, [acked], request=request)

    assert await queue.release(_PARENT, "r1", request=request) is True

    assert not await StoredInboxClaim.objects.aexists()
    assert await _bodies(queue, "r2") == ["unacked"]


async def test_a_release_by_a_run_that_lost_its_claim_changes_nothing() -> None:
    clock = _Clock()
    queue = DefaultInboundQueue(clock=clock)
    await _send(queue, "news")
    await _bodies(queue, "dead")
    clock.now = _T0 + _LEASE + timedelta(seconds=1)
    await _bodies(queue, "r2")

    assert await queue.release(_PARENT, "dead", request=authed_request("7")) is False

    assert (await StoredInboxClaim.objects.aget()).run_id == "r2"
    assert await StoredInboundMessage.objects.filter(taken_by="r2").acount() == 1
    assert await _bodies(queue, "r3") == []


async def test_a_taken_message_is_acked_once() -> None:
    queue = DefaultInboundQueue()
    request = authed_request("7")
    sent = await _send(queue, "news")
    await _bodies(queue, "r1")

    assert await queue.ack(_PARENT, [sent], request=request) == 1
    assert await queue.ack(_PARENT, [sent], request=request) == 0
    assert await queue.release(_PARENT, "r1", request=request) is True
    assert await _bodies(queue, "r2") == []


async def test_inboxes_are_claimed_apart_by_owner_scope_and_thread() -> None:
    queue = DefaultInboundQueue()
    threads = [_PARENT, ThreadRef("t1", scope="b"), ThreadRef("t1"), ThreadRef("t1", scope="")]
    threads.append(ThreadRef("t2", scope="a"))
    for thread in threads:
        await _send(queue, thread.scope or "-", thread)
        assert await _bodies(queue, "r1", thread) == [thread.scope or "-"]

    for thread in threads:
        await _send(queue, "again", thread)
        assert await _bodies(queue, "r2", thread) == []
    taken = await queue.take(_PARENT, "r2", request=authed_request("8"), lease=_LEASE)
    assert taken == []
    assert await StoredInboxClaim.objects.acount() == len(threads) + 1


@pytest.mark.parametrize("field", ["thread_id", "scope", "run_id"])
async def test_an_over_long_id_takes_nothing_and_writes_nothing(field: str) -> None:
    value = too_long(StoredInboxClaim, field)
    thread = ThreadRef(
        thread_id=value if field == "thread_id" else "t1",
        scope=value if field == "scope" else "a",
    )

    with pytest.raises(IdentifierTooLongError) as error:
        await DefaultInboundQueue().take(
            thread, value if field == "run_id" else "r1", request=authed_request("7"), lease=_LEASE
        )

    assert error.value.field == field
    assert not await StoredInboxClaim.objects.aexists()


async def test_two_claims_on_one_unscoped_thread_are_refused() -> None:
    row = {"owner_id": "7", "thread_id": "t1", "claimed_at": _T0, "lease_expires_at": _T0}
    await StoredInboxClaim.objects.acreate(run_id="r1", **row)

    with pytest.raises(IntegrityError):
        await StoredInboxClaim.objects.acreate(run_id="r2", **row)
