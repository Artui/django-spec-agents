from __future__ import annotations

from django.core.files.base import ContentFile
from django.db.models import Model

from django_spec_agents.contrib.store.models import StoredAttachment


def stored_attachment(attachment_id: str, owner_id: str = "7") -> StoredAttachment:
    """An attachment row with a five-byte blob in the default storage."""
    row = StoredAttachment(attachment_id=attachment_id, owner_id=owner_id, size=5)
    row.file.save(attachment_id, ContentFile(b"hello"), save=False)
    row.save()
    return row


def limit_of(model: type[Model], field: str) -> int:
    """The column width ``field`` has on ``model``, read from the field itself."""
    return model._meta.get_field(field).max_length


def too_long(model: type[Model], field: str) -> str:
    """A value one character longer than ``field``'s column on ``model``."""
    return "x" * (limit_of(model, field) + 1)


def at_limit(model: type[Model], field: str) -> str:
    """A value exactly as long as ``field``'s column on ``model``."""
    return "x" * limit_of(model, field)


def refusal(model: type[Model], field: str) -> str:
    """The ``pytest.raises`` match for refusing ``field`` on ``model``."""
    return (
        f"^{field} is {limit_of(model, field) + 1} characters, over its {limit_of(model, field)}-"
    )
