"""A ``SpecRegistry`` is accepted wherever a ``name -> spec`` mapping is."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import django_service_specs
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django_service_specs import FieldAudience, FieldMarking, SelectorKind, SpecRegistry
from pydantic_ai import ModelRetry
from rest_framework.permissions import AllowAny
from rest_framework_services import (
    JsonSchemaRegistry,
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


def ping(*, user: Any) -> dict[str, str]:
    """Answer with the acting user's name."""
    return {"pong": user.username}


def _native_spec() -> django_service_specs.ServiceSpec:
    """A django-service-specs spec, declared on the kernel with no drf-services in it."""
    return django_service_specs.ServiceSpec(
        service=ping, permissions=[django_service_specs.Unrestricted()]
    )


def _native_registry() -> SpecRegistry:
    registry = SpecRegistry()
    registry.register("native_ping", _native_spec())
    return registry


class TestANativeSpecIsServed:
    """A kernel-native spec is a tool like a drf-services one. The toolset reads
    every spec through ``kernel_spec``, which a django-service-specs spec answers
    with itself, so nothing in it asks which library declared the spec."""

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.parametrize(
        ("build", "source"),
        [
            (SpecToolset, _native_registry),
            (SpecToolset, lambda: _native_registry().specs()),
            (lambda source: SpecCapability(source).get_toolset(), _native_registry),
        ],
        ids=["registry", "mapping", "capability"],
    )
    async def test_whichever_way_it_arrives(self, build: Any, source: Any) -> None:
        toolset = build(source())
        user = await User.objects.acreate(username="native")
        tools = await toolset.get_tools(ctx_for(user))

        result = await toolset.call_tool("native_ping", {}, ctx_for(user), tools["native_ping"])

        assert result == {"pong": "native"}
        assert tools["native_ping"].tool_def.description == "Answer with the acting user's name."

    def test_a_native_selector_spec_is_a_read_only_tool(self) -> None:
        native = django_service_specs.SelectorSpec(
            kind=SelectorKind.LIST,
            selector=list_widgets,
            permissions=[django_service_specs.Unrestricted()],
        )

        toolset = SpecToolset({"native_list": native})

        assert toolset._tool_defs["native_list"].metadata == {"annotations": {"readOnlyHint": True}}

    @pytest.mark.parametrize(
        "native",
        [
            pytest.param(_native_spec, id="service"),
            pytest.param(
                lambda: django_service_specs.SelectorSpec(
                    kind=SelectorKind.LIST,
                    selector=list_widgets,
                    permissions=[django_service_specs.Unrestricted()],
                ),
                id="selector",
            ),
        ],
    )
    def test_a_native_spec_is_described_alike_under_a_consumer_registry(self, native: Any) -> None:
        """A toolset's ``json_schema_registry`` describes drf-services'
        serializers and filters, which a native spec has none of: its tool is
        described as under the default registry, rather than reflected through
        drf-services, which reads only its own classes."""
        spec = native()

        default = SpecToolset({"t": spec})._tool_defs["t"]
        consumer = SpecToolset({"t": spec}, json_schema_registry=JsonSchemaRegistry())._tool_defs[
            "t"
        ]

        assert consumer.parameters_json_schema == default.parameters_json_schema
        assert consumer.return_schema == default.return_schema

    @pytest.mark.parametrize(
        ("sent", "seen"),
        [({}, "all"), ({"region": None}, None), ({"region": "north"}, "north")],
        ids=["omitted-takes-the-default", "null-is-a-value", "sent"],
    )
    async def test_a_native_read_s_null_is_a_value_where_it_is_nullable(
        self, sent: dict[str, Any], seen: Any
    ) -> None:
        """A null is dropped as the model declining a read only where the read
        cannot be null. A native read declared ``nullable`` admits it, so the
        callable gets the ``None`` sent, never the default in its place."""

        def region_of(*, region: str | None = "unset") -> dict[str, Any]:
            """The region asked for."""
            return {"region": region}

        native = django_service_specs.SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=region_of,
            reads=django_service_specs.Parameters.of(
                django_service_specs.Parameter("region", "string", nullable=True, default="all")
            ),
            permissions=[django_service_specs.Unrestricted()],
        )
        toolset = SpecToolset({"region": native})
        tools = await toolset.get_tools(ctx_for(User(username="u")))

        result = await toolset.call_tool(
            "region", sent, ctx_for(User(username="u")), tools["region"]
        )

        assert result == {"region": seen}

    def test_a_polymorphic_spec_is_refused_naming_its_variants(self) -> None:
        """Refused by its own ``as_kernel_spec``, which names the remedy: one
        tool per variant. The toolset adds the tool's name."""
        moderate = PolymorphicServiceSpec(
            discriminator=lambda *, data: data["op"],
            specs={"create": _create_spec()},
        )

        with pytest.raises(TypeError, match=r"^SpecToolset tool 'moderate': ") as raised:
            SpecToolset({"moderate": moderate})

        assert "Register each variant from its `specs` mapping" in str(raised.value)

    def test_a_value_that_compiles_to_no_spec_is_refused_naming_the_tool(self) -> None:
        # A registry entry is not a spec, though django-service-specs defines
        # both: the kernel refuses it, and the toolset names the tool.
        entry = _native_registry().get("native_ping")

        with pytest.raises(
            TypeError,
            match=r"^SpecToolset tool 'native_ping': expected a ServiceSpec or SelectorSpec",
        ):
            SpecToolset({"native_ping": entry})


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


class TestTheSpecsOwnReads:
    """What a caller with no HTTP request has to be told, declared on the spec.

    Over HTTP the URLconf supplies the route captures and the query string the
    read-shaping params, so a spec mounted on a view is already complete. Off
    HTTP nobody supplies them, and every agent transport needs the *identical*
    answer -- which is why the declaration belongs to the spec (``reads=`` and
    ``field_audiences=``), compiled into the spec's reads and presenter, rather
    than to this toolset's constructor or an MCP server's registrar. A registry
    and a bare mapping hand the toolset the same spec, so they serve it alike.
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
        ("read", "name"),
        [(UrlKwarg("tenant_pk"), "tenant_pk"), (QueryParam(name="since"), "since")],
        ids=["url-kwarg", "query-param"],
    )
    @pytest.mark.parametrize("source", ["registry", "mapping"])
    def test_reads_reach_the_tool_with_nothing_in_the_constructor(
        self, read: UrlKwarg | QueryParam, name: str, source: str
    ) -> None:
        registry = self._contract_registry(reads=(read,))
        toolset = SpecToolset(registry if source == "registry" else registry.specs())

        props = toolset._tool_defs["list_widgets"].parameters_json_schema["properties"]
        assert name in props

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
    @pytest.mark.parametrize("source", ["registry", "mapping"])
    async def test_each_source_advertises_the_spec_s_reads_and_routes_them(
        self, source: str
    ) -> None:
        """The spec carries its ``reads``, which are parameters of the compiled
        spec, so the kernel's schema the tool advertises lists them whichever
        way the spec arrived, and drf-services routes a value sent under one of
        them onto the offline view and its query string as it does for any
        caller.
        """
        spec = replace(
            _agent_list_spec(),
            permission_classes=[RecordingPermission],
            reads=(UrlKwarg("tenant_pk"), QueryParam(name="since")),
        )
        registry = SpecRegistry()
        registry.register("list_widgets", spec)

        user = await User.objects.acreate(username="u")

        toolset = SpecToolset(registry if source == "registry" else registry.specs())
        tools = await toolset.get_tools(ctx_for(user))
        await toolset.call_tool(
            "list_widgets",
            {"tenant_pk": "5", "since": "2020"},
            ctx_for(user),
            tools["list_widgets"],
        )

        props = toolset._tool_defs["list_widgets"].parameters_json_schema["properties"]
        assert {"tenant_pk", "since"} <= set(props)
        assert RecordingPermission.seen["kwargs"] == {"tenant_pk": "5"}
        assert RecordingPermission.seen["query_params"] == {"since": ["2020"]}


# --- the native example the spec-registry page shows -------------------------
#
# The same declarations as ``docs/spec-tools/spec-registry.md``'s "A spec
# declared on the kernel", on this suite's ``Widget`` where the page has an
# ``Order``, so a change to what the page says a native spec does fails here.


class SignedIn(django_service_specs.PermissionCheck):
    def has_permission(self, principal: Any, spec: Any) -> bool:
        return bool(principal is not None and principal.is_authenticated)


def widgets_named(*, user: Any, name: str) -> list[dict[str, Any]]:
    """The caller's widgets under one name."""
    return list(Widget.objects.filter(owner=user, name=name).values("name", "price"))


def reprice_widget(*, user: Any, pk: int, price: int) -> dict[str, Any]:
    """Set the price of one of the caller's widgets."""
    Widget.objects.filter(owner=user, pk=pk).update(price=price)
    return {"pk": pk, "price": price}


_LIST_WIDGETS = django_service_specs.SelectorSpec(
    kind=SelectorKind.LIST,
    selector=widgets_named,
    reads=django_service_specs.Parameters.of(
        django_service_specs.Parameter("name", "string", default="gear"),
    ),
    permissions=[SignedIn()],
)
_REPRICE_WIDGET = django_service_specs.ServiceSpec(
    service=reprice_widget,
    reads=django_service_specs.Parameters.of(
        django_service_specs.Parameter("pk", "integer", required=True),
        django_service_specs.Parameter("price", "integer", required=True, minimum=0),
    ),
    permissions=[SignedIn()],
)


class TestTheNativeExample:
    def test_its_arguments_are_its_declared_reads(self) -> None:
        toolset = SpecToolset({"list_widgets": _LIST_WIDGETS, "reprice_widget": _REPRICE_WIDGET})

        listed = toolset._tool_defs["list_widgets"].parameters_json_schema
        repriced = toolset._tool_defs["reprice_widget"].parameters_json_schema
        assert listed["properties"]["name"]["default"] == "gear"
        assert set(listed["properties"]) == {"name", "page", "limit"}
        assert set(repriced["properties"]) == {"pk", "price"}
        assert sorted(repriced["required"]) == ["pk", "price"]

    @pytest.mark.django_db(transaction=True)
    async def test_it_serves_the_signed_in_caller(self) -> None:
        user = await User.objects.acreate(username="u")
        widget = await Widget.objects.acreate(name="gear", price=1, owner=user)
        toolset = SpecToolset({"list_widgets": _LIST_WIDGETS, "reprice_widget": _REPRICE_WIDGET})

        tools = await toolset.get_tools(ctx_for(user))
        repriced = await toolset.call_tool(
            "reprice_widget", {"pk": widget.pk, "price": 5}, ctx_for(user), tools["reprice_widget"]
        )
        listed = await toolset.call_tool("list_widgets", {}, ctx_for(user), tools["list_widgets"])

        assert repriced == {"pk": widget.pk, "price": 5}
        assert listed["items"] == [{"name": "gear", "price": 5}]

    async def test_a_spec_declaring_no_reads_takes_no_argument(self) -> None:
        """A native spec reflects nothing off its callable, so one declaring no
        reads advertises an empty schema and refuses whatever is sent."""
        bare = django_service_specs.SelectorSpec(
            kind=SelectorKind.LIST, selector=widgets_named, permissions=[SignedIn()]
        )
        toolset = SpecToolset({"list_widgets": bare})
        user = User(username="u")

        tools = await toolset.get_tools(ctx_for(user))
        assert set(tools["list_widgets"].tool_def.parameters_json_schema["properties"]) == {
            "page",
            "limit",
        }
        with pytest.raises(ModelRetry, match=r"^name: Unknown argument\.$"):
            await toolset.call_tool(
                "list_widgets", {"name": "gear"}, ctx_for(user), tools["list_widgets"]
            )
