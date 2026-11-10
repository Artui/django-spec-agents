from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypedDict

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django_service_specs import (
    DEFAULT_POOL_SEEDS,
    Parameter,
    Parameters,
    PoolSeeds,
    SelectorKind,
    SelectorSpec,
    ServerArguments,
    ServiceSpec,
    ValidationContext,
    Validator,
    build_call_context,
    spec_caller_input_schema,
)
from pydantic_ai import ModelRetry, RunContext

from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import AGENT_TRANSPORT, SpecToolset
from django_spec_agents.spec_tools.spec_toolset import _required_arguments, _spec_ordering_argument
from tests.spec_tools.kernel_specs import allow
from tests.spec_tools.test_spec_toolset import _input_schema_of, ctx_for

_MARKED: dict[str, Any] = {"marked": True}
"""The run's context, which each Validator here declares itself to once it reaches a call."""


def _marked(call_context: dict[str, Any] | None) -> bool:
    """Whether ``call_context`` is the call's own, carrying the run's entry."""
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
    """Declares ``title``, and ``tenant`` beside it either to the run's calls or to no call."""

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


def _a_row(*, pk: int) -> dict[str, Any]:
    """A target lookup taking ``pk``, whose read is what these tests weigh a field against."""
    return {"pk": pk}


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
    """The call's declaration refuses the bound value: the server's error, not a retry."""
    toolset = SpecToolset(
        {"record": _service(_TypedByTheCall())},
        tool_server_arguments={"record": ServerArguments({"project_pk": "x"})},
    )

    with pytest.raises(
        ImproperlyConfigured,
        match=r"SpecToolset tool 'record': .*binds a value its parameter refuses",
    ):
        await _call_in_the_run(toolset, "record", {})


async def test_a_seed_named_field_the_call_does_not_declare_is_answered_by_the_seed():
    """The call's declaration has no ``tenant`` field, so the seed owns it."""
    toolset = SpecToolset(
        {"record": _service(_TenantDeclared(to_the_call=False))}, pool_seeds=_tenant_seeds()
    )

    result = await _call_in_the_run(toolset, "record", {"title": "Dune", "tenant": "globex"})

    assert "tenant" in _input_schema_of(toolset, "record")["properties"]
    assert result == {"data": {"title": "Dune"}, "tenant": "tenant-of-acme"}


async def test_a_seed_named_field_declared_only_to_the_call_is_the_model_s_to_send():
    """The call's declared field goes into ``data``; ``tenant`` is the seed's."""
    toolset = SpecToolset(
        {"record": _service(_TenantDeclared(to_the_call=True))}, pool_seeds=_tenant_seeds()
    )

    result = await _call_in_the_run(toolset, "record", {"title": "Dune", "tenant": "globex"})

    assert "tenant" not in _input_schema_of(toolset, "record")["properties"]
    assert result == {"data": {"title": "Dune", "tenant": "globex"}, "tenant": "tenant-of-acme"}


class _Owner(TypedDict):
    owner: int


def _fills_owner() -> _Owner:
    """A typed provider filling ``owner``, so the key is the server's on every call."""
    return _Owner(owner=1)


class _OwnerToTheCall(Validator):
    """Declares ``title``, and ``owner`` an integer only to the run's calls."""

    def parameters(self, *, call_context: dict[str, Any] | None = None) -> Parameters:
        title = Parameter("title", "string")
        if _marked(call_context):
            return Parameters.of(title, Parameter("owner", "integer"))
        return Parameters.of(title)

    def validate(self, arguments: Mapping[str, Any], context: ValidationContext) -> dict[str, Any]:
        return dict(arguments)


async def test_a_provider_filled_field_declared_only_to_the_call_drops_the_model_s_value():
    """The provider's ``owner`` is the one validated; the model's malformed guess costs no retry."""
    toolset = SpecToolset(
        {
            "record": ServiceSpec(
                service=report,
                validator=_OwnerToTheCall(),
                input_data=_fills_owner,
                atomic=False,
                permissions=allow(),
            )
        }
    )

    result = await _call_in_the_run(toolset, "record", {"title": "Dune", "owner": "nobody"})

    assert "owner" not in _input_schema_of(toolset, "record")["properties"]
    assert result == {"data": {"title": "Dune", "owner": 1}, "tenant": None}


class _ToOneTransport(Validator):
    """Declares ``title``, and a required ``channel`` beside it under one transport only."""

    def __init__(self, transport: str, *, name: str = "channel") -> None:
        self.transport = transport
        self.name = name

    def parameters(self, *, call_context: dict[str, Any] | None = None) -> Parameters:
        title = Parameter("title", "string", required=True)
        if call_context is not None and call_context["transport"] == self.transport:
            return Parameters.of(title, Parameter(self.name, "string", required=True))
        return Parameters.of(title)

    def validate(self, arguments: Mapping[str, Any], context: ValidationContext) -> dict[str, Any]:
        return dict(arguments)


async def test_a_required_field_declared_only_to_the_agent_transport_is_offered_and_required():
    """The schema is built under the agent's stamp, so the model is asked for the field."""
    toolset = SpecToolset({"record": _service(_ToOneTransport(AGENT_TRANSPORT))})

    schema = _input_schema_of(toolset, "record")
    result = await _call_in_the_run(toolset, "record", {"title": "Dune", "channel": "sms"})

    assert schema["properties"]["channel"] == {"type": "string"}
    assert sorted(schema["required"]) == ["channel", "title"]
    assert result == {"data": {"title": "Dune", "channel": "sms"}, "tenant": None}


async def test_a_field_declared_only_to_another_transport_is_not_offered():
    """The stamp is the agent's, so a field declared to the CLI is neither offered nor accepted."""
    toolset = SpecToolset({"record": _service(_ToOneTransport("cli"))})

    assert _input_schema_of(toolset, "record") == {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
        "additionalProperties": False,
    }
    with pytest.raises(ModelRetry, match="channel"):
        await _call_in_the_run(toolset, "record", {"title": "Dune", "channel": "sms"})


def test_a_validator_that_never_opted_in_is_described_as_it_was():
    """A ``parameters()`` taking no keyword is not handed the stamp, and its schema is unchanged."""

    class _NoKeyword(Validator):
        def parameters(self) -> Parameters:
            return Parameters.of(Parameter("title", "string", required=True))

        def validate(
            self, arguments: Mapping[str, Any], context: ValidationContext
        ) -> dict[str, Any]:
            return dict(arguments)

    toolset = SpecToolset({"record": _service(_NoKeyword())})

    assert _input_schema_of(toolset, "record") == {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
        "additionalProperties": False,
    }


class _Recording(Validator):
    """Records what it is handed, and writes into it as a stage of a call may."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.handed: list[dict[str, Any]] = []

    def parameters(self, *, call_context: dict[str, Any] | None = None) -> Parameters:
        if call_context is not None:
            self.handed.append(dict(call_context))
            call_context.setdefault("first_asked_by", self.label)
        return Parameters.of(Parameter("title", "string"))

    def validate(self, arguments: Mapping[str, Any], context: ValidationContext) -> dict[str, Any]:
        return dict(arguments)


def test_the_schema_s_asks_are_handed_the_stamp_alone():
    """No run is in hand, so the stamp names the transport and every other entry is ``None``."""
    validator = _Recording("one")

    SpecToolset({"record": _service(validator)})

    assert validator.handed[0] == {
        "request": None,
        "view": None,
        "format": None,
        "action": None,
        "transport": AGENT_TRANSPORT,
    }


def test_each_tool_s_schema_is_asked_with_a_stamp_of_its_own():
    """A stage may write into its context, and one tool's write never reaches another's schema."""
    first, second = _Recording("first"), _Recording("second")

    SpecToolset({"one": _service(first), "two": _service(second)})

    assert "first_asked_by" not in second.handed[0]


def test_a_clash_declared_only_to_the_agent_transport_is_refused_naming_the_tool():
    """Assembling the argument set under the stamp is the first read that sees this clash."""
    spec = ServiceSpec(
        service=report,
        validator=_ToOneTransport(AGENT_TRANSPORT, name="pk"),
        instance_selector_spec=SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=_a_row,
            reads=Parameters.of(Parameter("pk", "integer", required=True)),
        ),
        atomic=False,
        permissions=allow(),
    )

    with pytest.raises(
        ImproperlyConfigured,
        match=r"^SpecToolset tool 'rename': Parameters \['pk'\] are declared by two sources\.",
    ):
        SpecToolset({"rename": spec})


def test_a_field_named_for_a_dispatcher_seat_only_under_the_agent_is_refused_naming_the_tool():
    """``user`` is the dispatcher's seat, which a field declared only to the agent cannot take."""
    with pytest.raises(
        ImproperlyConfigured, match=r"^SpecToolset tool 'record': .*user"
    ) as excinfo:
        SpecToolset({"record": _service(_ToOneTransport(AGENT_TRANSPORT, name="user"))})

    assert not hasattr(excinfo.value, "__notes__")
    assert not hasattr(excinfo.value.__cause__, "__notes__")


async def test_a_seed_named_field_declared_only_to_the_agent_is_offered_and_is_the_model_s():
    """The schema's server-owned keys read the stamp as the call's do, so the two agree."""
    toolset = SpecToolset(
        {"record": _service(_ToOneTransport(AGENT_TRANSPORT, name="tenant"))},
        pool_seeds=_tenant_seeds(),
    )

    schema = _input_schema_of(toolset, "record")
    result = await _call_in_the_run(toolset, "record", {"title": "Dune", "tenant": "globex"})

    assert sorted(schema["required"]) == ["tenant", "title"]
    assert result == {"data": {"title": "Dune", "tenant": "globex"}, "tenant": "tenant-of-acme"}


def test_binding_a_field_declared_only_to_the_agent_is_refused_when_the_toolset_is_built():
    """``check_server_arguments`` reads the declaration made to no call, which has no such field."""
    with pytest.raises(ImproperlyConfigured, match=r"^SpecToolset tool 'record': .*channel"):
        SpecToolset(
            {"record": _service(_ToOneTransport(AGENT_TRANSPORT))},
            tool_server_arguments={"record": ServerArguments({"channel": "sms"})},
        )


def test_a_validator_reading_the_stamp_s_request_raises_when_the_toolset_is_built():
    """The stamp carries no request, so a Validator declaring ``call_context`` guards it."""

    class _ReadsTheRequest(Validator):
        def parameters(self, *, call_context: dict[str, Any] | None = None) -> Parameters:
            if call_context is None:
                return Parameters()
            return Parameters.of(Parameter(call_context["request"].user.username, "string"))

        def validate(
            self, arguments: Mapping[str, Any], context: ValidationContext
        ) -> dict[str, Any]:
            return dict(arguments)

    with pytest.raises(AttributeError, match="user") as excinfo:
        SpecToolset({"record": _service(_ReadsTheRequest())})

    assert excinfo.value.__notes__ == ["SpecToolset tool 'record'"]


async def test_the_required_reads_owe_no_context():
    """A call's missing-argument check names reads, and a Validator's field is never one."""
    spec = ServiceSpec(
        service=report,
        validator=_ToOneTransport(AGENT_TRANSPORT),
        instance_selector_spec=SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=_a_row,
            reads=Parameters.of(Parameter("pk", "integer", required=True)),
        ),
        atomic=False,
        permissions=allow(),
    )
    toolset = SpecToolset({"rename": spec})
    stamped = spec_caller_input_schema(spec, call_context=build_call_context(AGENT_TRANSPORT))

    assert "channel" in _input_schema_of(toolset, "rename")["required"]
    assert [name for name in stamped["required"] if name == "pk"] == list(_required_arguments(spec))
    with pytest.raises(ModelRetry) as refused:
        await _call_in_the_run(toolset, "rename", {"title": "Dune"})
    assert "pk" in str(refused.value)
    assert "channel" not in str(refused.value)


def test_a_list_selector_s_sort_owes_no_context():
    """A selector has no Validator, so its schema, and the sort read off it, ignores the stamp."""

    def rows(*, ordering: str = "") -> list[dict[str, Any]]:
        return []

    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=rows,
        reads=Parameters.of(Parameter("ordering", "string")),
        permissions=allow(),
    )
    stamped = spec_caller_input_schema(spec, call_context=build_call_context(AGENT_TRANSPORT))

    assert stamped == spec_caller_input_schema(spec)
    assert _spec_ordering_argument(spec) == "ordering"


class _RequiredByTheRun(Validator):
    """Declares ``title``, and a required ``tenant`` only to a call in the marked run."""

    def parameters(self, *, call_context: dict[str, Any] | None = None) -> Parameters:
        title = Parameter("title", "string")
        if _marked(call_context):
            return Parameters.of(title, Parameter("tenant", "string", required=True))
        return Parameters.of(title)

    def validate(self, arguments: Mapping[str, Any], context: ValidationContext) -> dict[str, Any]:
        return dict(arguments)


class _Tenant(TypedDict):
    tenant: str


def _fills_tenant(seedv: str) -> _Tenant:
    """A typed provider filling the Validator's ``tenant`` from the per-call seed ``seedv``."""
    return _Tenant(tenant=seedv)


class _SeededPerCall(SpecToolset):
    """Seeds each call with ``seedv`` and, when asked to, a ``tenant`` of its own."""

    def __init__(self, *args: Any, seed: str = "seedv", **kwargs: Any) -> None:
        self._seed = seed
        super().__init__(*args, **kwargs)

    def call_seeds(
        self, *, ctx: RunContext[Any], tool_name: str
    ) -> Mapping[str, Callable[..., Any]]:
        return {self._seed: lambda: "from-the-seed"}


async def test_a_field_required_by_the_run_is_filled_from_a_provider_reading_a_call_seed():
    """The remedy the quickstart names: an ``input_data=`` provider, which reads a per-call seed."""
    toolset = _SeededPerCall(
        {
            "record": ServiceSpec(
                service=report,
                validator=_RequiredByTheRun(),
                input_data=_fills_tenant,
                atomic=False,
                permissions=allow(),
            )
        }
    )

    result = await _call_in_the_run(toolset, "record", {"title": "Dune"})

    assert "tenant" not in _input_schema_of(toolset, "record")["properties"]
    assert result["data"] == {"title": "Dune", "tenant": "from-the-seed"}


async def test_a_seed_alone_does_not_fill_a_field_required_by_the_run():
    """A seed fills the service's parameter of its name, not the Validator field."""
    toolset = _SeededPerCall({"record": _service(_RequiredByTheRun())}, seed="tenant")

    with pytest.raises(ModelRetry, match="tenant"):
        await _call_in_the_run(toolset, "record", {"title": "Dune"})


class _OfferedWithoutARequest(Validator):
    """Declares ``title``, and ``tenant`` beside it only to a context carrying no request."""

    def parameters(self, *, call_context: dict[str, Any] | None = None) -> Parameters:
        title = Parameter("title", "string")
        if call_context is not None and call_context["request"] is None:
            return Parameters.of(title, Parameter("tenant", "string"))
        return Parameters.of(title)

    def validate(self, arguments: Mapping[str, Any], context: ValidationContext) -> dict[str, Any]:
        return dict(arguments)


async def _call_in_a_run_with_a_request(
    toolset: SpecToolset, name: str, args: dict[str, Any]
) -> Any:
    """Call ``name`` in a run whose ``call_context`` carries a ``request``."""
    deps = AgentDeps(user=User(username="acme"))
    deps.call_context["request"] = object()
    ctx = ctx_for(deps=deps)
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, dict(args), ctx, tools[name])


async def test_a_seed_named_field_only_the_schema_offers_is_answered_by_the_seed():
    """The stamp's declaration is a third set, which the call's weighing has to include."""
    toolset = SpecToolset(
        {"record": _service(_OfferedWithoutARequest())}, pool_seeds=_tenant_seeds()
    )

    result = await _call_in_a_run_with_a_request(
        toolset, "record", {"title": "Dune", "tenant": "globex"}
    )

    assert "tenant" in _input_schema_of(toolset, "record")["properties"]
    assert result == {"data": {"title": "Dune"}, "tenant": "tenant-of-acme"}


async def test_a_plain_field_only_the_schema_offers_is_refused_as_unknown():
    """With no seed of its name the field is nobody's, and ``REJECT`` names it as unknown."""
    toolset = SpecToolset({"record": _service(_OfferedWithoutARequest())})

    with pytest.raises(ModelRetry, match="tenant"):
        await _call_in_a_run_with_a_request(
            toolset, "record", {"title": "Dune", "tenant": "globex"}
        )


class _LegacyForNoContext(Validator):
    """Declares ``title``, and ``legacy`` beside it only when handed ``None``."""

    def parameters(self, *, call_context: dict[str, Any] | None = None) -> Parameters:
        title = Parameter("title", "string")
        if call_context is None:
            return Parameters.of(title, Parameter("legacy", "string"))
        return Parameters.of(title)

    def validate(self, arguments: Mapping[str, Any], context: ValidationContext) -> dict[str, Any]:
        return dict(arguments)


def test_a_field_declared_only_when_handed_none_is_not_offered():
    """The schema is built under the stamp, never ``None``. A pin of the changelog's claim."""
    toolset = SpecToolset({"record": _service(_LegacyForNoContext())})

    assert "legacy" not in _input_schema_of(toolset, "record")["properties"]
