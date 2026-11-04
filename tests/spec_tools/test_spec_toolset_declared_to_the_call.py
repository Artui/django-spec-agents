"""A Validator that declares itself to the call: the toolset hands the call's context to the kernel.

django-service-specs reads a Validator declaring ``parameters(self, *,
call_context=None)`` as it declares itself to the context it is handed. The
tool's schema and the construction checks describe the spec to no call; a tool
call is under way, so the bound values and the server-owned keys are read as the
Validator declares itself to that call. Written against ``SpecToolset``'s public
surface only, so each test compiles against a toolset that hands no context and
fails there on what it asserts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django_service_specs import (
    DEFAULT_POOL_SEEDS,
    Parameter,
    Parameters,
    PoolSeeds,
    ServerArguments,
    ServiceSpec,
    ValidationContext,
    Validator,
)

from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import SpecToolset
from tests.spec_tools.kernel_specs import allow
from tests.spec_tools.test_spec_toolset import _input_schema_of, ctx_for

_MARKED: dict[str, Any] = {"marked": True}
"""The run's context, which each Validator here declares itself to once it reaches a call."""


def _marked(call_context: dict[str, Any] | None) -> bool:
    """Whether ``call_context`` is the call's own, carrying the run's entry.

    Keyed on what the dict carries, not on its being there, so a test reading
    it proves the run's context reached the kernel's helper through the call.
    """
    return bool(call_context is not None and call_context.get("marked"))


class _TypedByTheCall(Validator):
    """Declares ``project_pk`` a string to no call, and an integer to the run's calls."""

    def parameters(self, *, call_context: dict[str, Any] | None = None) -> Parameters:
        return Parameters.of(
            Parameter("project_pk", "integer" if _marked(call_context) else "string")
        )

    def validate(self, arguments: Mapping[str, Any], context: ValidationContext) -> dict[str, Any]:
        return dict(arguments)


class _TenantDeclared(Validator):
    """Declares ``title``, and ``tenant`` beside it either to the run's calls or to no call.

    ``to_the_call=True`` declares the field only to a call, ``False`` only to
    the description the tool's schema is built from.
    """

    def __init__(self, *, to_the_call: bool) -> None:
        self.to_the_call = to_the_call

    def parameters(self, *, call_context: dict[str, Any] | None = None) -> Parameters:
        title = Parameter("title", "string")
        if _marked(call_context) is self.to_the_call:
            return Parameters.of(title, Parameter("tenant", "string"))
        return Parameters.of(title)

    def validate(self, arguments: Mapping[str, Any], context: ValidationContext) -> dict[str, Any]:
        return dict(arguments)


def report(*, data: dict[str, Any], tenant: Any = None) -> dict[str, Any]:
    """Report the validated data and the service's ``tenant``, so a test sees each seat."""
    return {"data": data, "tenant": tenant}


def _service(validator: Validator) -> ServiceSpec:
    return ServiceSpec(service=report, validator=validator, atomic=False, permissions=allow())


def _tenant_seeds() -> PoolSeeds:
    return DEFAULT_POOL_SEEDS.extend(tenant=lambda *, user: f"tenant-of-{user.username}")


async def _call_in_the_run(toolset: SpecToolset, name: str, args: dict[str, Any]) -> Any:
    """Call ``name`` in a run whose ``call_context`` carries the marked entry."""
    deps = AgentDeps(user=User(username="acme"))
    deps.call_context = dict(_MARKED)
    ctx = ctx_for(deps=deps)
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, dict(args), ctx, tools[name])


async def test_a_bound_value_is_checked_against_the_call_s_own_declaration():
    """The call's declaration refuses the bound value: the server's error, not a retry.

    Construction checks the binding against the declaration made to no call,
    which takes ``"x"`` as a string, so the toolset builds. The call's own
    declares an integer. Handed no context, the binding passed the value on and
    the kernel's shape check answered it as the model's mistake, a
    ``ModelRetry`` naming ``project_pk``: a value the model neither sent nor
    was offered, so no retry could correct it.
    """
    toolset = SpecToolset(
        {"record": _service(_TypedByTheCall())},
        tool_server_arguments={"record": ServerArguments({"project_pk": "x"})},
    )

    with pytest.raises(ImproperlyConfigured, match="binds a value its parameter refuses"):
        await _call_in_the_run(toolset, "record", {})


async def test_a_seed_named_field_the_call_does_not_declare_is_answered_by_the_seed():
    """The schema offers ``tenant``; the call's declaration has no such field, so the seed owns it.

    Described to no call, the Validator declares ``tenant``, so the tool's
    schema offers it and the model sends it. In the call it declares no such
    field, so the registered seed is what fills the service's ``tenant``, and
    the model's value is dropped like any seed-named argument the spec
    declares. Handed no context, the server-owned keys were read off the
    declaration made to no call, which leaves the field to the caller: the
    model's value reached dispatch, which refused it as an unknown argument.
    """
    toolset = SpecToolset(
        {"record": _service(_TenantDeclared(to_the_call=False))}, pool_seeds=_tenant_seeds()
    )

    result = await _call_in_the_run(toolset, "record", {"title": "Dune", "tenant": "globex"})

    assert "tenant" in _input_schema_of(toolset, "record")["properties"]
    assert result == {"data": {"title": "Dune"}, "tenant": "tenant-of-acme"}


async def test_a_seed_named_field_declared_only_to_the_call_is_the_model_s_to_send():
    """The field the call declares is validated into ``data``; the service's ``tenant`` is the seed's.

    A pin rather than a falsification of the context: the model's argument is
    dropped only where the declaration made to no call names it, and that one
    has no ``tenant``, so the value reaches dispatch with or without the
    context handed to ``server_owned_keys``. What the call's context decides is
    the Validator's reading of it there, which takes the model's value as the
    field.
    """
    toolset = SpecToolset(
        {"record": _service(_TenantDeclared(to_the_call=True))}, pool_seeds=_tenant_seeds()
    )

    result = await _call_in_the_run(toolset, "record", {"title": "Dune", "tenant": "globex"})

    assert "tenant" not in _input_schema_of(toolset, "record")["properties"]
    assert result == {"data": {"title": "Dune", "tenant": "globex"}, "tenant": "tenant-of-acme"}
