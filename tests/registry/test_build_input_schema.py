from __future__ import annotations

import datetime
import functools
import json
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Any, Union

import annotated_types
import pytest
from pydantic import Field
from pydantic_ai import RunContext

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.registry.build_input_schema import build_input_schema
from django_spec_agents.types.tool_category import ToolCategory


class _Custom:
    """Module-level marker for the 'unrecognised class falls through' branch."""


# Module-level, so a string reference to it resolves in a tool's globals and
# nowhere else.
_Count = int


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


def test_annotated_parameters_unwrap_to_their_first_argument() -> None:
    # `eval_str=True` keeps `Annotated`; the underlying type shapes the schema
    # and the metadata adds its keys, a plain string adding none.
    def fn(
        n: Annotated[int, Field(description="How many.", ge=1)],
        tags: list[Annotated[str, "tag"]],
        maybe: Annotated[int | None, Field(le=5)] = None,
        wrapped: Annotated[list[Annotated[int, "i"]], "outer"] = [],  # noqa: B006
        trailing: Annotated[int, "a"] | None = None,
    ) -> None:
        del n, tags, maybe, wrapped, trailing

    props = build_input_schema(fn)["properties"]
    assert props["n"] == {"type": "integer", "description": "How many.", "minimum": 1}
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["maybe"] == {"type": "integer", "nullable": True, "maximum": 5}
    assert props["wrapped"] == {"type": "array", "items": {"type": "integer"}}
    assert props["trailing"] == {"type": "integer", "nullable": True}


def test_a_field_s_numeric_bounds_map_to_their_json_schema_keywords() -> None:
    def fn(
        ratio: Annotated[float, Field(gt=0, lt=1)],
        step: Annotated[int, Field(ge=0, le=10, multiple_of=2)],
    ) -> None:
        del ratio, step

    props = build_input_schema(fn)["properties"]
    assert props["ratio"] == {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1}
    assert props["step"] == {"type": "integer", "minimum": 0, "maximum": 10, "multipleOf": 2}


def test_a_length_bound_takes_the_keyword_of_what_it_bounds() -> None:
    def fn(
        code: Annotated[str, Field(min_length=2, max_length=3)],
        ids: Annotated[list[int], Field(min_length=1, max_length=5)],
        extra: Annotated[dict[str, Any], Field(min_length=0, max_length=4)],
        either: Annotated[int | str, Field(max_length=3)],
    ) -> None:
        del code, ids, extra, either

    props = build_input_schema(fn)["properties"]
    assert props["code"] == {"type": "string", "minLength": 2, "maxLength": 3}
    assert props["ids"] == {
        "type": "array",
        "items": {"type": "integer"},
        "minItems": 1,
        "maxItems": 5,
    }
    assert props["extra"] == {"type": "object", "minProperties": 0, "maxProperties": 4}
    # No ``type`` to say which keyword applies, so the bound is not advertised.
    assert props["either"] == {"anyOf": [{"type": "integer"}, {"type": "string"}]}


def test_a_field_s_title_examples_and_pattern_are_advertised() -> None:
    def fn(
        slug: Annotated[str, Field(title="Slug", examples=["a-b"], pattern="^[a-z-]+$")],
        compiled: Annotated[str, Field(pattern=re.compile("^x"))],
        raw: Annotated[str, re.compile("^y")],
        binary: Annotated[str, Field(pattern=re.compile(b"^z"))],
    ) -> None:
        del slug, compiled, raw, binary

    props = build_input_schema(fn)["properties"]
    assert props["slug"] == {
        "type": "string",
        "title": "Slug",
        "examples": ["a-b"],
        "pattern": "^[a-z-]+$",
    }
    assert props["compiled"] == {"type": "string", "pattern": "^x"}
    assert props["raw"] == {"type": "string", "pattern": "^y"}
    # A bytes pattern has no JSON Schema spelling.
    assert props["binary"] == {"type": "string"}


def test_bare_annotated_types_metadata_maps_as_a_field_s_does() -> None:
    def fn(
        n: Annotated[int, annotated_types.Ge(1)],
        window: Annotated[int, annotated_types.Interval(gt=0, le=9)],
        name: Annotated[str, annotated_types.Len(1, 8)],
    ) -> None:
        del n, window, name

    props = build_input_schema(fn)["properties"]
    assert props["n"] == {"type": "integer", "minimum": 1}
    assert props["window"] == {"type": "integer", "exclusiveMinimum": 0, "maximum": 9}
    assert props["name"] == {"type": "string", "minLength": 1, "maxLength": 8}


def test_field_metadata_reaches_nested_and_merged_annotations() -> None:
    def fn(
        scores: list[Annotated[int, Field(ge=0)]],
        flattened: Annotated[Annotated[int, Field(ge=1)], Field(description="d")],
        later: Annotated[int, Field(ge=1), Field(ge=2)],
        anything: Annotated[Any, Field(description="Free form.")],
    ) -> None:
        del scores, flattened, later, anything

    props = build_input_schema(fn)["properties"]
    assert props["scores"] == {"type": "array", "items": {"type": "integer", "minimum": 0}}
    assert props["flattened"] == {"type": "integer", "minimum": 1, "description": "d"}
    assert props["later"] == {"type": "integer", "minimum": 2}
    assert props["anything"] == {"description": "Free form."}


def test_every_value_carried_serialises_to_json() -> None:
    def fn(
        day: Annotated[datetime.date, Field(ge=datetime.date(2020, 1, 1))],
        amount: Annotated[Decimal, Field(ge=Decimal("0"))],
        price: Annotated[float, Field(le=Decimal("9.99"), gt=0)],
        rate: Annotated[float, Field(examples=[Decimal("1.5")])],
        opaque: Annotated[int, Field(examples=[object()])],
    ) -> None:
        del day, amount, price, rate, opaque

    props = build_input_schema(fn)["properties"]
    json.dumps(props)
    assert props["day"] == {}
    assert props["amount"] == {}
    assert props["price"] == {"type": "number", "exclusiveMinimum": 0}
    assert props["rate"] == {"type": "number", "examples": ["1.5"]}
    assert props["opaque"] == {"type": "integer"}


def test_an_unresolvable_top_level_annotation_raises() -> None:
    # Under ``from __future__ import annotations`` every annotation is a string,
    # and `inspect.signature` evaluates the top level; only a nested reference
    # falls back to an empty fragment.
    def fn(x: Nope) -> None:  # noqa: F821
        del x

    with pytest.raises(NameError, match="Nope"):
        build_input_schema(fn)


def test_a_string_reference_nested_in_an_annotation_is_resolved() -> None:
    def fn(
        n: Annotated["int", Field(ge=1)],  # noqa: UP037
        tags: list[Annotated["int", "t"]],  # noqa: UP037
        plain: list["int"],  # noqa: UP037
        aliased: Annotated["_Count", "c"],  # noqa: UP037
    ) -> None:
        del n, tags, plain, aliased

    props = build_input_schema(fn)["properties"]
    assert props["n"] == {"type": "integer", "minimum": 1}
    assert props["tags"] == {"type": "array", "items": {"type": "integer"}}
    assert props["plain"] == {"type": "array", "items": {"type": "integer"}}
    # A module-level name, so the reference resolves in ``fn``'s globals.
    assert props["aliased"] == {"type": "integer"}


def test_a_reference_resolves_in_the_wrapped_function_s_globals() -> None:
    # ``inner`` is defined in a namespace of its own, holding ``_Hidden``, which
    # this module does not; ``wrapper``'s globals are this module's.
    namespace: dict[str, Any] = {"_Hidden": int}
    exec("def inner(ids: \"list['_Hidden']\") -> None: ...", namespace)
    inner = namespace["inner"]

    @functools.wraps(inner)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    props = build_input_schema(wrapper)["properties"]
    assert props["ids"] == {"type": "array", "items": {"type": "integer"}}


def test_an_unresolvable_nested_reference_falls_back_to_an_empty_fragment() -> None:
    def fn(
        missing: list[Annotated["Nope", "t"]],  # noqa: F821, UP037
        broken: list["1 +"],  # noqa: F722
    ) -> None:
        del missing, broken

    props = build_input_schema(fn)["properties"]
    assert props["missing"] == {"type": "array", "items": {}}
    assert props["broken"] == {"type": "array", "items": {}}


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
