from __future__ import annotations

from django.conf import settings
from django.core.files.storage import Storage, default_storage, storages

from django_spec_agents.types.attachment_storage_alias import ATTACHMENT_STORAGE_ALIAS


def attachment_storage() -> Storage:
    """The ``Storage`` attachment bytes live in."""
    if ATTACHMENT_STORAGE_ALIAS in settings.STORAGES:
        return storages[ATTACHMENT_STORAGE_ALIAS]
    return default_storage


__all__ = ["attachment_storage"]
