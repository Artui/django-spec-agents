"""Kernel-declared specs the spec-capability builder's and resolver's tests serve.

``build_spec_capability`` hands its source to ``spec_tools``, which serves
django-service-specs specs only, so these are the kernel's own ``ServiceSpec``
and ``SelectorSpec``. The ``[mcp]`` bridge's server, in ``mcp_server``,
registers ``LOOKUP_SPECS`` too, so both routes serve one declaration.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django_service_specs import (
    FormValidator,
    Output,
    OutputField,
    Parameter,
    Parameters,
    Presenter,
    SelectorKind,
    SelectorSpec,
    ServiceSpec,
    Unrestricted,
)

# Read, never written: ``rename_row`` returns a renamed copy, so no test leaks a
# rename into the next.
_ROWS: dict[int, dict[str, Any]] = {1: {"id": 1, "name": "first"}}

# The one argument naming the row. A kernel spec says what it reads in
# ``reads=``, and a required read is what the toolset lists in ``required``.
_PK = Parameters.of(Parameter("pk", "integer", required=True))


def _ok(user: Any) -> dict[str, Any]:
    """A no-op spec service."""
    return {"ok": True}


def open_spec() -> ServiceSpec:
    """A spec that dispatches for anyone. Its permissions are declared because
    the spec capability refuses a spec that declares none."""
    return ServiceSpec(service=_ok, atomic=False, permissions=[Unrestricted()])


def row_by_pk(*, pk: int) -> dict[str, Any]:
    """Read one row by its primary key."""
    return _ROWS[pk]


def rename_row(*, instance: dict[str, Any], name: str) -> dict[str, Any]:
    """Rename one row."""
    return {**instance, "name": name}


GET_ROW_SPEC = SelectorSpec(
    kind=SelectorKind.RETRIEVE,
    selector=row_by_pk,
    reads=_PK,
    permissions=[Unrestricted()],
)

# The row arrives through ``instance_selector_spec``, so ``pk`` is the lookup's
# read and not the service's own: the case where a tool changing one row never
# told the model which argument names that row.
RENAME_ROW_SPEC = ServiceSpec(
    service=rename_row,
    reads=Parameters.of(Parameter("name", "string", required=True)),
    instance_selector_spec=SelectorSpec(kind=SelectorKind.RETRIEVE, selector=row_by_pk, reads=_PK),
    permissions=[Unrestricted()],
    atomic=False,
)

LOOKUP_SPECS: dict[str, Any] = {"get_row": GET_ROW_SPEC, "rename_row": RENAME_ROW_SPEC}


# ---------- list-shaped specs, served by both routes ----------


class _NamedRow(Presenter):
    """A row as its ``id`` and ``name``."""

    def output(self) -> Output:
        return Output(fields=(OutputField("id", "integer"), OutputField("name", "string")))

    def present(self, value: Any) -> Any:
        return {"id": value["id"], "name": value["name"]}


class _NameForm(forms.Form):
    """One row a ``many`` call names."""

    name = forms.CharField(max_length=100)


_TOUCHED: list[dict[str, Any]] = [{"id": 1, "name": "first"}, {"id": 2, "name": "second"}]


def touch_rows() -> list[dict[str, Any]]:
    """Touch the rows, returning the rows touched."""
    return _TOUCHED


def touch_nothing() -> None:
    """Touch the rows, returning nothing."""
    return None


def name_rows(*, data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Name several rows in one call."""
    return [{"id": index, "name": row["name"]} for index, row in enumerate(data, start=1)]


def _reread_rows(*, result: Any) -> Any:
    """The output re-read of a write that returns its rows: the rows as returned."""
    return result


# One spec per kind of list result a service presents: its own return
# (``returns_many``), the rows of a ``many`` call, and a ``LIST`` output re-read;
# plus a ``returns_many`` that may present nothing.
LIST_SPECS: dict[str, Any] = {
    "touch_rows": ServiceSpec(
        service=touch_rows,
        presenter=_NamedRow(),
        returns_many=True,
        permissions=[Unrestricted()],
        atomic=False,
    ),
    "name_rows": ServiceSpec(
        service=name_rows,
        validator=FormValidator(_NameForm),
        presenter=_NamedRow(),
        many=True,
        permissions=[Unrestricted()],
        atomic=False,
    ),
    "reread_rows": ServiceSpec(
        service=touch_rows,
        output_selector_spec=SelectorSpec(
            kind=SelectorKind.LIST, selector=_reread_rows, presenter=_NamedRow()
        ),
        permissions=[Unrestricted()],
        atomic=False,
    ),
    "touch_nothing": ServiceSpec(
        service=touch_nothing,
        presenter=_NamedRow(),
        returns_many=True,
        allow_none=True,
        permissions=[Unrestricted()],
        atomic=False,
    ),
}

# What each call sends: a ``many`` spec's list travels under its ``many_argument``.
LIST_CALLS: dict[str, dict[str, Any]] = {
    "touch_rows": {},
    "name_rows": {"items": [{"name": "first"}, {"name": "second"}]},
    "reread_rows": {},
    "touch_nothing": {},
}
