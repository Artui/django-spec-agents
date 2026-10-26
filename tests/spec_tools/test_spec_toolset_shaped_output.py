"""The return schema of a spec declaring ``shapes``, which promises no row field.

A shape reaches the presenter alone, and what it does there is the presenter's:
a ``fields`` selection leaves keys out, so no key of a row is present under
every value the model may send. The return schema says so by requiring none,
at any depth, while the paging envelope and the ``affordances`` answers, which
the toolset and dispatch write whatever the shapes, stay required.
"""

from __future__ import annotations

from typing import Any

import pytest
from django_service_specs import (
    Affordance,
    Output,
    OutputField,
    Parameter,
    Parameters,
    SelectorKind,
    SelectorSpec,
    ServiceSpec,
)

from django_spec_agents.spec_tools import SpecToolset
from tests.spec_tools.kernel_specs import RowPresenter, allow

_FIELDS = Parameter("fields", "string", help="Comma-separated fields to return.")


def _rows(**_: Any) -> list[dict[str, Any]]:
    """List the projects."""
    return []


def _row(**_: Any) -> dict[str, Any] | None:
    """Fetch one project."""
    return None


def _presenter() -> RowPresenter:
    """A project with a nested owner and a list of tags, each always present."""
    return RowPresenter(
        OutputField("name", "string"),
        OutputField(
            "owner",
            "object",
            fields=Output((OutputField("id", "integer"), OutputField("email", "string"))),
        ),
        OutputField("tags", "array", items=Output((OutputField("label", "string"),))),
    )


def _selector(kind: SelectorKind, **kwargs: Any) -> SelectorSpec:
    return SelectorSpec(
        kind=kind,
        selector=_rows if kind is SelectorKind.LIST else _row,
        presenter=_presenter(),
        permissions=allow(),
        **kwargs,
    )


def _required_anywhere(node: Any) -> list[list[str]]:
    """Every ``required`` list at any depth of ``node``."""
    if isinstance(node, list):
        return [found for entry in node for found in _required_anywhere(entry)]
    if not isinstance(node, dict):
        return []
    found = [node["required"]] if "required" in node else []
    return found + [entry for value in node.values() for entry in _required_anywhere(value)]


def _return_schema(spec: Any) -> dict[str, Any]:
    schema = SpecToolset({"tool": spec})._tool_defs["tool"].return_schema
    assert schema is not None
    return schema


def test_a_shaped_list_requires_no_row_field_at_any_depth_and_keeps_its_envelope():
    """The item, its nested object and its array's rows require nothing, and
    the page envelope the toolset writes keeps its four keys required."""
    schema = _return_schema(_selector(SelectorKind.LIST, shapes=Parameters.of(_FIELDS)))

    item = schema["properties"]["items"]["items"]
    assert set(item["properties"]) == {"name", "owner", "tags"}
    assert _required_anywhere(item) == []
    assert schema["required"] == ["items", "page", "totalPages", "hasNext"]


def test_a_list_declaring_no_shape_keeps_every_required():
    """Holds the ``shapes`` guard in ``_return_schema``: dropping it would leave
    an unshaped spec's rows promising nothing."""
    schema = _return_schema(_selector(SelectorKind.LIST))

    item = schema["properties"]["items"]["items"]
    assert item["required"] == ["name", "owner", "tags"]
    assert item["properties"]["owner"]["required"] == ["id", "email"]
    assert item["properties"]["tags"]["items"]["required"] == ["label"]


@pytest.mark.parametrize(
    ("spec", "item_of"),
    [
        pytest.param(
            _selector(SelectorKind.RETRIEVE, shapes=Parameters.of(_FIELDS), allow_none=True),
            lambda schema: schema,
            id="a-retrieve-admitting-null",
        ),
        pytest.param(
            ServiceSpec(
                service=_row,
                atomic=False,
                presenter=_presenter(),
                shapes=Parameters.of(_FIELDS),
                permissions=allow(),
            ),
            lambda schema: schema,
            id="a-service-presenting-its-return",
        ),
        pytest.param(
            ServiceSpec(
                service=_rows,
                atomic=False,
                returns_many=True,
                presenter=_presenter(),
                shapes=Parameters.of(_FIELDS),
                permissions=allow(),
            ),
            lambda schema: schema["items"],
            id="a-service-returning-an-array",
        ),
    ],
)
def test_every_shaped_item_requires_nothing_wherever_it_sits(spec, item_of):
    """One row or an array of them, the item is found where the schema puts it."""
    item = item_of(_return_schema(spec))

    assert set(item["properties"]) == {"name", "owner", "tags"}
    assert _required_anywhere(item) == []


def test_a_shaped_row_keeps_the_affordances_dispatch_adds_required():
    """``affordances`` is written onto each row after the presenter runs, so a
    shape cannot leave it out: it stays required, with its own schema whole,
    while the presenter's fields are not."""
    cancel = ServiceSpec(
        service=lambda **_: None,
        atomic=False,
        affordances=[Affordance(code="closed", reason="Closed.", when=lambda **_: False)],
    )
    shaped = _selector(
        SelectorKind.LIST, shapes=Parameters.of(_FIELDS), affordances={"cancel": cancel}
    )
    plain = _selector(SelectorKind.LIST, affordances={"cancel": cancel})

    item = _return_schema(shaped)["properties"]["items"]["items"]
    unshaped = _return_schema(plain)["properties"]["items"]["items"]

    assert item["required"] == ["affordances"]
    assert item["properties"]["affordances"] == unshaped["properties"]["affordances"]
    assert _required_anywhere(item["properties"]["name"]) == []
    assert _required_anywhere(item["properties"]["owner"]) == []


def test_a_shaped_spec_with_no_presenter_still_returns_none():
    """A shape with nothing to present it declares no return schema, rather
    than one relaxed out of nothing."""
    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=_rows,
        shapes=Parameters.of(_FIELDS),
        permissions=allow(),
    )

    assert SpecToolset({"tool": spec})._tool_defs["tool"].return_schema is None
