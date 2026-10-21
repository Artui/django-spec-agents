from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Union

import pytest
from pydantic_ai import RunContext

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.registry.build_input_schema import build_input_schema
from django_spec_agents.types.tool_category import ToolCategory


class _Custom:
    """Module-level marker for the 'unrecognised class falls through' branch."""


def test_primitive_parameters() -> None:
    def fn(
        s: str,
        n: int,
        b: bool,
        f: float,
        none: None = None,
        anything: Any = None,
    ) -> None:
        del s, n, b, f, none, anything

    schema = build_input_schema(fn)
    props = schema["properties"]
    assert props["s"] == {"type": "string"}
    assert props["n"] == {"type": "integer"}
    assert props["b"] == {"type": "boolean"}
    assert props["f"] == {"type": "number"}
    assert props["none"] == {"type": "null"}
    assert props["anything"] == {}
    assert schema["required"] == ["s", "n", "b", "f"]
    assert schema["additionalProperties"] is False
    # Category default is stamped; no x-destructive when not destructive.
    assert schema["x-category"] == "other"
    assert "x-destructive" not in schema


@pytest.mark.parametrize(
    ("kwargs", "stamped", "absent"),
    [
        (
            {"destructive": True, "category": ToolCategory.UI_WRITE},
            {"x-destructive": True, "x-category": "ui_write"},
            "x-confirm",
        ),
        (
            {"destructive": True, "confirm": "Activate this project?"},
            {"x-confirm": "Activate this project?"},
            "x-summary",
        ),
        ({"summary": "Query orders"}, {"x-summary": "Query orders"}, "x-confirm"),
    ],
    ids=["destructive-and-category", "confirm", "summary"],
)
def test_extensions_are_stamped_only_when_supplied(
    kwargs: dict[str, Any], stamped: dict[str, Any], absent: str
) -> None:
    def fn(value: str) -> None:
        del value

    schema = build_input_schema(fn, **kwargs)
    assert {key: schema[key] for key in stamped} == stamped
    assert absent not in schema


def test_container_types() -> None:
    def fn(
        names: list[str],
        pairs: dict[str, int],
        seen: set[int],
        coords: tuple[int, ...],
    ) -> None:
        del names, pairs, seen, coords

    props = build_input_schema(fn)["properties"]
    assert props["names"] == {"type": "array", "items": {"type": "string"}}
    assert props["pairs"] == {"type": "object"}
    assert props["seen"] == {"type": "array", "items": {"type": "integer"}}
    assert props["coords"]["type"] == "array"


def test_union_types() -> None:
    def fn(
        a: int | None,
        b: str | int,
        c: Union[str, int, None],  # noqa: UP007 — intentional legacy form
    ) -> None:
        del a, b, c

    props = build_input_schema(fn)["properties"]
    assert props["a"] == {"type": "integer", "nullable": True}
    assert "anyOf" in props["b"]
    assert "anyOf" in props["c"]


def test_var_args_are_skipped() -> None:
    def fn(*args: int, **kwargs: str) -> None:
        del args, kwargs

    schema = build_input_schema(fn)
    assert schema["properties"] == {}
    assert "required" not in schema


def test_array_without_args_falls_back() -> None:
    def fn(items: list) -> None:  # noqa: ANN001 — intentional bare ``list``
        del items

    props = build_input_schema(fn)["properties"]
    assert props["items"] == {"type": "array", "items": {}}


def test_bare_dict_and_unknown_class() -> None:
    def fn(d: dict, custom: _Custom) -> None:  # noqa: ANN001
        del d, custom

    props = build_input_schema(fn)["properties"]
    assert props["d"] == {"type": "object"}
    assert props["custom"] == {}


def test_unknown_origin_yields_empty_fragment() -> None:
    def fn(x: Mapping[str, int]) -> None:
        del x

    props = build_input_schema(fn)["properties"]
    assert props["x"] == {}


def test_no_defaults_omits_required() -> None:
    def fn(a: int = 1) -> None:
        del a

    assert "required" not in build_input_schema(fn)


class TestTheRunContextParameterIsNotAToolArgument:
    """``ctx: RunContext[...]`` is how a tool reaches the acting user, and
    pydantic-ai supplies it. Emitting it as a property would hand a client or
    another model a schema advertising a phantom argument it would have to invent."""

    def test_a_run_context_first_parameter_is_skipped(self) -> None:
        def find_order(ctx: RunContext[AgentDeps], order_id: int) -> None:
            del ctx, order_id

        schema = build_input_schema(find_order)

        assert schema["properties"] == {"order_id": {"type": "integer"}}
        assert schema["required"] == ["order_id"]

    def test_a_run_context_only_tool_advertises_no_arguments(self) -> None:
        def whoami(ctx: RunContext[AgentDeps]) -> None:
            del ctx

        schema = build_input_schema(whoami)

        assert schema["properties"] == {}
        assert "required" not in schema

    def test_a_bare_run_context_annotation_counts(self) -> None:
        def whoami(ctx: RunContext) -> None:  # type: ignore[type-arg]
            del ctx

        assert build_input_schema(whoami)["properties"] == {}

    def test_only_the_first_parameter_is_treated_this_way(self) -> None:
        """Pydantic-AI takes the context first or not at all, so a later
        parameter of that type is an ordinary (unschematisable) argument rather
        than a second hidden one."""

        def odd(order_id: int, ctx: RunContext[AgentDeps]) -> None:
            del order_id, ctx

        assert set(build_input_schema(odd)["properties"]) == {"order_id", "ctx"}

    def test_a_parameter_merely_named_ctx_is_still_an_argument(self) -> None:
        def fn(ctx: str) -> None:
            del ctx

        assert build_input_schema(fn)["properties"] == {"ctx": {"type": "string"}}
