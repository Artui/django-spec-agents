from __future__ import annotations

from enum import Enum


class SchemaStrategy(str, Enum):
    """How [`build_input_schema`][django_spec_agents.build_input_schema] derives a
    tool's JSON Schema from its signature.

    ``HAND_BUILT`` is the default and the package's own mapping: a flat schema
    with no titles, ``X | None`` as ``nullable``, and an unrecognised type as an
    empty fragment. ``PYDANTIC`` hands the parameters to pydantic and returns its
    JSON Schema: titles on every property, ``X | None`` as ``anyOf``, defaults,
    and ``$defs`` for nested models and enums.
    """

    HAND_BUILT = "hand_built"
    PYDANTIC = "pydantic"


__all__ = ["SchemaStrategy"]
