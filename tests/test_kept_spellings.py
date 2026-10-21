"""The spellings that keep django-pydantic-agent's name, each against its literal.

The package was renamed, and a name that lives only in Python followed it. These
did not, because their value lives somewhere a rename cannot reach: a project's
``STORAGES`` and ``LOGGING``, every live session, every stored file name, a
deploy script, a browser reading a tool's schema, a model's prompt. A rename of
any of them is silent where it matters: an attachment alias nothing names falls
back to default storage, a session key nobody wrote returns no conversation, and
a handler configured for a logger that no longer logs hears nothing.

So each is asserted here against the literal, not against another name for the
same value, and a blanket rewrite of the old prefix fails this file first.
"""

from __future__ import annotations

import logging

import pytest
from django.core.management import get_commands
from django.http import HttpRequest

from django_spec_agents import (
    X_CATEGORY_KEY,
    X_CONFIRM_KEY,
    X_DESTRUCTIVE_KEY,
    X_SUMMARY_KEY,
    LoggingAuditLogger,
)
from django_spec_agents.agent.attachment_toolset import build_attachment_toolset
from django_spec_agents.contrib.store.models import StoredAttachment
from django_spec_agents.persistence import django_session_conversation_store
from django_spec_agents.persistence.null_attachment_store import NullAttachmentStore
from django_spec_agents.policy.audit import audit_capability
from django_spec_agents.policy.failure import tool_failure_policy
from django_spec_agents.types.attachment_storage_alias import ATTACHMENT_STORAGE_ALIAS


def test_the_attachment_storage_alias() -> None:
    # Spelled out literally in a project's ``STORAGES``.
    assert ATTACHMENT_STORAGE_ALIAS == "django_pydantic_agent_attachments"


def test_the_session_key() -> None:
    # Every conversation a live session holds sits under it.
    assert django_session_conversation_store._SESSION_KEY == "django_pydantic_agent_conversations"


def test_the_failure_logger() -> None:
    assert tool_failure_policy._logger.name == "django_pydantic_agent.failure"


def test_the_audit_loggers() -> None:
    # The capability's fallback for a broken backend, and the reference logger's
    # default: one name, configured once in a project's ``LOGGING``.
    assert audit_capability._fallback_logger.name == "django_pydantic_agent.audit"
    assert LoggingAuditLogger()._logger.name == "django_pydantic_agent.audit"


def test_the_upload_prefix() -> None:
    # Part of every stored file's name, so stored files keep it whatever the
    # package is called.
    assert (
        StoredAttachment._meta.get_field("file").upload_to == "django_pydantic_agent/attachments/"
    )


def test_the_four_schema_extension_keys() -> None:
    # Read by the web component and by projects' own frontends off the schema.
    assert X_DESTRUCTIVE_KEY == "x-destructive"
    assert X_CATEGORY_KEY == "x-category"
    assert X_CONFIRM_KEY == "x-confirm"
    assert X_SUMMARY_KEY == "x-summary"


def test_the_attachment_tool_name() -> None:
    # Named in projects' prompts and asserted by django-ag-ui.
    toolset = build_attachment_toolset(NullAttachmentStore(), HttpRequest())
    assert list(toolset.tools) == ["read_attachment"]


@pytest.mark.parametrize(
    "command",
    [
        "agent_store_backfill_hashes",
        "agent_store_prune_attachments",
        "agent_store_purge_memory",
        "agent_store_strip_inline_bytes",
    ],
)
def test_the_management_commands(command: str) -> None:
    # Named in deploy scripts and schedules; they never named the package.
    assert get_commands()[command] == "django_spec_agents.contrib.store"


def test_the_loggers_are_the_ones_the_caplog_filters_name() -> None:
    # The caplog tests filter records by these literals and assert the list
    # empty, which passes vacuously if the source logger is renamed and the
    # filter is not. Each of them also asserts this identity, and this test
    # states it once for the reader.
    assert logging.getLogger("django_pydantic_agent.failure") is tool_failure_policy._logger
    assert logging.getLogger("django_pydantic_agent.audit") is audit_capability._fallback_logger
