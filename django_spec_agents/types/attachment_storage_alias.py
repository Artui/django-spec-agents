"""The ``STORAGES`` alias attachment bytes are written through, when a project names one.

Agent uploads are user-supplied files a project usually wants in a private
bucket, and without this the only way to move them is to change the global
default and every other ``FileField`` with it. The value keeps the spelling
django-pydantic-agent gave it: it is configured in a project's ``STORAGES``,
and an alias nothing names falls back to default storage silently.
"""

from __future__ import annotations

ATTACHMENT_STORAGE_ALIAS = "django_pydantic_agent_attachments"

__all__ = ["ATTACHMENT_STORAGE_ALIAS"]
