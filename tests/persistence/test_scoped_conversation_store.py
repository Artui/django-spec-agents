from __future__ import annotations

import pytest
from django.test import RequestFactory

from django_spec_agents.contrib.store.default_conversation_store import DefaultConversationStore
from django_spec_agents.contrib.store.models import StoredConversation
from django_spec_agents.persistence.identifier_too_long_error import IdentifierTooLongError
from django_spec_agents.persistence.scoped_conversation_store import ScopedConversationStore
from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.conversation_meta import ConversationMeta
from django_spec_agents.persistence.types.conversation_store import ConversationStore


class _MemoryStore:
    """A store keyed exactly the way the real ones are: by thread id."""

    def __init__(self) -> None:
        self.rows: dict[str, Conversation] = {}
        self.titles: dict[str, str] = {}

    async def load(self, thread_id, *, request):  # noqa: ANN001, ANN202, ARG002
        return self.rows.get(thread_id)

    async def save(self, conversation, *, request):  # noqa: ANN001, ANN202, ARG002
        self.rows[conversation.thread_id] = conversation

    async def delete(self, thread_id, *, request):  # noqa: ANN001, ANN202, ARG002
        self.rows.pop(thread_id, None)

    async def list(self, *, request, limit=None):  # noqa: ANN001, ANN202, ARG002
        return [ConversationMeta(thread_id=t, title=t) for t in self.rows]

    async def exists(self, thread_id, *, request):  # noqa: ANN001, ANN202, ARG002
        return thread_id in self.rows

    async def rename(self, thread_id, title, *, request):  # noqa: ANN001, ANN202, ARG002
        self.titles[thread_id] = title


@pytest.fixture
def request_() -> object:
    return RequestFactory().post("/agent/")


async def test_two_scopes_over_one_store_do_not_see_each_others_threads(request_) -> None:
    """Unscoped, a thread from /internal/agent would show in /public/agent's history."""
    inner = _MemoryStore()
    internal = ScopedConversationStore(inner, scope="internal")
    public = ScopedConversationStore(inner, scope="public")

    await internal.save(Conversation(thread_id="t1", messages=[]), request=request_)

    assert await internal.load("t1", request=request_) is not None
    assert await public.load("t1", request=request_) is None
    assert await public.exists("t1", request=request_) is False
    assert [m.thread_id for m in await public.list(request=request_)] == []


async def test_the_scope_never_reaches_the_client(request_) -> None:
    """Only the storage key is prefixed — the client's ids round-trip unchanged."""
    inner = _MemoryStore()
    scoped = ScopedConversationStore(inner, scope="internal")

    await scoped.save(Conversation(thread_id="t1", messages=[]), request=request_)

    assert "internal:t1" in inner.rows
    loaded = await scoped.load("t1", request=request_)
    assert loaded is not None
    assert loaded.thread_id == "t1"
    assert [m.thread_id for m in await scoped.list(request=request_)] == ["t1"]


async def test_delete_and_rename_are_scoped(request_) -> None:
    inner = _MemoryStore()
    internal = ScopedConversationStore(inner, scope="internal")
    public = ScopedConversationStore(inner, scope="public")
    await internal.save(Conversation(thread_id="t1", messages=[]), request=request_)

    # A same-id delete from the other scope must not touch it.
    await public.delete("t1", request=request_)
    assert await internal.exists("t1", request=request_) is True

    await internal.rename("t1", "Renamed", request=request_)
    assert inner.titles == {"internal:t1": "Renamed"}

    await internal.delete("t1", request=request_)
    assert await internal.exists("t1", request=request_) is False


async def test_an_unprefixed_row_is_left_alone(request_) -> None:
    """Rows written without a scope keep their own ids — the unkey is a strip, not a slice."""
    inner = _MemoryStore()
    inner.rows["legacy"] = Conversation(thread_id="legacy", messages=[])
    scoped = ScopedConversationStore(inner, scope="internal")

    assert [m.thread_id for m in await scoped.list(request=request_)] == []
    assert "legacy" in inner.rows


async def test_a_scope_name_cannot_extend_another_scopes_name(request_) -> None:
    """The reproduction, and the reason the separator is reserved."""
    inner = _MemoryStore()
    ScopedConversationStore(inner, scope="admin")

    with pytest.raises(ValueError, match="scope"):
        ScopedConversationStore(inner, scope="admin:readonly")


async def test_the_partition_holds_for_a_scope_that_merely_shares_a_prefix(request_) -> None:
    """``admin`` and ``administrators`` are two scopes: the separator ends the scope."""
    inner = _MemoryStore()
    admin = ScopedConversationStore(inner, scope="admin")
    administrators = ScopedConversationStore(inner, scope="administrators")

    await administrators.save(Conversation(thread_id="t1", messages=[]), request=request_)

    assert [m.thread_id for m in await admin.list(request=request_)] == []
    assert await admin.load("t1", request=request_) is None


class _CheckingStore(_MemoryStore):
    """A store that can preflight its ids, recording what it was asked to check."""

    def __init__(self) -> None:
        super().__init__()
        self.checked: list[dict[str, str | None]] = []

    def check_identifiers(self, **ids: str | None) -> None:
        self.checked.append(ids)


def test_a_wrapper_over_a_store_without_the_check_does_not_offer_one() -> None:
    scoped = ScopedConversationStore(_MemoryStore(), scope="internal")

    assert not hasattr(scoped, "check_identifiers")
    assert isinstance(scoped, ConversationStore)


def test_the_check_is_forwarded_with_the_scope_applied_to_the_thread_id() -> None:
    inner = _CheckingStore()
    scoped = ScopedConversationStore(inner, scope="internal")

    scoped.check_identifiers(thread_id="t1", run_id="r1")

    assert inner.checked == [{"thread_id": "internal:t1", "run_id": "r1"}]
    assert isinstance(scoped, ConversationStore)


def test_the_check_without_a_thread_id_forwards_the_ids_untouched() -> None:
    inner = _CheckingStore()

    ScopedConversationStore(inner, scope="internal").check_identifiers(run_id="r1")

    assert inner.checked == [{"run_id": "r1"}]


def test_the_prefix_counts_against_the_inner_column() -> None:
    scoped = ScopedConversationStore(DefaultConversationStore(), scope="internal")
    room = StoredConversation._meta.get_field("thread_id").max_length - len("internal:")

    scoped.check_identifiers(thread_id="x" * room)
    with pytest.raises(IdentifierTooLongError, match="^thread_id is 256 characters"):
        scoped.check_identifiers(thread_id="x" * (room + 1))
