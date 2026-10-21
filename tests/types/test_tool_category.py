from __future__ import annotations

from django_spec_agents.types.tool_category import ToolCategory


def test_tool_category_values() -> None:
    assert ToolCategory.SHELL.value == "shell"
    assert ToolCategory.UI_WRITE.value == "ui_write"
    # Every member round-trips through its string value.
    for member in ToolCategory:
        assert ToolCategory(member.value) is member
