from __future__ import annotations

from django_spec_agents.constants import (
    X_CATEGORY_KEY,
    X_CONFIRM_KEY,
    X_DESTRUCTIVE_KEY,
    X_SUMMARY_KEY,
    ToolCategory,
)


def test_tool_category_values() -> None:
    assert ToolCategory.SHELL.value == "shell"
    assert ToolCategory.UI_WRITE.value == "ui_write"
    # Every member round-trips through its string value.
    for member in ToolCategory:
        assert ToolCategory(member.value) is member


def test_extension_keys() -> None:
    assert X_DESTRUCTIVE_KEY == "x-destructive"
    assert X_CATEGORY_KEY == "x-category"
    assert X_CONFIRM_KEY == "x-confirm"
    assert X_SUMMARY_KEY == "x-summary"


def test_the_destructive_metadata_key_names_this_package() -> None:
    # In-process only: it rides ``ToolDefinition.metadata`` between a bridge and
    # ``ToolGuard`` and is never stored, so it follows the package's name.
    from django_spec_agents import DESTRUCTIVE_METADATA_KEY

    assert DESTRUCTIVE_METADATA_KEY == "django_spec_agents.destructive"
