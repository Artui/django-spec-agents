from __future__ import annotations

from django.db.models import QuerySet
from django.utils import timezone

from django_spec_agents.contrib.store.models import StoredAttachment, StoredConversation
from django_spec_agents.contrib.store.reconcile_conversation_attachments import (
    reconcile_conversation_attachments,
)
from django_spec_agents.contrib.store.utils import delete_attachments, unreferenced_attachments
from django_spec_agents.persistence.model_conversation_store import ModelConversationStore
from django_spec_agents.persistence.types.conversation import Conversation
from django_spec_agents.persistence.types.conversation_meta import (
    ConversationMeta,
    ConversationMetaList,
)
from django_spec_agents.persistence.utils import derive_preview, derive_title


class DefaultConversationStore(ModelConversationStore):
    """A ready-to-use model-backed store over ``StoredConversation``."""

    def _fetch(self, thread_id: str, owner_id: str | None) -> Conversation | None:
        row = _thread_rows(thread_id, owner_id).first()
        if row is None:
            return None
        return Conversation(
            thread_id=row.thread_id,
            messages=list(row.messages),
            owner_id=row.owner_id or None,
        )

    def _store(self, conversation: Conversation, owner_id: str | None) -> None:
        messages = conversation.messages
        defaults = {
            "messages": list(messages),
            "preview": derive_preview(messages),
        }
        row, created = StoredConversation.objects.get_or_create(
            owner_id=owner_id or "",
            thread_id=conversation.thread_id,
            defaults={**defaults, "title": derive_title(messages)},
        )
        if not created:
            # ``.update()`` preserves the title but bypasses the ``auto_now``
            # field, so bump ``updated_at`` explicitly to keep the drawer ordered
            # by recency.
            StoredConversation.objects.filter(pk=row.pk).update(
                **defaults, updated_at=timezone.now()
            )
        # Reconcile from the messages just stored: ``row.messages`` is a save
        # behind on the update path.
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
            .values("thread_id", "title", "preview", "updated_at", "owner_id")
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
            )
            for row in rows
        ]

    def _exists(self, thread_id: str, owner_id: str | None) -> bool:
        # Metadata-only, unlike the base's ``_fetch`` fallback: no message body is deserialized.
        return _thread_rows(thread_id, owner_id).exists()

    def _rename(self, thread_id: str, title: str, owner_id: str | None) -> None:
        _thread_rows(thread_id, owner_id).update(title=title)


def _thread_rows(thread_id: str, owner_id: str | None) -> QuerySet[StoredConversation]:
    """The owner's row for ``thread_id``, as a queryset; anonymous is stored as ``""``."""
    return StoredConversation.objects.filter(owner_id=owner_id or "", thread_id=thread_id)


__all__ = ["DefaultConversationStore"]
