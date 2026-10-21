"""What migration 0004 imports in place of django-pydantic-agent's module.

The fixture's five migrations are django-pydantic-agent's, verbatim but for 0004's
reference to this function: the real one lives in a distribution this repository
does not install. The ``AlterField`` that names it changes state only, and emits
no SQL, so the schema the upgrade starts from is the one that shipped.
"""

from __future__ import annotations

from django.core.files.storage import Storage, default_storage


def attachment_storage() -> Storage:
    return default_storage
