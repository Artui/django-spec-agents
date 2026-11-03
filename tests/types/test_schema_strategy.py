from __future__ import annotations

from django_spec_agents import SchemaStrategy


def test_schema_strategy_values() -> None:
    assert SchemaStrategy.HAND_BUILT.value == "hand_built"
    assert SchemaStrategy.PYDANTIC.value == "pydantic"
    assert [member.value for member in SchemaStrategy] == ["hand_built", "pydantic"]
