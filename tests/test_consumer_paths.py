"""The module paths django-ag-ui imports from, each at its path.

django-ag-ui reads this package by leaf module as often as by its root, and some of
what it reads is not exported from the root at all: the request helpers in
``utils``, the attachment toolset, the model builder, the owner helpers and the
three integration modules, which need extras the root must not import. Those paths
are as public as the root's names, so a move or rename of one fails here before it
fails in django-ag-ui's sweep.

The set is django-ag-ui's own imports, read off its default branch; the root
names it re-exports are the root ``__all__``'s to hold.
"""

from __future__ import annotations

import importlib
import types

import pytest

# Imported by django-ag-ui and exported from no ``__all__`` of this package.
NOT_AT_THE_ROOT = [
    (
        "django_spec_agents.utils",
        ["AuthorizePredicate", "GetUser", "aauthorize", "auth_error_response", "authorize"],
    ),
    ("django_spec_agents.agent.attachment_toolset", ["build_attachment_toolset"]),
    ("django_spec_agents.agent.build_model", ["build_model"]),
    ("django_spec_agents.persistence.utils", ["owner_id_for", "resolve_owner_id"]),
    ("django_spec_agents.integrations.build_spec_capability", ["build_spec_capability"]),
    ("django_spec_agents.integrations.drf_mcp", ["DRFMCPToolset"]),
    ("django_spec_agents.integrations.resolve_spec_mapping", ["resolve_spec_mapping"]),
    ("django_spec_agents.integrations.types.spec_source", ["SpecSource"]),
]

# Root names django-ag-ui imports from their leaf modules instead, in its package
# and its tests.
LEAF_PATHS_OF_ROOT_NAMES = [
    ("django_spec_agents.agent.agent_factory", ["build_agent"]),
    ("django_spec_agents.agent.build_tool_catalog", ["build_tool_catalog"]),
    ("django_spec_agents.agent.types.agent_config", ["AgentConfig"]),
    ("django_spec_agents.agent.types.agent_deps", ["AgentDeps"]),
    ("django_spec_agents.agent.types.agent_factory_fn", ["AgentFactoryFn"]),
    ("django_spec_agents.persistence.anonymous_operation_error", ["AnonymousOperationError"]),
    (
        "django_spec_agents.persistence.django_session_conversation_store",
        ["DjangoSessionConversationStore"],
    ),
    ("django_spec_agents.persistence.null_attachment_store", ["NullAttachmentStore"]),
    ("django_spec_agents.persistence.null_conversation_store", ["NullConversationStore"]),
    ("django_spec_agents.persistence.types.attachment_ref", ["AttachmentRef"]),
    ("django_spec_agents.persistence.types.attachment_store", ["AttachmentStore"]),
    ("django_spec_agents.persistence.types.conversation", ["Conversation"]),
    ("django_spec_agents.persistence.types.conversation_meta", ["ConversationMeta"]),
    ("django_spec_agents.persistence.types.conversation_store", ["ConversationStore"]),
    ("django_spec_agents.persistence.types.opened_attachment", ["OpenedAttachment"]),
    ("django_spec_agents.policy.audit.logging_audit_logger", ["LoggingAuditLogger"]),
    ("django_spec_agents.policy.audit.null_audit_logger", ["NullAuditLogger"]),
    ("django_spec_agents.policy.audit.types.audit_event", ["AuditEvent"]),
    ("django_spec_agents.policy.audit.types.audit_logger", ["AuditLogger"]),
    ("django_spec_agents.policy.failure.types.tool_failure_config", ["ToolFailureConfig"]),
    ("django_spec_agents.policy.guard.types.tool_guard_config", ["ToolGuardConfig"]),
    ("django_spec_agents.registry.decorator", ["tool"]),
    ("django_spec_agents.registry.tool_registry", ["ToolRegistry"]),
    (
        "django_spec_agents.contrib.store.default_attachment_store",
        ["DefaultAttachmentStore"],
    ),
    (
        "django_spec_agents.contrib.store.default_conversation_store",
        ["DefaultConversationStore"],
    ),
    ("django_spec_agents.contrib.store.default_step_store", ["DefaultStepStore"]),
    (
        "django_spec_agents.contrib.store.models",
        [
            "StoredConversation",
            "StoredRun",
            "StoredSnapshot",
            "StoredStepEvent",
            "StoredToolEffect",
        ],
    ),
]


@pytest.mark.parametrize(
    ("module", "names"),
    NOT_AT_THE_ROOT + LEAF_PATHS_OF_ROOT_NAMES,
    ids=[module for module, _ in NOT_AT_THE_ROOT + LEAF_PATHS_OF_ROOT_NAMES],
)
def test_the_path_holds_the_names(module: str, names: list[str]) -> None:
    imported = importlib.import_module(module)
    assert [name for name in names if not hasattr(imported, name)] == []


def test_the_integrations_package_hands_out_the_builder_module() -> None:
    # django-ag-ui's tests import the submodule by its package and patch an
    # attribute on it, which works only while the package re-exports nothing: a
    # re-export of the function would shadow the submodule of the same name.
    from django_spec_agents.integrations import build_spec_capability

    assert isinstance(build_spec_capability, types.ModuleType)
