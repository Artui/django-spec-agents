from __future__ import annotations

import contextlib
import functools
import inspect
import json
import math
import re
import types
import typing
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import ConfigDict, create_model
from pydantic.fields import FieldInfo
from pydantic_core import PydanticSerializationError, to_jsonable_python

from django_spec_agents.registry.utils import run_context_parameter
from django_spec_agents.types.schema_strategy import SchemaStrategy
from django_spec_agents.types.tool_category import ToolCategory
from django_spec_agents.types.x_category_key import X_CATEGORY_KEY
from django_spec_agents.types.x_confirm_key import X_CONFIRM_KEY
from django_spec_agents.types.x_destructive_key import X_DESTRUCTIVE_KEY
from django_spec_agents.types.x_summary_key import X_SUMMARY_KEY

# Constraint attributes shared by `annotated_types` metadata and pydantic's own
# (``Field(pattern=...)``), read by name so either kind maps the same way.
_BOUND_KEYS = (
    ("gt", "exclusiveMinimum"),
    ("ge", "minimum"),
    ("lt", "exclusiveMaximum"),
    ("le", "maximum"),
    ("multiple_of", "multipleOf"),
)

# A length bound's JSON Schema keyword depends on what it bounds, as pydantic's does.
_LENGTH_KEYS = {
    "string": ("minLength", "maxLength"),
    "array": ("minItems", "maxItems"),
    "object": ("minProperties", "maxProperties"),
}


def build_input_schema(
    fn: Callable[..., Any],
    *,
    destructive: bool = False,
    category: ToolCategory = ToolCategory.OTHER,
    confirm: str | None = None,
    summary: str | None = None,
    schema_strategy: SchemaStrategy = SchemaStrategy.HAND_BUILT,
) -> dict[str, Any]:
    """Derive a JSON Schema object from ``fn``'s parameters.

    ``schema_strategy`` picks the derivation. The default,
    ``SchemaStrategy.HAND_BUILT``, covers ``str``, ``int``, ``float``,
    ``bool``, ``list[T]``, ``dict[str, Any]`` and ``X | None`` unions, and
    reads ``Annotated[T, ...]`` as ``T`` plus what its metadata maps to: a
    ``Field``'s ``description``, ``title`` and ``examples``, and the bounds of a
    ``Field`` or of ``annotated_types`` metadata. A string or forward reference
    nested in an annotation is resolved in the globals the top level is
    evaluated in. Anything richer, or a nested reference that does not resolve,
    falls back to an empty fragment, imposing no type constraint but staying
    wire-valid; so
    does a bound that is not an ``int`` or ``float`` (a ``Decimal``, a
    ``date``), and ``examples`` that do not serialise to JSON. An annotation
    that does not evaluate at the top level raises ``NameError`` under either
    strategy, from `inspect.signature`.

    ``SchemaStrategy.PYDANTIC`` builds a pydantic model over the same
    parameters and returns its JSON Schema, so it covers whatever pydantic
    does, and raises where pydantic does: on a type it cannot schematise, an
    unresolvable reference (``NameError``), or a parameter pydantic would not
    hold as a field (``TypeError``).

    ``destructive`` / ``category`` / ``confirm`` / ``summary`` are stamped at the
    schema root as the matching ``x-*`` extension keys, which AG-UI passes
    through verbatim to the client.

    A leading ``ctx: RunContext[...]`` parameter is **not** an argument and is
    left out. Pydantic-AI fills it from the run — it is how a tool reaches the
    acting user — so advertising it would ask the model to invent a value for
    something it cannot supply.

    Raises:
        ValueError: when ``schema_strategy`` is not a ``SchemaStrategy``.
    """
    strategy = SchemaStrategy(schema_strategy)
    # `eval_str=True` resolves string annotations and leaves the result as written.
    # `typing.get_type_hints` would also strip `Annotated`, resolve forward
    # references nested in a generic and turn a bare ``None`` into ``type(None)``;
    # `_hint_to_schema` unwraps `Annotated`, resolves a nested reference and reads
    # a bare ``None`` itself.
    sig = inspect.signature(fn, eval_str=True)
    globalns = _signature_globals(fn)
    context_parameter = run_context_parameter(sig)
    arguments = [
        param
        for name, param in sig.parameters.items()
        if name != context_parameter
        and param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    if strategy is SchemaStrategy.PYDANTIC:
        return _stamp(
            _pydantic_schema(fn, arguments, globalns), destructive, category, confirm, summary
        )
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in arguments:
        properties[param.name] = _hint_to_schema(_hint(param), globalns)
        if param.default is inspect.Parameter.empty:
            required.append(param.name)
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    _stamp(schema, destructive, category, confirm, summary)
    if required:
        schema["required"] = required
    return schema


def _signature_globals(fn: Callable[..., Any]) -> dict[str, Any]:
    """The namespace `eval_str` evaluates ``fn``'s annotations in.

    So a nested reference resolves as a top-level one does. `inspect.signature`
    reads a partial through its ``func`` and a callable instance through
    ``type(fn).__call__``, neither of which has ``__globals__`` of its own; a
    builtin has none anywhere, and nothing to resolve in one. A bound method
    reads its function's, which is why it is not sent to ``__call__``
    (`test_a_reference_resolves_in_a_bound_method_s_globals` fails without
    the ``ismethod`` check).
    """
    target = inspect.unwrap(fn)
    if isinstance(target, functools.partial):
        target = inspect.unwrap(target.func)
    if not (inspect.isfunction(target) or inspect.ismethod(target)):
        target = inspect.unwrap(type(target).__call__)
    return getattr(target, "__globals__", {})


def _hint(param: inspect.Parameter) -> Any:
    return Any if param.annotation is inspect.Parameter.empty else param.annotation


def _stamp(
    schema: dict[str, Any],
    destructive: bool,
    category: ToolCategory,
    confirm: str | None,
    summary: str | None,
) -> dict[str, Any]:
    schema[X_CATEGORY_KEY] = category.value
    if destructive:
        schema[X_DESTRUCTIVE_KEY] = True
    if confirm is not None:
        schema[X_CONFIRM_KEY] = confirm
    if summary is not None:
        schema[X_SUMMARY_KEY] = summary
    return schema


def _resolve(hint: Any, globalns: dict[str, Any]) -> Any:
    """``hint`` with every string and forward reference in it evaluated.

    `typing.get_type_hints` is the public evaluator of nested references; handed
    a holder rather than ``fn``, it reads one annotation and nothing else of the
    function, and ``include_extras`` keeps ``Annotated``.
    """
    holder = types.SimpleNamespace(__annotations__={"hint": hint})
    return typing.get_type_hints(holder, globalns=globalns, include_extras=True)["hint"]


def _pydantic_schema(
    fn: Callable[..., Any], arguments: list[inspect.Parameter], globalns: dict[str, Any]
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        param.name: (
            _resolve(_hint(param), globalns),
            ... if param.default is inspect.Parameter.empty else param.default,
        )
        for param in arguments
    }
    name = getattr(fn, "__name__", type(fn).__name__)
    # ``forbid`` is pydantic's spelling of the hand-built schema's
    # ``additionalProperties: false``.
    model = create_model(name, __config__=ConfigDict(extra="forbid"), **fields)
    # Pydantic reads a leading underscore as a private attribute and
    # ``model_config`` as configuration, and drops either from the fields
    # without a word; a schema missing an argument the tool takes is worse
    # than none.
    dropped = [field for field in fields if field not in model.model_fields]
    if dropped:
        raise TypeError(
            f"the pydantic schema strategy cannot carry {dropped!r} of {name!r}: "
            "pydantic does not hold them as fields. Rename them or use "
            "SchemaStrategy.HAND_BUILT."
        )
    return model.model_json_schema()


def _hint_to_schema(hint: Any, globalns: dict[str, Any]) -> dict[str, Any]:
    if isinstance(hint, (str, typing.ForwardRef)):
        # `eval_str` evaluates a top-level string, so this is one nested in
        # ``Annotated["int", ...]`` or ``list["int"]``.
        try:
            hint = _resolve(hint, globalns)
        except Exception:
            # Evaluating the string can raise anything its code does (a
            # ``NameError``, a ``SyntaxError``); the fragment stays empty, as it
            # was before references were resolved, rather than failing a tool
            # that registered.
            return {}
    # A bare ``None`` annotation means ``type(None)``, but `eval_str` leaves it
    # as the ``None`` object where `get_type_hints` would not.
    if hint is None:
        hint = type(None)
    if hint is Any:
        return {}
    origin = typing.get_origin(hint)
    if origin is typing.Annotated:
        # Python flattens nested ``Annotated``, so every piece of metadata is here.
        base, *metadata = typing.get_args(hint)
        schema = _hint_to_schema(base, globalns)
        return {**schema, **_metadata_schema(metadata, schema.get("type"))}
    if origin is None:
        return _scalar_schema(hint)
    if origin in (list, tuple, set, frozenset):
        args = typing.get_args(hint)
        items = _hint_to_schema(args[0], globalns) if args else {}
        return {"type": "array", "items": items}
    if origin is dict:
        return {"type": "object"}
    if origin is typing.Union or origin is types.UnionType:
        # ``Union[T]`` collapses to ``T`` in Python, so a union here always
        # has >= 2 distinct args. Exactly one non-``None`` arg means the
        # ``X | None`` shape; expose that as a nullable scalar.
        non_none = [a for a in typing.get_args(hint) if a is not type(None)]
        if len(non_none) == 1:
            return {**_hint_to_schema(non_none[0], globalns), "nullable": True}
        return {"anyOf": [_hint_to_schema(a, globalns) for a in non_none]}
    return {}


def _metadata_schema(metadata: Iterable[Any], base_type: str | None) -> dict[str, Any]:
    """The JSON Schema keys ``Annotated`` metadata maps to; later pieces win.

    A ``FieldInfo`` gives its ``title`` (only when one was passed: pydantic's
    derived titles are the pydantic strategy's), ``description`` and
    ``examples`` (in JSON mode, left out when they do not serialise), and its
    bounds through ``metadata``, which holds the same
    ``annotated_types`` objects a bare ``Annotated[int, Ge(1)]`` does. Anything
    else, a plain string say, maps to nothing.
    """
    schema: dict[str, Any] = {}
    for item in metadata:
        if isinstance(item, FieldInfo):
            for attribute in ("title", "description"):
                value = getattr(item, attribute)
                if value is not None:
                    schema[attribute] = value
            if item.examples is not None:
                # JSON mode, as pydantic renders examples: a ``Decimal`` becomes
                # its string, and examples that do not serialise are left out.
                # A non-finite float survives JSON mode, but JSON has no
                # spelling for it, so ``allow_nan=False`` leaves those out too.
                with contextlib.suppress(PydanticSerializationError, ValueError):
                    examples = to_jsonable_python(item.examples)
                    json.dumps(examples, allow_nan=False)
                    schema["examples"] = examples
            schema.update(_metadata_schema(item.metadata, base_type))
            continue
        for attribute, key in _BOUND_KEYS:
            value = getattr(item, attribute, None)
            # JSON Schema bounds are numbers; a ``Decimal`` or ``date`` bound
            # would not serialise, and has no keyword of its own here, and JSON
            # has no infinity or NaN. Only a float can be non-finite, and
            # ``isfinite`` overflows on an ``int`` past float range
            # (`test_a_non_finite_bound_or_example_is_left_out` fails without
            # ``isfinite`` or the ``int`` branch first,
            # `test_every_value_carried_serialises_to_json` without the
            # ``float`` check).
            if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
                schema[key] = value
        pattern = getattr(item, "pattern", None)
        if isinstance(pattern, re.Pattern):
            # ``Field(pattern=re.compile(...))`` keeps the compiled pattern.
            pattern = pattern.pattern
        if isinstance(pattern, str):
            schema["pattern"] = pattern
        length_keys = _LENGTH_KEYS.get(base_type or "")
        if length_keys is not None:
            for attribute, key in zip(("min_length", "max_length"), length_keys, strict=True):
                value = getattr(item, attribute, None)
                if value is not None:
                    schema[key] = value
    return schema


def _scalar_schema(hint: Any) -> dict[str, Any]:
    if hint is bool:
        return {"type": "boolean"}
    if hint is str:
        return {"type": "string"}
    if hint is int:
        return {"type": "integer"}
    if hint is float:
        return {"type": "number"}
    if hint is type(None):
        return {"type": "null"}
    if hint in (list, tuple, set, frozenset):
        return {"type": "array", "items": {}}
    if hint is dict:
        return {"type": "object"}
    return {}


__all__ = ["build_input_schema"]
