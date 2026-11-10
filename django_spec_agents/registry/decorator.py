from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from django_spec_agents.registry.tool_registry import ToolRegistry
from django_spec_agents.registry.types.tool_spec import ToolSpec
from django_spec_agents.registry.utils import first_doc_paragraph
from django_spec_agents.types.schema_strategy import SchemaStrategy
from django_spec_agents.types.tool_category import ToolCategory

F = TypeVar("F", bound=Callable[..., Any])


def tool(
    registry: ToolRegistry,
    *,
    name: str | None = None,
    description: str | None = None,
    destructive: bool = False,
    category: ToolCategory = ToolCategory.OTHER,
    confirm: str | None = None,
    summary: str | None = None,
    schema_strategy: SchemaStrategy | None = None,
) -> Callable[[F], F]:
    """Register the decorated callable on ``registry`` as a tool."""

    def _decorator(fn: F) -> F:
        spec = ToolSpec(
            name=name or getattr(fn, "__name__", "<anonymous>"),
            fn=fn,
            description=description if description is not None else first_doc_paragraph(fn),
            destructive=destructive,
            category=category,
            confirm=confirm,
            summary=summary,
            schema_strategy=schema_strategy,
        )
        registry.register(spec)
        return fn

    return _decorator


__all__ = ["tool"]
