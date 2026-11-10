from __future__ import annotations

from enum import Enum


class SchemaStrategy(str, Enum):
    """How a tool's JSON Schema is derived from its signature."""

    HAND_BUILT = "hand_built"
    PYDANTIC = "pydantic"


__all__ = ["SchemaStrategy"]
