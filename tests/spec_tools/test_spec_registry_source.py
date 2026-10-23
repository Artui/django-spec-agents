"""A ``SpecRegistry`` is accepted wherever a ``name -> spec`` mapping is."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import django_service_specs
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django_service_specs import FieldAudience, FieldMarking, SelectorKind, SpecRegistry
from rest_framework.permissions import AllowAny
from rest_framework_services import (
    PolymorphicServiceSpec,
    QueryParam,
    SelectorSpec,
    ServiceSpec,
    UrlKwarg,
)

from django_spec_agents.spec_tools import SpecCapability, SpecToolset
from tests.spec_tools.test_spec_toolset import RecordingPermission, ctx_for
from tests.spec_tools.testapp.models import Widget
from tests.spec_tools.testapp.serializers import AgentWidgetSerializer, WidgetSerializer


def list_widgets(user):
    """List widgets owned by the acting user."""
    return Widget.objects.filter(owner=user)


def create_widget(user, data):
    """Create a widget."""
    return Widget.objects.create(owner=user, **data)


def _list_spec() -> SelectorSpec:
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        output_serializer=WidgetSerializer,
        permission_classes=[AllowAny],
    )


def _agent_list_spec() -> SelectorSpec:
    """The same read, through the serializer that carries agent markings."""
    return SelectorSpec(
        kind=SelectorKind.LIST,
        selector=list_widgets,
        output_serializer=AgentWidgetSerializer,
        permission_classes=[AllowAny],
    )


def _create_spec() -> ServiceSpec:
    return ServiceSpec(service=create_widget, permission_classes=[AllowAny], atomic=False)


def _registry() -> SpecRegistry:
    registry = SpecRegistry()
    registry.register("list_widgets", _list_spec(), tags=("read", "public"))
    registry.register("create_widget", _create_spec(), tags=("write", "admin"))
    return registry


class TestToolsetFromRegistry:
    def test_exposes_every_entry_as_a_tool(self) -> None:
        toolset = SpecToolset(_registry())
        assert sorted(toolset._tool_defs) == ["create_widget", "list_widgets"]

    def test_matches_the_equivalent_dict(self) -> None:
        """``registry.specs()`` is the dict the toolset already accepted, so the
        two construction paths must be indistinguishable."""
        registry = _registry()
        from_registry = SpecToolset(registry)
        from_dict = SpecToolset(registry.specs())

        assert from_registry._specs == from_dict._specs
        assert from_registry._tool_defs.keys() == from_dict._tool_defs.keys()

    def test_read_only_hint_still_derives_from_spec_kind(self) -> None:
        toolset = SpecToolset(_registry())
        assert toolset._tool_defs["list_widgets"].metadata == {
            "annotations": {"readOnlyHint": True}
        }
        assert toolset._tool_defs["create_widget"].metadata == {
            "annotations": {"readOnlyHint": False}
        }

    def test_two_toolsets_from_one_registry_are_independent(self) -> None:
        registry = _registry()
        reads = SpecToolset(registry.by_tag("read"), id="reads")
        writes = SpecToolset(registry.by_tag("write"), id="writes")

        assert list(reads._tool_defs) == ["list_widgets"]
        assert list(writes._tool_defs) == ["create_widget"]
        assert reads.id != writes.id

    def test_an_empty_registry_yields_no_tools(self) -> None:
        assert SpecToolset(SpecRegistry())._tool_defs == {}

    def test_registration_order_is_preserved(self) -> None:
        registry = SpecRegistry()
        registry.register("b_spec", _list_spec())
        registry.register("a_spec", _create_spec())

        assert list(SpecToolset(registry)._tool_defs) == ["b_spec", "a_spec"]

    def test_a_registry_name_invalid_as_a_tool_name_still_fails_fast(self) -> None:
        """Registry names are free-form; provider tool names are not."""
        registry = SpecRegistry()
        registry.register("not a valid tool name", _list_spec())

        with pytest.raises(ValueError, match="not a valid tool name"):
            SpecToolset(registry)


def _native_registry() -> SpecRegistry:
    # A django-service-specs spec, which the registry accepts because it
    # compiles to a kernel spec, and which drf-services cannot dispatch.
    registry = SpecRegistry()
    registry.register(
        "native_create",
        django_service_specs.ServiceSpec(
            service=create_widget, permissions=[django_service_specs.Unrestricted()]
        ),
    )
    return registry


_NATIVE_REFUSAL = (
    r"^SpecToolset tool 'native_create': django_service_specs\.ServiceSpec is not a "
    r"rest_framework_services\.ServiceSpec or SelectorSpec\. "
)


class TestANativeSpecIsRefused:
    """A kernel-native spec is refused when the toolset is built. Left to the
    toolset's internals it would die on the first drf-services attribute it
    lacks, an ``AttributeError`` naming neither the tool nor what to do. The
    refusal is a ``TypeError`` because the value is the wrong kind, and it names
    the tool and both ways out."""

    @pytest.mark.parametrize(
        ("build", "source"),
        [
            (SpecToolset, _native_registry),
            (SpecToolset, lambda: _native_registry().specs()),
            (SpecCapability, _native_registry),
        ],
        ids=["registry", "mapping", "capability"],
    )
    def test_whichever_way_it_arrives(self, build: Any, source: Any) -> None:
        with pytest.raises(TypeError, match=_NATIVE_REFUSAL) as raised:
            build(source())

        assert "django_service_specs.dispatch" in str(raised.value)

    def test_a_native_selector_spec_is_pointed_at_dispatch_too(self) -> None:
        native = django_service_specs.SelectorSpec(
            kind=SelectorKind.LIST,
            selector=list_widgets,
            permissions=[django_service_specs.Unrestricted()],
        )

        with pytest.raises(TypeError, match=r"SelectorSpec is not a ") as raised:
            SpecToolset({"native_list": native})

        assert "django_service_specs.dispatch" in str(raised.value)

    def test_a_polymorphic_spec_is_pointed_at_its_variants(self) -> None:
        # A drf-services class, so the kernel's dispatch is the wrong way out:
        # the polymorphic actions page expands ``.specs`` into one tool per
        # variant, and the message says that instead.
        moderate = PolymorphicServiceSpec(
            discriminator=lambda *, data: data["op"],
            specs={"create": _create_spec()},
        )

        with pytest.raises(
            TypeError,
            match=(
                r"^SpecToolset tool 'moderate': rest_framework_services\."
                r"PolymorphicServiceSpec is not a rest_framework_services\.ServiceSpec "
                r"or SelectorSpec\. "
            ),
        ) as raised:
            SpecToolset({"moderate": moderate})

        assert ".specs" in str(raised.value)
        assert "django_service_specs.dispatch" not in str(raised.value)

    def test_a_registry_entry_is_not_pointed_at_dispatch(self) -> None:
        # ``RegisteredSpec`` lives in django-service-specs too, but it is an
        # entry, not a spec dispatch could run: only a kernel spec is pointed
        # there, whatever module the value comes from.
        entry = _native_registry().get("native_create")

        with pytest.raises(TypeError, match=r"RegisteredSpec is not a ") as raised:
            SpecToolset({"native_create": entry})

        assert "django_service_specs.dispatch" not in str(raised.value)


class TestCapabilityFromRegistry:
    def test_wraps_a_registry(self) -> None:
        capability = SpecCapability(_registry())
        assert sorted(capability.get_toolset()._tool_defs) == ["create_widget", "list_widgets"]

    def test_matches_the_equivalent_dict(self) -> None:
        registry = _registry()
        assert (
            SpecCapability(registry).get_toolset()._specs
            == SpecCapability(registry.specs()).get_toolset()._specs
        )

    def test_filtered_views_give_independent_capabilities(self) -> None:
        registry = _registry()
        reads = SpecCapability(registry.by_tag("read"), id="reads")
        admin = SpecCapability(registry.by_tag("admin"), id="admin", defer_loading=True)

        assert list(reads.get_toolset()._tool_defs) == ["list_widgets"]
        assert list(admin.get_toolset()._tool_defs) == ["create_widget"]
        assert reads.defer_loading is False
        assert admin.defer_loading is True
        assert reads.id != admin.id

    def test_capability_id_mirrors_its_toolset(self) -> None:
        capability = SpecCapability(_registry(), id="reads")
        assert capability.id == capability.get_toolset().id == "reads"


class TestTheEntrysOfflineContract:
    """What a caller with no HTTP request has to be told, declared on the spec.

    Over HTTP the URLconf supplies the route captures and the query string the
    read-shaping params, so a spec mounted on a view is already complete. Off
    HTTP nobody supplies them, and every agent transport needs the *identical*
    answer -- which is why the declaration belongs to the spec (``reads=`` and
    ``field_audiences=``), whose registry entry answers it as
    ``entry.agent_contract``, rather than to this toolset's constructor or an
    MCP server's registrar.
    """

    def _contract_registry(
        self,
        *,
        reads: tuple[UrlKwarg | QueryParam, ...] = (),
        field_audiences: dict[str, FieldMarking] | None = None,
    ) -> SpecRegistry:
        registry = SpecRegistry()
        registry.register(
            "list_widgets",
            replace(_agent_list_spec(), reads=reads, field_audiences=field_audiences),
        )
        return registry

    @pytest.mark.parametrize(
        ("read", "name", "channel"),
        [
            (UrlKwarg("tenant_pk"), "tenant_pk", "_tool_url_kwargs"),
            (QueryParam(name="since"), "since", "_tool_query_params"),
        ],
        ids=["url-kwarg", "query-param"],
    )
    def test_reads_reach_the_tool_with_nothing_in_the_constructor(
        self, read: UrlKwarg | QueryParam, name: str, channel: str
    ) -> None:
        toolset = SpecToolset(self._contract_registry(reads=(read,)))

        props = toolset._tool_defs["list_widgets"].parameters_json_schema["properties"]
        assert name in props
        assert getattr(toolset, channel)["list_widgets"] == (read,)

    def test_field_audiences_reach_the_projection(self) -> None:
        # The audiences are part of the spec's own output declaration, so one
        # spec projects the same field set whichever agent transport serves it,
        # and the registry and a bare mapping project alike.
        registry = self._contract_registry(field_audiences={"price": FieldMarking()})

        from_registry = SpecToolset(registry)._projections["list_widgets"]
        from_mapping = SpecToolset(registry.specs())._projections["list_widgets"]

        assert from_registry.audience("price") is FieldAudience.CONTENT
        assert from_mapping.audience("price") is FieldAudience.CONTENT

    def test_the_serializer_still_decides_when_the_contract_is_silent(self) -> None:
        toolset = SpecToolset(self._contract_registry())

        assert toolset._projections["list_widgets"].audience("price") is FieldAudience.HIDDEN

    def test_a_field_audiences_clash_is_refused_when_the_toolset_is_built(self) -> None:
        # ``name`` is already the serializer's label; claiming it for ``id`` as
        # well leaves two, and a record has one name. The refusal is
        # drf-services', from the spec's own output declaration, and the toolset
        # prefixes it with the tool: drf-services names the output, which a
        # registry of many specs shares across tools.
        registry = self._contract_registry(field_audiences={"id": FieldMarking.label()})

        with pytest.raises(
            ImproperlyConfigured,
            match=r"^SpecToolset tool 'list_widgets': Output: both 'id' and 'name' are marked",
        ) as raised:
            SpecToolset(registry)

        # Re-raised, not replaced: drf-services' own refusal is the cause.
        assert isinstance(raised.value.__cause__, ImproperlyConfigured)
        assert str(raised.value.__cause__).startswith("Output: both 'id' and 'name'")

    def test_a_field_audiences_key_naming_no_output_field_is_refused(self) -> None:
        # A marking for a field the output does not carry would silently project
        # nothing; drf-services refuses it, and the toolset names the tool.
        registry = self._contract_registry(field_audiences={"nope": FieldMarking.label()})

        with pytest.raises(ImproperlyConfigured) as raised:
            SpecToolset(registry)

        assert str(raised.value).startswith("SpecToolset tool 'list_widgets': ")
        assert "'nope'" in str(raised.value)

    @pytest.mark.django_db(transaction=True)
    async def test_a_bare_mapping_advertises_the_spec_s_reads_and_routes_them(self) -> None:
        """``registry.specs()`` flattens the entry to its spec, and the toolset
        reads channels off the entry alone, so a bare mapping's tool declares
        none of its own. The spec still carries its ``reads``, which are
        parameters of the compiled spec, so the kernel's schema the tool
        advertises lists them as a registry-built tool's does, and drf-services
        routes a value sent under one of them onto the offline view and its query
        string as it does for any caller.
        """
        spec = replace(
            _agent_list_spec(),
            permission_classes=[RecordingPermission],
            reads=(UrlKwarg("tenant_pk"), QueryParam(name="since")),
        )
        registry = SpecRegistry()
        registry.register("list_widgets", spec)

        user = await User.objects.acreate(username="u")

        toolset = SpecToolset(registry.specs())
        tools = await toolset.get_tools(ctx_for(user))
        await toolset.call_tool(
            "list_widgets",
            {"tenant_pk": "5", "since": "2020"},
            ctx_for(user),
            tools["list_widgets"],
        )

        assert toolset._tool_url_kwargs["list_widgets"] == ()
        assert toolset._tool_query_params["list_widgets"] == ()
        props = toolset._tool_defs["list_widgets"].parameters_json_schema["properties"]
        assert {"tenant_pk", "since"} <= set(props)
        assert RecordingPermission.seen["kwargs"] == {"tenant_pk": "5"}
        assert RecordingPermission.seen["query_params"] == {"since": ["2020"]}
