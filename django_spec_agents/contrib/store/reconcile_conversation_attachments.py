from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django_spec_agents.contrib.store.models import StoredAttachment, StoredConversation
from django_spec_agents.persistence.utils import message_field


def reconcile_conversation_attachments(
    conversation: StoredConversation, messages: Iterable[Any]
) -> None:
    """Point ``conversation`` at exactly the attachments its messages quote."""
    quoted = _quoted_ids(messages)
    conversation.attachments.set(
        StoredAttachment.objects.filter(owner_id=conversation.owner_id, attachment_id__in=quoted)
    )


def _quoted_ids(messages: Iterable[Any]) -> set[str]:
    """Every attachment id the messages mention, deduplicated."""
    quoted: set[str] = set()
    for message in messages:
        entries = message_field(message, "attachments")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            identifier = message_field(entry, "id")
            if isinstance(identifier, str) and identifier.strip():
                quoted.add(identifier)
    return quoted


__all__ = ["reconcile_conversation_attachments"]
