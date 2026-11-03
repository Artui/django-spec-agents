"""Fixture django-spec-mcp servers over kernel specs, for the ``[mcp]`` bridge's tests.

Kernel specs only: django-spec-mcp registers nothing else, and nothing here
imports Django REST Framework, so the bridge's tests hold without it.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django_service_specs import (
    AdditionalInputRequired,
    Affordance,
    DispatchError,
    Output,
    OutputField,
    Parameter,
    Parameters,
    PermissionCheck,
    Presenter,
    SelectorKind,
    SelectorSpec,
    ServiceError,
    ServiceSpec,
    ServiceValidationError,
    Unrestricted,
)
from django_spec_mcp import MCPServer

from tests.integrations.kernel_specs import LOOKUP_SPECS

_TWO_NUMBERS = Parameters.of(
    Parameter("a", "integer", required=True), Parameter("b", "integer", required=True)
)
_OPEN = [Unrestricted()]


def add_numbers(*, a: int, b: int) -> dict[str, Any]:
    """Add two numbers."""
    return {"result": a + b}


def reject_input(*, a: int, b: int) -> dict[str, Any]:
    """Refuse the numbers."""
    raise ServiceValidationError("must be even")


def deny_by_policy(*, a: int, b: int) -> dict[str, Any]:
    """Refuse by a business rule."""
    raise ServiceError("denied by policy")


def close_the_books() -> dict[str, Any]:
    """Close the books."""
    return {"status": "closed"}


def ask_for_a_reason() -> dict[str, Any]:
    """Ask for the reason the books are being closed."""
    raise AdditionalInputRequired(
        "Say why the books are being closed.", schema={"reason": {"type": "string"}}
    )


def ask_which_row() -> dict[str, Any]:
    """Ask which row to read, from a selector."""
    raise AdditionalInputRequired("Say which row.", schema={"reason": {"type": "string"}})


def refuse_to_dispatch() -> dict[str, Any]:
    """Raise a bare dispatch error."""
    raise DispatchError("The call could not be dispatched.")


def missing_row(*, pk: int) -> dict[str, Any]:
    """Read a row that is not there."""
    raise ObjectDoesNotExist


class _Refuse(PermissionCheck):
    """Nobody may run the spec, so django-spec-mcp answers JSON-RPC ``-32006``."""

    def has_permission(self, principal: Any, spec: Any) -> bool:
        return False


# Whether the books are open, which the affordance below reads. Closed unless a
# test opens them, so every call is refused before the service runs. Both
# bridges leave a tool out of a step's tools while its operation condition is
# unmet, so an agent meets this refusal only when the books close between a
# step offering the tool and its call, which is what a test opening them models.
BOOKS = {"open": False}

# Shared with the spec-tools route, which the tests build a capability over, so
# the same refusal is asserted to read identically through both.
REFUSED_SPEC = ServiceSpec(
    service=close_the_books,
    permissions=_OPEN,
    atomic=False,
    affordances=[
        Affordance(code="books_closed", reason="The books are closed.", when=lambda: BOOKS["open"])
    ],
)

# A selector asking for input, which django-spec-mcp never asks a client for: it
# answers a service error. Shared with the spec-tools route to compare the two.
SELECTOR_ASKS_SPEC = SelectorSpec(
    kind=SelectorKind.RETRIEVE,
    selector=ask_which_row,
    reads=Parameters.of(Parameter("reason", "string")),
    permissions=_OPEN,
)

server = MCPServer(name="test")
server.register_tool(
    ServiceSpec(service=add_numbers, reads=_TWO_NUMBERS, permissions=_OPEN, atomic=False),
    name="add",
    description="Add two numbers.",
)
server.register_tool(
    ServiceSpec(service=reject_input, reads=_TWO_NUMBERS, permissions=_OPEN, atomic=False),
    name="invalid",
    description="Refuse the numbers.",
)
server.register_tool(
    ServiceSpec(service=deny_by_policy, reads=_TWO_NUMBERS, permissions=_OPEN, atomic=False),
    name="denied",
    description="Refuse by a business rule.",
)
server.register_tool(REFUSED_SPEC, name="refused", description="Close the books.")
# The service asks for ``reason``, which its spec declares, so django-spec-mcp
# can ask for it; in process it cannot ask anyone, and degrades the request to an
# ``input_required`` result naming it.
server.register_tool(
    ServiceSpec(
        service=ask_for_a_reason,
        reads=Parameters.of(Parameter("reason", "string")),
        permissions=_OPEN,
        atomic=False,
    ),
    name="needs_input",
    description="Close the books, given a reason.",
)
# The same request from a spec that does not declare ``reason``: an answer would
# be refused, so django-spec-mcp answers JSON-RPC ``-32603`` rather than asking.
server.register_tool(
    ServiceSpec(service=ask_for_a_reason, permissions=_OPEN, atomic=False),
    name="undeclared_input",
    description="Close the books, asking for a reason it does not take.",
)
server.register_tool(
    ServiceSpec(service=add_numbers, reads=_TWO_NUMBERS, permissions=[_Refuse()], atomic=False),
    name="forbidden",
    description="Add two numbers, for nobody.",
)
server.register_tool(
    ServiceSpec(service=refuse_to_dispatch, permissions=_OPEN, atomic=False),
    name="undispatchable",
    description="Raise a bare dispatch error.",
)
server.register_tool(
    SELECTOR_ASKS_SPEC, name="selector_asks", description="Ask which row, from a selector."
)
server.register_tool(
    SelectorSpec(
        kind=SelectorKind.RETRIEVE,
        selector=missing_row,
        reads=Parameters.of(Parameter("pk", "integer", required=True)),
        permissions=_OPEN,
    ),
    name="missing_row",
    description="Read a row that is not there.",
)

# The row-by-``pk`` specs the spec-tools route's tests build a capability over,
# so what each route asks of a model is compared for one spec.
lookup_server = MCPServer(name="lookup")
lookup_server.register_tool(
    LOOKUP_SPECS["get_row"], name="get_row", description="Read one row by its primary key."
)
lookup_server.register_tool(
    LOOKUP_SPECS["rename_row"], name="rename_row", description="Rename one row."
)


class SelectableRow(Presenter):
    """A row that reads its own ``fields`` shape and refuses a name it lacks.

    No selection library behind it: the server never reads the value, so the
    contract the bridge relies on is only that a presenter raising a
    ``ServiceValidationError`` while rendering comes back as a result it can
    retry.
    """

    def output(self) -> Output:
        return Output(fields=(OutputField("id", "integer"), OutputField("name", "string")))

    def present(self, value: Any) -> Any:
        return value

    def presented(self, value: Any, context: Any) -> Any:
        raw = context.shapes.get("fields") if context is not None else None
        if not raw:
            return value
        wanted = [name.strip() for name in raw.split(",")]
        for name in wanted:
            if name not in value:
                raise ServiceValidationError(f"Unknown field `{name}`.")
        return {name: value[name] for name in wanted}


def list_rows() -> list[dict[str, Any]]:
    """List the rows."""
    return [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


paged_server = MCPServer(name="paged")
paged_server.register_tool(
    SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_rows,
        shapes=Parameters.of(Parameter("fields", "string")),
        presenter=SelectableRow(),
        permissions=_OPEN,
    ),
    name="list_rows",
    description="List rows.",
    paginate=True,
)

# A tool advertised without an ``outputSchema``, which the bridge must pass on as
# absent rather than as a schema of ``None``.
bare_server = MCPServer(name="bare")
bare_server.register_tool(
    ServiceSpec(service=close_the_books, permissions=_OPEN, atomic=False),
    name="close_the_books",
    description="Close the books.",
    include_output_schema=False,
    include_structured_content=False,
)

# One mutation per ``annotations=`` override a project can register it with, for
# the bridge's destructive-hint rule. The wire each produces is asserted where
# the rule is, so a change to django-spec-mcp's merge fails there.
hinted_server = MCPServer(name="hinted")
for _name, _annotations in (
    ("plain", None),
    ("read_only", {"readOnlyHint": True}),
    ("non_destructive", {"destructiveHint": False}),
    ("destructive", {"destructiveHint": True}),
):
    hinted_server.register_tool(
        ServiceSpec(service=add_numbers, reads=_TWO_NUMBERS, permissions=_OPEN, atomic=False),
        name=_name,
        description="Add two numbers.",
        annotations=_annotations,
    )
