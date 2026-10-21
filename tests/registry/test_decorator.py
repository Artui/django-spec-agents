from __future__ import annotations

import pytest

from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.types.tool_category import ToolCategory


def test_decorator_uses_function_name_and_docstring() -> None:
    reg = ToolRegistry()

    @tool(reg)
    def echo(value: str) -> str:
        """Echo the value back.

        Second paragraph that should be dropped.
        """
        return value

    binding = reg.get("echo")
    assert binding.spec.description == "Echo the value back."
    assert binding.spec.destructive is False
    assert binding.spec.category is ToolCategory.OTHER
    assert reg.call("echo", {"value": "hi"}) == "hi"


def test_decorator_overrides_take_precedence() -> None:
    reg = ToolRegistry()

    @tool(
        reg,
        name="custom",
        description="overridden",
        destructive=True,
        category=ToolCategory.UI_WRITE,
        confirm="Run the custom action?",
        summary="Custom action",
    )
    def fn() -> int:
        return 1

    binding = reg.get("custom")
    assert binding.spec.description == "overridden"
    assert binding.spec.destructive is True
    assert binding.spec.category is ToolCategory.UI_WRITE
    assert binding.spec.confirm == "Run the custom action?"
    assert binding.spec.summary == "Custom action"
    assert binding.input_schema["x-destructive"] is True
    assert binding.input_schema["x-confirm"] == "Run the custom action?"
    assert binding.input_schema["x-summary"] == "Custom action"


@pytest.mark.parametrize(
    ("docstring", "description"),
    [
        (None, ""),
        # No blank line, so the paragraph loop runs out rather than breaking.
        (
            "Single line, no blank line - loop exhausts without a break.",
            "Single line, no blank line - loop exhausts without a break.",
        ),
        (
            "First line of summary\n    continues onto a second line.\n\n    Second paragraph.\n",
            "First line of summary continues onto a second line.",
        ),
    ],
    ids=["missing", "single-line", "first-paragraph-only"],
)
def test_decorator_describes_a_tool_by_its_docstrings_first_paragraph(
    docstring: str | None, description: str
) -> None:
    reg = ToolRegistry()

    def fn() -> None:
        return None

    fn.__doc__ = docstring
    tool(reg)(fn)

    assert reg.get("fn").spec.description == description
