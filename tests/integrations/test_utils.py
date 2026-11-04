"""The wording the MCP bridge teaches the model, held on its own terms.

The bridge passes the key its server writes a reason under no field with; the
tests here pass the kernel's spelling, ``non_field_errors``. Which key the bridge
passes is held at its call site, in ``test_mcp.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from django_spec_agents.integrations.utils import failure_message, retry_message

_NON_FIELD_KEY = "non_field_errors"


def _retry_message(message: str, detail: Any) -> str:
    return retry_message(message, detail, non_field_key=_NON_FIELD_KEY)


def test_retry_message_without_detail_is_the_bare_message() -> None:
    assert _retry_message("nope", None) == "nope"


def test_a_detail_the_message_already_names_is_dropped() -> None:
    # The servers' missing-argument line names the argument its detail is keyed
    # by, and that detail's only reason is "required", which the line says.
    assert (
        _retry_message("Missing required argument(s): `pk`.", {"pk": ["This field is required."]})
        == "Missing required argument(s): `pk`."
    )


def test_a_name_counts_only_as_a_whole_token() -> None:
    """Holds the lookarounds: a name is matched as a token, never a substring.

    ``a`` sits inside ``arguments``, so a substring match would drop the only
    place the generic message's reason is written. A key ending in a non-word
    character still matches, which ``\\b`` would refuse.
    """
    detail = {"a": ["A valid integer is required."]}
    assert _retry_message("Invalid arguments", detail) == (
        'Invalid arguments: {"a": ["A valid integer is required."]}'
    )
    assert _retry_message("Missing required argument(s): `a`.", detail) == (
        "Missing required argument(s): `a`."
    )
    assert (
        _retry_message("Missing required argument(s): `tags[]`.", {"tags[]": ["Required."]})
        == "Missing required argument(s): `tags[]`."
    )


@pytest.mark.parametrize(
    "detail",
    [
        pytest.param({"items": {"name": ["This field is required."]}}, id="dict"),
        pytest.param({"items": [{}, {"name": ["This field is required."]}]}, id="list"),
    ],
)
def test_a_nested_key_is_a_name_the_message_must_carry(detail: Any) -> None:
    # Every key counts, at any depth: naming ``items`` says nothing about which
    # of its fields was refused.
    assert _retry_message("Invalid `items`.", detail) == (f"Invalid `items`.: {json.dumps(detail)}")
    assert _retry_message("Invalid `name` in `items`.", detail) == "Invalid `name` in `items`."


def test_a_detail_the_message_names_only_in_part_is_kept_whole() -> None:
    # Every name, not any: the field the message leaves out is still in the
    # detail, and the detail is never cut down to it.
    detail = {"pk": ["This field is required."], "name": ["This field is required."]}
    assert _retry_message("Missing required argument(s): `pk`.", detail) == (
        f"Missing required argument(s): `pk`.: {json.dumps(detail)}"
    )
    assert (
        _retry_message("Missing required argument(s): `name`, `pk`.", detail)
        == "Missing required argument(s): `name`, `pk`."
    )


_UNEXPECTED = "Unexpected argument(s): 'notify_owner'."


@pytest.mark.parametrize(
    "detail",
    [
        pytest.param({"non_field_errors": [_UNEXPECTED]}, id="non-field-key"),
        pytest.param([_UNEXPECTED], id="bare-list"),
        pytest.param(_UNEXPECTED, id="bare-string"),
    ],
)
def test_a_reason_under_no_field_counts_as_its_own_words(detail: Any) -> None:
    """Holds the non-field branch: the non-field key is structure, not a name.

    Its strings sit under no field, so they are what the message has to quote,
    as a bare list or string's are; the key itself never counts as said.
    """
    assert _retry_message(_UNEXPECTED, detail) == _UNEXPECTED
    assert _retry_message("Invalid arguments", detail) == (
        f"Invalid arguments: {json.dumps(detail)}"
    )


def test_the_non_field_key_is_the_one_passed() -> None:
    # The key is the caller's, not a spelling held here: under another key the
    # same reason is still structure, and ``non_field_errors`` becomes a field.
    detail = {"errors": [_UNEXPECTED]}
    assert retry_message(_UNEXPECTED, detail, non_field_key="errors") == _UNEXPECTED
    assert retry_message(
        _UNEXPECTED, {"non_field_errors": [_UNEXPECTED]}, non_field_key="errors"
    ) == (f"{_UNEXPECTED}: {json.dumps({'non_field_errors': [_UNEXPECTED]})}")


def test_a_failure_is_suffixed_with_its_code_alone() -> None:
    # ``failedStep`` named a drf-mcp chain step; django-spec-mcp has no chains
    # and never sends it, so a key by that name is not labelled.
    error = {"message": "The books are closed.", "code": "books_closed", "failedStep": "post"}
    assert failure_message(error) == "The books are closed. (code: books_closed)"
