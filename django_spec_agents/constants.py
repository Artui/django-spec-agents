"""Transitional: the names that moved into ``django_spec_agents.types``.

Read by the root ``__init__`` alone, until it imports each name from its leaf
module under ``types/``; nothing else may import from here, and the module goes
when the root stops reading it.
"""

from __future__ import annotations

from django_spec_agents.types.attachment_storage_alias import ATTACHMENT_STORAGE_ALIAS
from django_spec_agents.types.destructive_metadata_key import DESTRUCTIVE_METADATA_KEY
from django_spec_agents.types.tool_category import ToolCategory
from django_spec_agents.types.x_category_key import X_CATEGORY_KEY
from django_spec_agents.types.x_confirm_key import X_CONFIRM_KEY
from django_spec_agents.types.x_destructive_key import X_DESTRUCTIVE_KEY
from django_spec_agents.types.x_summary_key import X_SUMMARY_KEY

__all__ = [
    "ATTACHMENT_STORAGE_ALIAS",
    "DESTRUCTIVE_METADATA_KEY",
    "X_CATEGORY_KEY",
    "X_CONFIRM_KEY",
    "X_DESTRUCTIVE_KEY",
    "X_SUMMARY_KEY",
    "ToolCategory",
]
