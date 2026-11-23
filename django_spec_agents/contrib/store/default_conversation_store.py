from __future__ import annotations

from django.db.models import QuerySet
from django.utils import timezone

from django_spec_agents.contrib.store.models import StoredAttachment, StoredConversation
from django_spec_agents.contrib.store.reconcile_conversation_attachments import (
    reconcile_conversation_attachments,
)
from django_spec_agents.contrib.store.utils import (
    check_column_widths,
    delete_attachments,
    unreferenced_attachments,
)
from django_spec_agents.persistence.model_conversation_store import ModelConversationStore
from django_spec_agents.persistence.types.child_lineage_record import ChildLineageRecord
from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.conversation_meta import (
    ConversationMeta,
    ConversationMetaList,
)
from django_spec_agents.persistence.types.thread_ref import ThreadRef
from django_spec_agents.persistence.utils import derive_preview, derive_title


class DefaultConversationStore(ModelConversationStore):
    """A ready-to-use model-backed store over ``StoredConversation``.

    Cross-device, per-user history with a cheap thread list. Add
    ``"django_spec_agents.contrib.store"`` to ``INSTALLED_APPS``, run
    ``migrate``, and pass an instance to your transport's
    ``conversation_store=``. For a bespoke schema, subclass
    [`ModelConversationStore`][django_spec_agents.ModelConversationStore] instead.

    Every query filters by the ``owner_id`` the base resolves. A title is derived
    from the first user message at first save and then left alone except by a
    rename; the preview re-derives on every save.

    Saving also reconciles which attachments the thread refers to, and deleting
    it drops the ones nothing else refers to, so an attachment's lifetime is tied
    to the conversations quoting it. An upload that was never sent belongs to no
    conversation, and the ``agent_store_prune_attachments`` command collects
    those instead.

    It is an
    [`UnlinkableConversationStore`][django_spec_agents.UnlinkableConversationStore]:
    ``unlink_parent`` writes NULL to the thread's parent columns and its lineage,
    and leaves its place in the list alone, as a rename does.
    """

    def check_identifiers(self, *, thread_id: str) -> None:
        """Raise ``IdentifierTooLongError`` for a ``thread_id`` its column cannot hold.

        It reads the column width and writes nothing, so a transport can refuse the
        id before it starts a run; ``preflight_identifiers`` calls it.
        """
        check_column_widths(StoredConversation, thread_id=thread_id)

    def _fetch(self, thread_id: str, owner_id: str | None) -> Conversation | None:
        row = _thread_rows(thread_id, owner_id).first()
        if row is None:
            return None
        return Conversation(
            thread_id=row.thread_id,
            messages=list(row.messages),
            owner_id=row.owner_id or None,
            parent=_parent(row.parent_thread_id, row.parent_scope),
            lineage=None if row.lineage is None else ChildLineageRecord.from_json(row.lineage),
            routine_id=row.routine_id,
        )

    def _store(self, conversation: Conversation, owner_id: str | None) -> None:
        parent = conversation.parent
        check_column_widths(
            StoredConversation,
            owner_id=owner_id,
            thread_id=conversation.thread_id,
            parent_thread_id=None if parent is None else parent.thread_id,
            parent_scope=None if parent is None else parent.scope,
            routine_id=conversation.routine_id,
        )
        messages = conversation.messages
        defaults: dict[str, object] = {
            "messages": list(messages),
            "preview": derive_preview(messages),
        }
        if parent is not None:
            defaults.update(parent_thread_id=parent.thread_id, parent_scope=parent.scope)
        if conversation.lineage is not None:
            defaults["lineage"] = conversation.lineage.to_json()
        if conversation.routine_id is not None:
            defaults["routine_id"] = conversation.routine_id
        row, created = StoredConversation.objects.get_or_create(
            owner_id=owner_id or "",
            thread_id=conversation.thread_id,
            defaults={**defaults, "title": derive_title(messages)},
        )
        if not created:
            # ``.update()`` skips ``auto_now``, so bump ``updated_at`` by hand for recency order.
            StoredConversation.objects.filter(pk=row.pk).update(
                **defaults, updated_at=timezone.now()
            )
        reconcile_conversation_attachments(row, messages)

    def _remove(self, thread_id: str, owner_id: str | None) -> None:
        """Delete the thread, and with it the attachments nothing else holds."""
        rows = _thread_rows(thread_id, owner_id)
        referenced = list(
            StoredAttachment.objects.filter(conversations__in=rows)
            .distinct()
            .values_list("pk", flat=True)
        )
        rows.delete()
        delete_attachments(
            unreferenced_attachments(StoredAttachment.objects.filter(pk__in=referenced))
        )

    def _list(self, owner_id: str | None, limit: int | None) -> ConversationMetaList:
        rows = (
            StoredConversation.objects.filter(owner_id=owner_id or "")
            .order_by("-updated_at")
            .values(
                "thread_id",
                "title",
                "preview",
                "updated_at",
                "owner_id",
                "parent_thread_id",
                "parent_scope",
                "routine_id",
            )
        )
        if limit is not None:
            rows = rows[:limit]
        return [
            ConversationMeta(
                thread_id=row["thread_id"],
                title=row["title"],
                updated_at=row["updated_at"],
                preview=row["preview"],
                owner_id=row["owner_id"] or None,
                parent=_parent(row["parent_thread_id"], row["parent_scope"]),
                routine_id=row["routine_id"],
            )
            for row in rows
        ]

    def _exists(self, thread_id: str, owner_id: str | None) -> bool:
        # Metadata-only, unlike the base's ``_fetch`` fallback: no message body is deserialized.
        return _thread_rows(thread_id, owner_id).exists()

    def _rename(self, thread_id: str, title: str, owner_id: str | None) -> None:
        _thread_rows(thread_id, owner_id).update(title=title)

    def _unlink_parent(self, thread_id: str, owner_id: str | None) -> None:
        _thread_rows(thread_id, owner_id).update(
            parent_thread_id=None, parent_scope=None, lineage=None
        )


def _parent(thread_id: str | None, scope: str | None) -> ThreadRef | None:
    return None if thread_id is None else ThreadRef(thread_id=thread_id, scope=scope)


def _thread_rows(thread_id: str, owner_id: str | None) -> QuerySet[StoredConversation]:
    """The owner's row for ``thread_id``, as a queryset; anonymous is stored as ``""``."""
    return StoredConversation.objects.filter(owner_id=owner_id or "", thread_id=thread_id)


__all__ = ["DefaultConversationStore"]
