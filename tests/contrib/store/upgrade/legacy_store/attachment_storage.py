from __future__ import annotations

from django.core.files.storage import Storage, default_storage


def attachment_storage() -> Storage:
    return default_storage
