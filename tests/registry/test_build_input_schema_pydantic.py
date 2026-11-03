"""``build_input_schema`` under ``SchemaStrategy.PYDANTIC``, and choosing a strategy."""

from __future__ import annotations

import functools
from enum import Enum
from typing import Annotated, Any

import pytest
from pydantic import Field, PydanticSchemaGenerationError
from pydantic.json_schema import PydanticJsonSchemaWarning
from pydantic_ai import RunContext

from django_spec_agents import SchemaStrategy, ToolCategory, build_input_schema
from django_spec_agents.agent.types.agent_deps import AgentDeps

_OPAQUE = object()

# Module-level, so a string reference to it resolves in a tool's globals and
# nowhere else.
_Count = int


class _Opaque:
    """A class pydantic has no schema for."""


class _Colour(str, Enum):
    RED = "red"
    BLUE = "blue"


class _CountTool:
    """A callable instance: `inspect.signature` reads ``type(self).__call__``."""

    def __call__(self, ids: list[Annotated["_Count", "t"]]) -> None:  # noqa: UP037
        del ids


def _count_ids(ids: list[Annotated["_Count", "t"]], limit: int) -> None:  # noqa: UP037
    del ids, limit


def _find(
    n: Annotated[int, Field(description="How many.", ge=1)],
    note: str | None = None,
) -> None:
    del n, note


def test_the_default_strategy_is_hand_built() -> None:
    assert build_input_schema(_find) == build_input_schema(
        _find, schema_strategy=SchemaStrategy.HAND_BUILT
    )
    assert build_input_schema(_find)["properties"]["note"] == {
        "type": "string",
        "nullable": True,
    }


def test_the_pydantic_strategy_returns_pydantic_s_schema() -> None:
    schema = build_input_schema(_find, schema_strategy=SchemaStrategy.PYDANTIC)
    assert schema["title"] == "_find"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["n"]
    assert schema["properties"]["n"] == {
        "description": "How many.",
        "minimum": 1,
        "title": "N",
        "type": "integer",
    }
    # ``X | None`` is pydantic's ``anyOf`` with a ``null`` branch, not ``nullable``,
    # and a default is advertised.
    assert schema["properties"]["note"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "title": "Note",
    }


def test_the_pydantic_strategy_puts_nested_types_in_defs() -> None:
    def fn(colour: _Colour = _Colour.RED) -> None:
        del colour

    schema = build_input_schema(fn, schema_strategy=SchemaStrategy.PYDANTIC)
    assert schema["properties"]["colour"] == {"$ref": "#/$defs/_Colour", "default": "red"}
    assert schema["$defs"]["_Colour"]["enum"] == ["red", "blue"]


def test_the_pydantic_strategy_stamps_keys_and_skips_non_arguments() -> None:
    def fn(ctx: RunContext[AgentDeps], n: int, *args: int, **kwargs: str) -> None:
        del ctx, n, args, kwargs

    schema = build_input_schema(
        fn,
        destructive=True,
        category=ToolCategory.UI_WRITE,
        confirm="Sure?",
        summary="Do it",
        schema_strategy=SchemaStrategy.PYDANTIC,
    )
    assert list(schema["properties"]) == ["n"]
    assert schema["x-destructive"] is True
    assert schema["x-category"] == "ui_write"
    assert schema["x-confirm"] == "Sure?"
    assert schema["x-summary"] == "Do it"


def test_the_pydantic_strategy_resolves_nested_string_references() -> None:
    def fn(
        n: Annotated["int", Field(ge=1)],  # noqa: UP037
        tags: list[Annotated["int", "t"]],  # noqa: UP037
    ) -> None:
        del n, tags

    props = build_input_schema(fn, schema_strategy=SchemaStrategy.PYDANTIC)["properties"]
    assert props["n"] == {"minimum": 1, "title": "N", "type": "integer"}
    assert props["tags"] == {"items": {"type": "integer"}, "title": "Tags", "type": "array"}


@pytest.mark.parametrize(
    ("fn", "title"),
    [(_CountTool(), "_CountTool"), (functools.partial(_count_ids, limit=1), "partial")],
    ids=["callable instance", "partial"],
)
def test_the_pydantic_strategy_resolves_references_where_the_signature_does(
    fn: Any, title: str
) -> None:
    # Neither object has a ``__name__``, so the model is named after its type.
    schema = build_input_schema(fn, schema_strategy=SchemaStrategy.PYDANTIC)
    assert schema["title"] == title
    assert schema["properties"]["ids"] == {
        "items": {"type": "integer"},
        "title": "Ids",
        "type": "array",
    }


def test_the_pydantic_strategy_raises_on_an_unresolvable_reference() -> None:
    def fn(missing: list["Nope"]) -> None:  # noqa: F821, UP037
        del missing

    with pytest.raises(NameError, match="Nope"):
        build_input_schema(fn, schema_strategy=SchemaStrategy.PYDANTIC)


def test_the_pydantic_strategy_refuses_a_parameter_pydantic_would_drop() -> None:
    def fn(_private: int, model_config: int, kept: int) -> None:
        del _private, model_config, kept

    with pytest.raises(TypeError, match=r"\['_private', 'model_config'\] of 'fn'"):
        build_input_schema(fn, schema_strategy=SchemaStrategy.PYDANTIC)


def test_the_pydantic_strategy_raises_on_a_type_pydantic_cannot_schematise() -> None:
    def fn(x: _Opaque) -> None:
        del x

    with pytest.raises(PydanticSchemaGenerationError):
        build_input_schema(fn, schema_strategy=SchemaStrategy.PYDANTIC)


def test_the_pydantic_strategy_leaves_out_a_default_that_is_not_json() -> None:
    def fn(x: Any = _OPAQUE) -> None:
        del x

    with pytest.warns(PydanticJsonSchemaWarning):
        schema = build_input_schema(fn, schema_strategy=SchemaStrategy.PYDANTIC)
    assert schema["properties"]["x"] == {"title": "X"}


def test_the_strategy_is_a_closed_choice() -> None:
    bogus: Any = "bogus"
    with pytest.raises(ValueError, match="bogus"):
        build_input_schema(_find, schema_strategy=bogus)
    # A member's value names it as well as the member does.
    by_value: Any = "pydantic"
    assert build_input_schema(_find, schema_strategy=by_value) == build_input_schema(
        _find, schema_strategy=SchemaStrategy.PYDANTIC
    )
