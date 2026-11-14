from __future__ import annotations

from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django_service_specs import (
    Parameter,
    Parameters,
    SelectorKind,
    SelectorSpec,
    ServiceSpec,
)
from pydantic_ai import ModelRetry

from django_spec_agents.spec_tools import SpecToolset
from tests.spec_tools.kernel_specs import allow, widget_input, widget_presenter
from tests.spec_tools.test_spec_toolset import ctx_for
from tests.spec_tools.testapp.models import Widget

OWNER = Parameters.of(Parameter("owner_id", "integer", required=True))


def _owned(user, owner_id: int):
    """The owner's widgets."""
    return Widget.objects.filter(owner_id=owner_id).order_by("pk")


def _add(*, data, user, collection):
    """Add widgets to the set, naming the set's rows before the call."""
    names = [widget.name for widget in collection]
    return [
        Widget.objects.create(owner=user, name=f"{item['name']}>{names}", price=item["price"])
        for item in data
    ]


def _spec(**kwargs: Any) -> ServiceSpec:
    return ServiceSpec(
        service=_add,
        validator=widget_input(),
        many=True,
        permissions=allow(),
        presenter=widget_presenter(),
        **kwargs,
    )


def _collected() -> ServiceSpec:
    return _spec(
        collection_selector_spec=SelectorSpec(selector=_owned, kind=SelectorKind.LIST, reads=OWNER)
    )


async def test_a_many_tool_advertises_its_collection_lookup_beside_the_list():
    tools = await SpecToolset({"add": _collected()}).get_tools(None)
    schema = tools["add"].tool_def.parameters_json_schema

    assert list(schema["properties"]) == ["owner_id", "items"]
    assert schema["properties"]["owner_id"] == {"type": "integer"}
    assert schema["required"] == ["owner_id", "items"]
    assert schema["additionalProperties"] is False


@pytest.mark.django_db(transaction=True)
async def test_a_call_sending_the_lookup_hands_the_service_the_collection():
    user = await sync_to_async(User.objects.create)(username="u")
    await sync_to_async(Widget.objects.create)(name="old", price=1, owner=user)
    toolset = SpecToolset({"add": _collected()})
    tools = await toolset.get_tools(None)

    out = await toolset.call_tool(
        "add",
        {"owner_id": user.pk, "items": [{"name": "a", "price": 2}]},
        ctx_for(user),
        tools["add"],
    )

    assert [row["name"] for row in out["items"]] == ["a>['old']"]


async def test_a_call_leaving_out_the_lookup_is_asked_for_it():
    toolset = SpecToolset({"add": _collected()})
    tools = await toolset.get_tools(None)

    with pytest.raises(ModelRetry, match="owner_id"):
        await toolset.call_tool(
            "add", {"items": [{"name": "a", "price": 2}]}, ctx_for(None), tools["add"]
        )


async def test_a_many_tool_without_a_collection_takes_the_list_alone():
    def _create(*, data, user):
        """Create widgets."""
        return []

    spec = ServiceSpec(
        service=_create,
        validator=widget_input(),
        many=True,
        permissions=allow(),
        presenter=widget_presenter(),
    )
    tools = await SpecToolset({"add": spec}).get_tools(None)
    schema = tools["add"].tool_def.parameters_json_schema

    assert list(schema["properties"]) == ["items"]
    assert schema["required"] == ["items"]


def test_an_instance_selector_on_a_many_spec_stays_refused():
    with pytest.raises(ImproperlyConfigured, match="many"):
        _spec(
            instance_selector_spec=SelectorSpec(
                selector=_owned, kind=SelectorKind.RETRIEVE, reads=OWNER
            )
        )
