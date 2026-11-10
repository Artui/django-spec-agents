from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import RequestFactory
from django_service_specs import (
    TRANSPORT_KEY,
    Affordance,
    OutputField,
    Parameter,
    Parameters,
    PermissionCheck,
    PresentationContext,
    SelectorKind,
    SelectorSpec,
    ServiceSpec,
    ValidationContext,
    Validator,
    build_call_context,
)

import django_spec_agents.spec_tools as spec_tools
import django_spec_agents.spec_tools.spec_toolset as spec_toolset
from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import SpecToolset
from tests.spec_tools.kernel_specs import RowPresenter, allow
from tests.spec_tools.test_spec_toolset import ctx_for

# --- the constant ----------------------------------------------------------------


def test_the_package_exports_the_value_it_stamps():
    """The toolset stamps the value read off the package a consumer imports."""
    assert spec_tools.AGENT_TRANSPORT == "agent"


# --- built through the kernel's builder ------------------------------------------


async def test_every_context_is_built_through_the_kernels_builder_with_the_run_as_base(
    monkeypatch: pytest.MonkeyPatch,
):
    """The schema's stamp, the call's context and the listing's are each its result."""
    built: list[tuple[str, Mapping[str, Any] | None, str | None]] = []
    returned: list[dict[str, Any]] = []

    def spy(
        transport: str, *, base: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        built.append((transport, base, kwargs.get("action")))
        context = build_call_context(transport, base=base, **kwargs)
        returned.append(context)
        return context

    monkeypatch.setattr(spec_toolset, "build_call_context", spy)
    seen: list[dict[str, Any]] = []

    def record(call_context: dict[str, Any]) -> dict[str, bool]:
        seen.append(call_context)
        return {"ok": True}

    run = {"carried": object()}
    deps = AgentDeps(user=User(username="u"))
    deps.call_context = run
    toolset = SpecToolset(
        {
            "do_it": ServiceSpec(
                service=record,
                atomic=False,
                permissions=allow(),
                affordances=[Affordance(code="c", reason="r", when=lambda call_context: True)],
            )
        }
    )
    ctx = ctx_for(deps.user, deps=deps)

    tools = await toolset.get_tools(ctx)
    await toolset.call_tool("do_it", {}, ctx, tools["do_it"])

    assert [(transport, base, action) for transport, base, action in built] == [
        ("agent", None, None),
        ("agent", run, "do_it"),
        ("agent", run, "do_it"),
    ]
    assert seen == [returned[-1]]
    assert seen[0] is returned[-1]


async def test_the_stamp_is_last_even_over_a_run_that_carries_the_key():
    """The run's own ``transport`` entry is overwritten, not merged, and not edited in place."""
    seen: list[Any] = []

    def record(call_context: dict[str, Any]) -> dict[str, bool]:
        seen.append(call_context[TRANSPORT_KEY])
        return {"ok": True}

    deps = AgentDeps(user=User(username="u"))
    deps.call_context = {TRANSPORT_KEY: "http", "action": "somebody_else"}
    toolset = SpecToolset({"do_it": ServiceSpec(service=record, atomic=False, permissions=allow())})
    ctx = ctx_for(deps.user, deps=deps)

    tools = await toolset.get_tools(ctx)
    await toolset.call_tool("do_it", {}, ctx, tools["do_it"])

    assert seen == ["agent"]
    assert deps.call_context == {TRANSPORT_KEY: "http", "action": "somebody_else"}


# --- the run's own entries survive the builder ----------------------------------------


async def test_the_runs_request_view_and_format_reach_a_callable_as_the_run_put_them():
    """The builder layers ``None`` defaults, then the run's entries, which keep their values."""
    arrived = RequestFactory().get("/agent/")
    view = object()
    seen: dict[str, Any] = {}

    def record(call_context: dict[str, Any], request: Any, view: Any) -> dict[str, bool]:
        seen.update(call_context, request_seed=request, view_seed=view)
        return {"ok": True}

    deps = AgentDeps(user=User(username="u"))
    deps.call_context = {"request": arrived, "view": view, "format": "json", "extra": 7}
    toolset = SpecToolset({"do_it": ServiceSpec(service=record, atomic=False, permissions=allow())})
    ctx = ctx_for(deps.user, deps=deps)

    tools = await toolset.get_tools(ctx)
    await toolset.call_tool("do_it", {}, ctx, tools["do_it"])

    assert seen["request"] is arrived
    assert seen["request_seed"] is arrived
    assert seen["view"] is view
    assert seen["view_seed"] is view
    assert seen["format"] == "json"
    assert seen["extra"] == 7
    assert seen["action"] == "do_it"
    assert seen["transport"] == "agent"


async def test_a_calls_context_is_a_plain_dict_of_its_own_whatever_the_run_holds():
    """The run's mapping is read, never handed on: the call gets a fresh ``dict``."""
    seen: list[Any] = []

    def record(call_context: dict[str, Any]) -> dict[str, bool]:
        seen.append(call_context)
        return {"ok": True}

    run: Mapping[str, Any] = {"carried": 1}
    deps = AgentDeps(user=User(username="u"))
    deps.call_context = run  # ty: ignore[invalid-assignment]
    toolset = SpecToolset({"do_it": ServiceSpec(service=record, atomic=False, permissions=allow())})
    ctx = ctx_for(deps.user, deps=deps)

    tools = await toolset.get_tools(ctx)
    await toolset.call_tool("do_it", {}, ctx, tools["do_it"])

    assert type(seen[0]) is dict
    assert seen[0] is not run
    assert run == {"carried": 1}


async def test_a_run_that_is_a_read_only_mapping_is_the_base_and_the_stamp_still_lands():
    """A ``MappingProxyType`` context is read and the stamp written over a copy."""
    seen: list[dict[str, Any]] = []

    def record(call_context: dict[str, Any]) -> dict[str, bool]:
        seen.append(call_context)
        return {"ok": True}

    run = MappingProxyType({"carried": 1, TRANSPORT_KEY: "http"})
    deps = AgentDeps(user=User(username="u"))
    deps.call_context = run  # ty: ignore[invalid-assignment]
    toolset = SpecToolset({"do_it": ServiceSpec(service=record, atomic=False, permissions=allow())})
    ctx = ctx_for(deps.user, deps=deps)

    tools = await toolset.get_tools(ctx)
    await toolset.call_tool("do_it", {}, ctx, tools["do_it"])

    assert type(seen[0]) is dict
    assert seen[0]["carried"] == 1
    assert seen[0][TRANSPORT_KEY] == "agent"
    assert dict(run) == {"carried": 1, TRANSPORT_KEY: "http"}


# --- one value across the stages of a call ---------------------------------------


class _Reads(PermissionCheck):
    """Admits every call, and records the transport its context carries."""

    def __init__(self, seen: list[tuple[str, Any]]) -> None:
        self.seen = seen

    def has_permission(
        self, principal: Any, spec: Any, *, call_context: dict[str, Any] | None = None
    ) -> bool:
        self.seen.append(("check", (call_context or {}).get(TRANSPORT_KEY)))
        return True


class _ValidatorReads(Validator):
    """Declares ``title``, and records the transport it is asked under and validates with."""

    def __init__(self, seen: list[tuple[str, Any]]) -> None:
        self.seen = seen

    def parameters(self, *, call_context: dict[str, Any] | None = None) -> Parameters:
        if call_context is not None:
            self.seen.append(("validator.parameters", call_context.get(TRANSPORT_KEY)))
        return Parameters.of(Parameter("title", "string"))

    def validate(self, arguments: Mapping[str, Any], context: ValidationContext) -> dict[str, Any]:
        self.seen.append(("validator", context.call_context.get(TRANSPORT_KEY)))
        return dict(arguments)


class _PresenterReads(RowPresenter):
    """Renders a row, and records the transport the presentation carries."""

    def __init__(self, seen: list[tuple[str, Any]]) -> None:
        super().__init__(OutputField("name", "string"))
        self.seen = seen

    def presented(self, value: Any, context: PresentationContext | None) -> Any:
        assert context is not None
        self.seen.append(("presenter", context.call_context.get(TRANSPORT_KEY)))
        return self.present(value)


async def test_a_callable_reads_one_value_across_the_stages_of_a_call():
    """Each of the call's seats reads ``agent``, and each ran."""
    seen: list[tuple[str, Any]] = []

    def write(call_context: dict[str, Any], *, data: dict[str, Any]) -> dict[str, bool]:
        seen.append(("service", call_context[TRANSPORT_KEY]))
        return {"ok": True}

    def kwargs(call_context: dict[str, Any]) -> dict[str, Any]:
        seen.append(("kwargs", call_context[TRANSPORT_KEY]))
        return {}

    def read(call_context: dict[str, Any]) -> list[dict[str, str]]:
        seen.append(("selector", call_context[TRANSPORT_KEY]))
        return [{"name": "Atlas"}]

    toolset = SpecToolset(
        {
            "write": ServiceSpec(
                service=write,
                validator=_ValidatorReads(seen),
                atomic=False,
                permissions=[_Reads(seen)],
            ),
            "read": SelectorSpec(
                kind=SelectorKind.LIST,
                selector=read,
                kwargs=kwargs,
                presenter=_PresenterReads(seen),
                permissions=[_Reads(seen)],
            ),
        }
    )
    ctx = ctx_for(User(username="u"))
    tools = await toolset.get_tools(ctx)

    await toolset.call_tool("write", {"title": "t"}, ctx, tools["write"])
    await toolset.call_tool("read", {}, ctx, tools["read"])

    assert {stage for stage, _ in seen} >= {
        "check",
        "validator.parameters",
        "validator",
        "service",
        "kwargs",
        "selector",
        "presenter",
    }
    assert {value for _, value in seen} == {"agent"}


async def test_a_listing_and_the_call_it_precedes_read_one_value():
    """A tool listed on a transport is not refused on another at call time."""
    seen: list[tuple[str, Any]] = []

    def when(call_context: dict[str, Any]) -> bool:
        seen.append(("condition", call_context[TRANSPORT_KEY]))
        return True

    toolset = SpecToolset(
        {
            "approve": ServiceSpec(
                service=lambda: {"ok": True},
                atomic=False,
                permissions=[_Reads(seen)],
                affordances=[Affordance(code="closed", reason="Closed.", when=when)],
            )
        }
    )
    ctx = ctx_for(User(username="u"))

    tools = await toolset.get_tools(ctx)
    await toolset.call_tool("approve", {}, ctx, tools["approve"])

    assert {stage for stage, _ in seen} >= {"condition", "check"}
    assert {value for _, value in seen} == {"agent"}
