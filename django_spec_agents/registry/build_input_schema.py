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
from pydantic_core import PydanticSerializationError, PydanticUndefined, to_jsonable_python

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
    """Derive a JSON Schema object from ``fn``'s parameters."""
    strategy = SchemaStrategy(schema_strategy)
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
        fragment = _hint_to_schema(_hint(param), globalns)
        if isinstance(param.default, FieldInfo):
            fragment = _field_default_schema(fragment, param.default)
            if param.default.is_required():
                required.append(param.name)
        elif param.default is inspect.Parameter.empty:
            required.append(param.name)
        properties[param.name] = fragment
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
    """The namespace `eval_str` evaluates ``fn``'s annotations in."""
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
    """``hint`` with every string and forward reference in it evaluated."""
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
    # ``forbid`` is pydantic's spelling of the hand-built schema's ``additionalProperties: false``.
    model = create_model(name, __config__=ConfigDict(extra="forbid"), **fields)
    dropped = [field for field in fields if field not in model.model_fields]
    if dropped:
        raise TypeError(
            f"the pydantic schema strategy cannot carry {dropped!r} of {name!r}: "
            "pydantic does not hold them as fields. Rename them or use "
            "SchemaStrategy.HAND_BUILT."
        )
    return model.model_json_schema(by_alias=False)


def _hint_to_schema(hint: Any, globalns: dict[str, Any]) -> dict[str, Any]:
    if isinstance(hint, (str, typing.ForwardRef)):
        # `eval_str` evaluates only a top-level string, so a nested one is handled here.
        try:
            hint = _resolve(hint, globalns)
        except Exception:
            return {}
    if hint is None:
        hint = type(None)
    if hint is Any:
        return {}
    origin = typing.get_origin(hint)
    if origin is typing.Annotated:
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
        non_none = [a for a in typing.get_args(hint) if a is not type(None)]
        if len(non_none) == 1:
            return {**_hint_to_schema(non_none[0], globalns), "nullable": True}
        return {"anyOf": [_hint_to_schema(a, globalns) for a in non_none]}
    return {}


def _field_default_schema(fragment: dict[str, Any], field: FieldInfo) -> dict[str, Any]:
    """``fragment`` with what a ``FieldInfo`` given as the default maps to."""
    schema = {**fragment, **_metadata_schema([field], fragment.get("type"))}
    if field.default is not PydanticUndefined:
        with contextlib.suppress(PydanticSerializationError, ValueError):
            schema["default"] = _json_value(field.default)
    return schema


def _json_value(value: Any) -> Any:
    """``value`` in JSON mode, as pydantic renders a default or examples."""
    result = to_jsonable_python(value)
    json.dumps(result, allow_nan=False)
    return result


def _metadata_schema(metadata: Iterable[Any], base_type: str | None) -> dict[str, Any]:
    """The JSON Schema keys ``Annotated`` metadata maps to; later pieces win."""
    schema: dict[str, Any] = {}
    for item in metadata:
        if isinstance(item, FieldInfo):
            for attribute in ("title", "description"):
                value = getattr(item, attribute)
                if value is not None:
                    schema[attribute] = value
            if item.examples is not None:
                with contextlib.suppress(PydanticSerializationError, ValueError):
                    schema["examples"] = _json_value(item.examples)
            schema.update(_metadata_schema(item.metadata, base_type))
            continue
        for attribute, key in _BOUND_KEYS:
            value = getattr(item, attribute, None)
            if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
                schema[key] = value
        pattern = getattr(item, "pattern", None)
        if isinstance(pattern, re.Pattern):
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
