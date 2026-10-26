"""A service's Validator fields, beside its ``input_data=`` provider, in the advertised schema.

``input_data`` is merged over the caller's arguments before the Validator runs,
so a field it fills or may fill is not demanded of the caller by dispatch, and a
schema that required it anyway told the model to send what the server supplies.
Written against ``SpecToolset``'s public surface only: each test builds a toolset
and reads the schema it advertises or the answer to a call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

import django_service_specs
import pytest
from django.contrib.auth.models import User
from django_service_specs import UNSET, Parameter, Parameters, ServiceSpec, UnsetType
from pydantic_ai.exceptions import ModelRetry

from django_spec_agents.spec_tools import SpecToolset
from tests.spec_tools.kernel_specs import allow
from tests.spec_tools.test_spec_toolset import _call, _input_schema_of, by_pk, get_any_widget


@dataclass
class Row:
    name: str
    owner: str


class _Recording(django_service_specs.DataclassValidator):
    """Records each time it is asked to validate, so a test sees whether it ran."""

    ran: list[dict[str, Any]] = []

    def validated(self, arguments: Any, context: Any) -> Any:
        _Recording.ran.append(dict(arguments))
        return super().validated(arguments, context)


@pytest.fixture(autouse=True)
def _forget_the_validations():
    _Recording.ran.clear()


def make_row(data: Row) -> dict[str, str]:
    """Report the validated row, so a test sees which value reached the Validator."""
    return {"name": data.name, "owner": data.owner}


def make_rows(data: list[Row]) -> dict[str, list[str]]:
    return {"owners": [row.owner for row in data]}


def approve_row(instance: Any, data: Row) -> dict[str, str]:
    return {"owner": data.owner}


def _fills_owner(user):
    """A provider whose return says nothing about its keys, and that fills ``owner``."""
    return {"owner": "SERVER"}


def _fills_nothing(user):
    """The same untyped seat, filling nothing: the call has to carry ``owner``."""
    return {}


class _OwnerOrDecline(TypedDict):
    """``owner`` or ``UNSET``: a key the provider may decline."""

    owner: str | UnsetType


def _declines_owner(user) -> _OwnerOrDecline:
    return _OwnerOrDecline(owner=UNSET)


def _fills_owner_when_it_can(user) -> _OwnerOrDecline:
    return _OwnerOrDecline(owner="SERVER")


class _Owner(TypedDict):
    owner: str


class _PkOrDecline(TypedDict):
    """A key the Validator does not declare, which a read in another seat does."""

    pk: int | UnsetType


def _declines_pk(user) -> _PkOrDecline:
    return _PkOrDecline(pk=UNSET)


def _always_fills_owner(user) -> _Owner:
    return _Owner(owner="SERVER")


def _service(input_data: Any, **kwargs: Any) -> ServiceSpec:
    return ServiceSpec(
        service=make_row,
        validator=_Recording(Row),
        input_data=input_data,
        atomic=False,
        permissions=allow(),
        **kwargs,
    )


def test_a_required_field_beside_an_untyped_input_data_is_advertised_and_not_required():
    """The provider may be what fills any Validator field, so the schema asks the
    model for none of them, as it asks for no read an untyped ``kwargs=`` may
    fill. Holds the untyped seat in ``_caller_schema``: leaving the fields as
    declared would keep ``name`` and ``owner`` required, which is what it said
    before.
    """
    toolset = SpecToolset({"t": _service(_fills_owner)})

    schema = _input_schema_of(toolset, "t")

    assert sorted(schema["properties"]) == ["name", "owner"]
    assert "required" not in schema


def test_a_field_a_typed_input_data_may_decline_is_advertised_and_not_required():
    """Only the key the provider may decline is relaxed: ``name`` is nobody's but
    the caller's. Holds ``names & keys.declinable`` for the Validator seat."""
    toolset = SpecToolset({"t": _service(_fills_owner_when_it_can)})

    schema = _input_schema_of(toolset, "t")

    assert sorted(schema["properties"]) == ["name", "owner"]
    assert schema["required"] == ["name"]


def test_a_field_a_typed_input_data_always_fills_is_not_advertised():
    """A guard: the key is the server's whatever the caller sends, so it leaves
    the schema altogether and the seat has nothing to relax."""
    toolset = SpecToolset({"t": _service(_always_fills_owner)})

    schema = _input_schema_of(toolset, "t")

    assert sorted(schema["properties"]) == ["name"]
    assert schema["required"] == ["name"]


@pytest.mark.django_db(transaction=True)
async def test_a_call_leaving_out_a_field_the_provider_fills_is_served():
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset({"t": _service(_fills_owner)})

    result = await _call(toolset, "t", user, {"name": "n"})

    assert result == {"name": "n", "owner": "SERVER"}
    assert _Recording.ran == [{"name": "n", "owner": "SERVER"}]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("provider", [_fills_nothing, _declines_owner], ids=["untyped", "typed"])
async def test_a_field_nobody_fills_is_handed_back_before_the_validator_runs(provider):
    """What makes not requiring ``owner`` safe: dispatch does not demand it of the
    caller, and when neither the provider nor the model fills it the kernel
    refuses the call before the Validator runs, naming the field as it names a
    read, and the model is handed that to send it next turn. The Validator never
    sees the call, so the retry is the kernel's wording and not the Validator's
    own, which is not the toolset's ``missing_arguments`` line either."""
    user = await User.objects.acreate(username="u")
    toolset = SpecToolset({"t": _service(provider)})

    with pytest.raises(ModelRetry) as raised:
        await _call(toolset, "t", user, {"name": "n"})

    assert str(raised.value) == "owner: This field is required."
    assert _Recording.ran == []


def test_a_read_is_still_required_beside_an_input_data_that_may_fill_a_field():
    """``input_data`` reaches the Validator's fields and no read: the target lookup
    still cannot run without its ``pk``, so only the fields are relaxed. Holds the
    seat to its own names."""
    spec = _service(_fills_owner, instance_selector_spec=by_pk(get_any_widget))
    toolset = SpecToolset({"t": spec})

    schema = _input_schema_of(toolset, "t")

    assert schema["required"] == ["pk"]
    assert sorted(schema["properties"]) == ["name", "owner", "pk"]


def test_a_key_the_input_data_may_decline_relaxes_only_a_field_of_the_validator():
    """``input_data`` reaches the Validator's fields only, so a key it may decline
    that names a read in the target lookup's seat says nothing about that read.
    Holds the ``names &`` for the Validator seat: relaxing every declinable key
    would leave ``pk`` optional."""
    toolset = SpecToolset(
        {"t": _service(_declines_pk, instance_selector_spec=by_pk(get_any_widget))}
    )

    assert _input_schema_of(toolset, "t")["required"] == ["pk", "name", "owner"]


async def test_a_call_missing_a_read_beside_an_input_data_is_still_handed_back():
    toolset = SpecToolset(
        {"t": _service(_fills_owner, instance_selector_spec=by_pk(get_any_widget))}
    )

    with pytest.raises(ModelRetry) as raised:
        await _call(toolset, "t", User(username="u"), {})

    assert str(raised.value) == "Missing required argument(s): `pk`."


def test_a_row_field_is_not_a_top_level_seat_on_a_many_spec():
    """A ``many`` spec's Validator fields are each row's, so ``input_data`` relaxes
    them inside every row and no top-level argument: a read sharing a row field's
    name stays required, and so does the list. Holds ``not compiled.many``."""
    spec = ServiceSpec(
        service=make_rows,
        validator=_Recording(Row),
        input_data=_fills_owner,
        many=True,
        reads=Parameters.of(Parameter("owner", "string", required=True)),
        atomic=False,
        permissions=allow(),
    )
    toolset = SpecToolset({"t": spec})

    schema = _input_schema_of(toolset, "t")

    assert sorted(schema["required"]) == sorted(["owner", spec.many_argument])
    # The rows are not relaxed either: the schema still lists the field there.
    assert schema["properties"][spec.many_argument]["items"]["required"] == ["name", "owner"]
