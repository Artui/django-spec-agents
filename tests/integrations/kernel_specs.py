"""Kernel-declared specs the spec-capability builder's and resolver's tests serve.

``build_spec_capability`` hands its source to ``spec_tools``, which serves
django-service-specs specs only, so these are the kernel's own ``ServiceSpec``
and ``SelectorSpec``. The drf-mcp bridge's tests keep their drf-services
declarations in ``drf_specs_lookup``, because that bridge registers nothing
else.
"""

from __future__ import annotations

from typing import Any

from django_service_specs import (
    Parameter,
    Parameters,
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
