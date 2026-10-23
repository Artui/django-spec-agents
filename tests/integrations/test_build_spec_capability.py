from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from rest_framework.permissions import AllowAny
from rest_framework_services import ServiceSpec, SpecRegistry

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.integrations.build_spec_capability import build_spec_capability
from tests.integrations.drf_specs_lookup import SPECS as LOOKUP_SPECS
from tests.integrations.helpers import (
    LOOKUP_CALLS,
    open_spec,
    run_context,
    run_omission_then_correction,
)


def _ok(user: Any) -> dict[str, Any]:
    """A no-op spec service."""
    return {"ok": True}


def test_an_unguarded_spec_is_refused_rather_than_exposed() -> None:
    """The strict default reaches a consumer straight through this builder.

    ``permission_classes=None`` means inherit over HTTP, but a capability
    dispatches off HTTP with nothing to inherit from, so a spec correctly guarded
    behind a viewset would become callable by whatever the model decided to call.
    Asserted here rather than only upstream because this function is how every
    consumer builds the capability.
    """
    with pytest.raises(ImproperlyConfigured, match="with no permissions"):
        build_spec_capability({"ping": ServiceSpec(service=_ok, atomic=False)})


@pytest.mark.parametrize("as_registry", [False, True], ids=["mapping", "registry"])
def test_exclude_names_drops_the_names_the_registry_owns(as_registry: bool) -> None:
    source: Any = {"ping": open_spec(), "dup": open_spec()}
    if as_registry:
        source = SpecRegistry()
        source.register("ping", open_spec())
        source.register("dup", open_spec())

    capability = build_spec_capability(source, exclude_names=frozenset({"dup"}))
    # The registry-owned name is dropped (registry wins the collision); the
    # capability's underlying toolset exposes only the surviving name.
    assert set(capability.get_toolset()._specs) == {"ping"}


async def test_carries_the_spec_conventions_to_the_model() -> None:
    # The conventions (the error contract, the list-tool pagination args) reach
    # the system prompt from the *toolset*, not the capability.
    # ``SpecToolset.get_instructions()`` carries them so they land whether a
    # toolset is attached directly or wrapped here, and the capability returns
    # None so pydantic-ai doesn't collect the same block twice.
    capability = build_spec_capability({"ping": open_spec()})
    assert capability.get_instructions() is None

    instructions = await capability.get_toolset().get_instructions(None)
    assert instructions is not None
    assert "error" in instructions.lower()


async def test_binds_the_acting_user_from_the_run_deps() -> None:
    """The user arrives on ``ctx.deps``, so nothing closes over a request and the
    capability is request-independent."""
    seen: dict[str, Any] = {}

    def ping(user: Any) -> dict[str, Any]:
        """Ping."""
        seen["user"] = user
        return {"ok": True}

    user = SimpleNamespace(name="alice")
    capability = build_spec_capability(
        {"ping": ServiceSpec(service=ping, atomic=False, permission_classes=[AllowAny])}
    )

    toolset = capability.get_toolset()
    ctx = run_context(AgentDeps(user=user))
    assert await toolset.call_tool("ping", {}, ctx, None) == {"ok": True}
    assert seen["user"] is user


async def test_one_capability_serves_two_users() -> None:
    """The same capability, two runs, two acting users: a built agent can be reused."""
    seen: list[Any] = []

    def whoami(user: Any) -> dict[str, Any]:
        """Whoami."""
        seen.append(user)
        return {"ok": True}

    capability = build_spec_capability(
        {"whoami": ServiceSpec(service=whoami, atomic=False, permission_classes=[AllowAny])}
    )
    toolset = capability.get_toolset()

    alice, bob = SimpleNamespace(name="alice"), SimpleNamespace(name="bob")
    await toolset.call_tool("whoami", {}, run_context(AgentDeps(user=alice)), None)
    await toolset.call_tool("whoami", {}, run_context(AgentDeps(user=bob)), None)

    assert seen == [alice, bob]


def test_accepts_a_spec_registry() -> None:
    registry = SpecRegistry()
    registry.register("ping", open_spec())

    capability = build_spec_capability(registry)
    assert set(capability.get_toolset()._specs) == {"ping"}


def _capture_source(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record what this builder actually hands to ``SpecCapability``."""
    import django_spec_agents.spec_tools

    captured: list[Any] = []

    def _stub(source: Any, **kwargs: Any) -> Any:
        captured.append(source)
        return SimpleNamespace()

    monkeypatch.setattr(django_spec_agents.spec_tools, "SpecCapability", _stub)
    return captured


class TestARegistryIsNotFlattened:
    """The entry carries more than the spec, and only the entry carries it.

    A registry entry holds the per-entry declarations an agent transport reads
    -- its tags, and the ``OfflineContract`` saying what a caller with no HTTP
    request has to be told. ``specs()`` returns ``name -> spec`` and drops all
    of it, which is why this builder must pass the source through rather than
    normalise it first. The failure is silent: the toolset is well-formed and
    merely missing declarations nobody asked it for.
    """

    def test_the_registry_reaches_the_capability_as_a_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _capture_source(monkeypatch)
        registry = SpecRegistry()
        registry.register(
            "ping",
            open_spec(),
            tags=("read",),
        )

        build_spec_capability(registry)

        # ``specs()`` is what tells a registry from a mapping -- the same test
        # ``resolve_spec_mapping`` uses -- and the entry is what survives.
        (source,) = captured
        assert callable(getattr(source, "specs", None))
        assert source.get("ping").tags == frozenset({"read"})

    def test_narrowing_by_exclude_names_keeps_it_a_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _capture_source(monkeypatch)
        registry = SpecRegistry()
        registry.register(
            "ping",
            open_spec(),
            tags=("read",),
        )
        registry.register("dup", open_spec())

        build_spec_capability(registry, exclude_names=frozenset({"dup"}))

        (source,) = captured
        assert set(source.specs()) == {"ping"}
        assert source.get("ping").tags == frozenset({"read"})

    def test_a_plain_mapping_is_still_a_plain_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _capture_source(monkeypatch)
        spec = open_spec()

        build_spec_capability({"ping": spec, "dup": spec}, exclude_names=frozenset({"dup"}))

        assert captured == [{"ping": spec}]


async def test_a_specs_progress_reports_reach_the_runs_sink() -> None:
    """The other half of what rides ``ctx.deps``.

    ``SpecToolset`` reads its reporter off ``ctx.deps.progress`` exactly as it
    reads the user off ``ctx.deps.user``. With no such field the reports would
    resolve to drf-services' no-op and a long-running spec would report into
    nothing, with no warning anywhere.
    """
    reports: list[tuple[int, int | None, str | None]] = []

    def sink(
        progress: int, *, total: int | None = None, message: str | None = None, **_: Any
    ) -> None:
        reports.append((progress, total, message))

    def import_rows(user: Any, progress: Any) -> dict[str, Any]:
        """Import rows."""
        progress(45, total=100, message="importing rows")
        return {"ok": True}

    capability = build_spec_capability(
        {
            "import_rows": ServiceSpec(
                service=import_rows, atomic=False, permission_classes=[AllowAny]
            )
        }
    )
    ctx = run_context(AgentDeps(user=SimpleNamespace(name="alice"), progress=sink))

    await capability.get_toolset().call_tool("import_rows", {}, ctx, None)

    assert reports == [(45, 100, "importing rows")]


@pytest.mark.parametrize("name", ["get_row", "rename_row"])
async def test_a_selector_parameter_without_a_default_is_required(name: str) -> None:
    """What the model reads: ``pk`` is advertised, and required.

    ``SpecToolset`` lists a selector parameter with no default, which it does
    not fill, in the tool's ``required``, and a single-item service advertises
    the lookup its target is resolved through. Without either, ``get_row``
    called ``pk`` optional and ``rename_row`` never mentioned it, so a model
    had no reason to send the one argument the call cannot run without.
    """
    ctx = run_context(AgentDeps(user=AnonymousUser()))
    tools = await build_spec_capability(LOOKUP_SPECS).get_toolset().get_tools(ctx)
    schema = tools[name].tool_def.parameters_json_schema
    assert "pk" in schema["properties"]
    assert "pk" in schema.get("required", [])


@pytest.mark.parametrize(("name", "omitted", "corrected", "row"), LOOKUP_CALLS)
async def test_a_call_missing_a_selector_argument_is_one_retry_then_the_row(
    name: str, omitted: dict[str, Any], corrected: dict[str, Any], row: dict[str, Any]
) -> None:
    """A call leaving ``pk`` out is a retry naming it, then the row.

    ``SpecToolset`` raises ``ModelRetry`` for the omission itself. Without it the
    selector raised ``TypeError`` out of the toolset and ended the run over an
    argument the model could have supplied. Driven through a real run, so the
    retry is what the model reads.
    """
    retries, returns = await run_omission_then_correction(
        name, omitted, corrected, capabilities=[build_spec_capability(LOOKUP_SPECS)]
    )

    assert retries == ["Missing required argument(s): `pk`."]
    assert returns == [row]
