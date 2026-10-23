"""What one tool call is dispatched with beyond its arguments: its seeds and its context.

The run's context is ``AgentDeps.call_context``, copied per call with the
tool's name under ``action``; the call's own seeds are what ``call_seeds``
returns, over the mount's ``pool_seeds``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import RequestFactory
from django_service_specs import DEFAULT_POOL_SEEDS, Affordance
from pydantic_ai import RunContext
from rest_framework.permissions import AllowAny
from rest_framework_services import ServiceSpec

from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import SpecToolset
from tests.spec_tools.test_spec_toolset import ctx_for


def _service(service: Callable[..., Any], **kwargs: Any) -> ServiceSpec:
    return ServiceSpec(
        service=service,
        atomic=False,
        permission_classes=[AllowAny],
        **kwargs,
    )


async def _call_each(toolset: SpecToolset, ctx: Any, *names: str) -> list[Any]:
    tools = await toolset.get_tools(ctx)
    return [await toolset.call_tool(name, {}, ctx, tools[name]) for name in names]


# --- the run's context ---------------------------------------------------------


async def test_each_call_of_a_run_is_handed_its_own_copy_of_the_run_s_context():
    """The tool's name is the call's ``action``, and a stage's write stays in its call.

    Two calls of one run may run at once, so one dict shared by both could not
    say which tool each is. Each is handed a copy of the run's: the run's
    entries are the same objects in both, the name is each call's own, and
    what the first call's service wrote is absent from the second's and from
    the run's.
    """
    seen: list[dict[str, Any]] = []

    def record(call_context: dict[str, Any]) -> dict[str, bool]:
        """Record what the call carries."""
        seen.append(call_context)
        if call_context.get("action") == "first":
            call_context["written"] = True
        return {"ok": True}

    shared = object()
    deps = AgentDeps(user=User(username="u"))
    deps.call_context = {"shared": shared}
    toolset = SpecToolset({"first": _service(record), "second": _service(record)})

    await _call_each(toolset, ctx_for(deps.user, deps=deps), "first", "second")

    assert [context.get("action") for context in seen] == ["first", "second"]
    assert all(context.get("shared") is shared for context in seen)
    assert "written" in seen[0]
    assert "written" not in seen[1]
    assert deps.call_context == {"shared": shared}


async def test_the_three_standard_entries_are_none_where_the_run_put_none():
    """``request``, ``view`` and ``format`` are present on every call, as on HTTP."""
    seen: list[dict[str, Any]] = []

    def record(call_context: dict[str, Any]) -> dict[str, bool]:
        """Record what the call carries."""
        seen.append(dict(call_context))
        return {"ok": True}

    user = User(username="u")
    toolset = SpecToolset({"do_it": _service(record)})

    await _call_each(toolset, ctx_for(user), "do_it")

    assert seen == [{"request": None, "view": None, "format": None, "action": "do_it"}]


async def test_a_deps_type_without_the_field_gets_a_context_per_call():
    """A project's own deps type need not declare ``call_context``."""
    seen: list[dict[str, Any]] = []

    def record(call_context: dict[str, Any]) -> dict[str, bool]:
        """Record what the call carries."""
        seen.append(call_context)
        return {"ok": True}

    user = User(username="u")
    toolset = SpecToolset({"first": _service(record), "second": _service(record)})

    await _call_each(toolset, ctx_for(user, deps=SimpleNamespace(user=user)), "first", "second")

    assert [context.get("action") for context in seen] == ["first", "second"]
    assert seen[0] is not seen[1]


async def test_the_request_a_run_arrived_on_is_the_seed_a_callable_declares():
    """The run's ``request`` entry is the ``request`` a spec callable is handed.

    The kernel's pool has no ``request``; a spec callable written for HTTP
    declares one, and off HTTP it is the request the run arrived on, which
    whoever starts the run puts in the run's context.
    """
    arrived = RequestFactory().get("/agent/")

    def echo(request: Any) -> dict[str, bool]:
        """Report whether the request is the run's."""
        return {"same": request is arrived}

    user = User(username="u")
    deps = AgentDeps(user=user)
    deps.call_context = {"request": arrived}
    toolset = SpecToolset({"echo": _service(echo)})

    assert await _call_each(toolset, ctx_for(user, deps=deps), "echo") == [{"same": True}]


# --- the call's own seeds ------------------------------------------------------


class _Tenanted(SpecToolset):
    """Seeds each call with a tenant built from the run and the tool's name."""

    def call_seeds(
        self, *, ctx: RunContext[Any], tool_name: str
    ) -> Mapping[str, Callable[..., Any]]:
        username = ctx.deps.user.username
        return {"tenant": lambda: f"{username}:{tool_name}"}


async def test_a_seed_the_hook_returns_reaches_the_call_and_is_not_asked_of_the_model():
    """The hook's seed fills a parameter with no default, so the model is never asked for it.

    The required-arguments check reads the call's seeds, the hook's among
    them; a hook answered after it would leave ``tenant`` refused as missing.
    """

    def where(tenant: str) -> dict[str, str]:
        """Say which tenant the call ran under."""
        return {"tenant": tenant}

    user = User(username="u")
    toolset = _Tenanted({"first": _service(where), "second": _service(where)})

    results = await _call_each(toolset, ctx_for(user), "first", "second")

    assert results == [{"tenant": "u:first"}, {"tenant": "u:second"}]


async def test_a_hook_seed_the_mount_already_registers_is_refused():
    """``PoolSeeds.extend`` refuses a name registered twice, rather than picking a winner."""

    def where(tenant: str) -> dict[str, str]:
        """Say which tenant the call ran under."""
        return {"tenant": tenant}

    user = User(username="u")
    toolset = _Tenanted(
        {"where": _service(where)},
        pool_seeds=DEFAULT_POOL_SEEDS.extend(tenant=lambda: "mount"),
    )

    with pytest.raises(ValueError, match="'tenant' is already registered"):
        await _call_each(toolset, ctx_for(user), "where")


def _approve(**_: Any) -> dict[str, bool]:
    """Approve the pending invoices."""
    return {"approved": True}


def _conditioned(when: Callable[..., Any]) -> ServiceSpec:
    return _service(
        _approve,
        affordances=[Affordance(code="closed", reason="Closed here.", when=when)],
    )


async def test_a_listing_asks_each_condition_with_its_own_tool_s_seeds():
    """The listing calls the hook per conditioned tool, as each call would."""

    class Gated(SpecToolset):
        def call_seeds(
            self, *, ctx: RunContext[Any], tool_name: str
        ) -> Mapping[str, Callable[..., Any]]:
            return {"open_here": lambda: tool_name == "approve"}

    toolset = Gated(
        {
            "approve": _conditioned(lambda open_here: open_here),
            "reject": _conditioned(lambda open_here: open_here),
        }
    )

    tools = await toolset.get_tools(ctx_for(User(username="u")))

    assert sorted(tools) == ["approve"]


async def test_a_listing_hands_each_condition_its_own_tool_s_name_as_the_action():
    """A condition naming ``call_context`` reads the operation it is asked about."""
    toolset = SpecToolset(
        {
            "approve": _conditioned(lambda call_context: call_context["action"] == "approve"),
            "reject": _conditioned(lambda call_context: call_context["action"] == "approve"),
        }
    )

    tools = await toolset.get_tools(ctx_for(User(username="u")))

    assert sorted(tools) == ["approve"]
