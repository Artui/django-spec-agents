"""What the model reads by default, pinned as text, through the public surface only.

Every line here is written out rather than imported: it is the text the model
reads, and a test comparing a default to itself would pass whatever it said.
Nothing in this file imports ``AgentConventions``, so the pins hold the bytes
whatever the type that carries them does.

The last test is the other half: the keyword through which the wording is
changed, asserted on both constructors' public signatures.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from django_service_specs import DEFAULT_PAGE_SIZE, Affordance
from pydantic_ai import ModelRetry
from rest_framework.permissions import AllowAny
from rest_framework_services import QueryParam, ServiceSpec

from django_spec_agents.spec_tools import SpecCapability, SpecToolset
from tests.spec_tools.test_spec_toolset import reading
from tests.spec_tools.wording_specs import approve, run_context, selector_specs

_BASE = (
    "The following tools call this application's services and selectors.\n"
    "- A successful call returns the tool's data. A business-rule failure comes back as a "
    "failed call whose content is a sentence explaining why — that is a final answer, not a "
    "reason to retry; read it and report it, do not call the same tool the same way again. "
    "The sentence may end with `(code: <name>)`, naming the rule that refused; it is the same "
    "code an item's `affordances` answer carries when a tool advertised one.\n"
    "- An invalid or missing argument comes back as a retry request naming the problem; "
    "correct the argument and call again.\n"
    "- A permission error is final: the current user may not perform that call — do not "
    "retry it.\n"
    "- Only pass documented parameters; unknown arguments are rejected."
)
_PAGINATION = (
    "- Read-only tools that return a collection always return one page, shaped "
    '{"items": [...], "page": 1, "totalPages": N, "hasNext": true|false}. They accept '
    f"optional `limit` (items per page, default {DEFAULT_PAGE_SIZE}) and `page` (1-based). "
    "When `hasNext` is true there are more items than you were shown: ask for the next "
    "`page`, or narrow the request with a filter — never answer as if the page were the "
    "whole collection."
)
_ORDERING = (
    "- Some collection tools also accept `ordering`. It takes exactly one of the values "
    "listed in that tool's schema (a sortable name, or the same name prefixed with `-` for "
    "descending) — not a comma-separated list, and not an arbitrary column."
)
_HANDLES = (
    "- Some tools return opaque identifier fields, described as such in the tool's "
    "output. Pass them to other tools that ask for one; refer to records by their "
    "name in anything you say, never by the identifier."
)
_READ_SHAPING = (
    "- Some tools accept read-shaping parameters (`fields`) that adjust the shape "
    "of the returned data without filtering it. On a tool that returns a page, they apply "
    "to each item in `items`, never to the page itself."
)
_UNAVAILABLE = (
    "- These operations exist but cannot be performed right now, so they are not among your "
    "tools. If the user asks for one, say it is unavailable at the moment and give the reason "
    "listed for it, rather than guessing why:\n"
    "  - `approve`: The books are closed."
)
_HANDLE_FIELD = (
    "An opaque identifier. Pass it to other tools that ask for one; refer to the record "
    "by its name in anything you say, never by this value."
)
_SCOPE = (
    "On a paged result it applies to each item in `items`, never to the page envelope "
    "(`items`, `page`, `totalPages`, `hasNext`)."
)


def _every_line() -> dict[str, Any]:
    """A spec map that turns on every line of the block and the unavailable list."""
    return {
        **selector_specs(),
        "approve": ServiceSpec(
            service=approve,
            atomic=False,
            affordances=[
                Affordance(code="books_closed", reason="The books are closed.", when=lambda: False)
            ],
            permission_classes=[AllowAny],
        ),
    }


def _toolset() -> SpecToolset:
    return SpecToolset(reading(_every_line(), {"list_widgets": [QueryParam("fields")]}))


async def test_the_default_instructions_block():
    instructions = await _toolset().get_instructions(run_context())

    assert instructions == "\n".join(
        [_BASE, _PAGINATION, _ORDERING, _HANDLES, _READ_SHAPING, _UNAVAILABLE]
    )


async def test_the_default_schema_wording():
    tools = await _toolset().get_tools(run_context())

    returned = tools["get_widget"].tool_def.return_schema
    accepted = tools["list_widgets"].tool_def.parameters_json_schema
    assert returned["properties"]["id"]["description"] == _HANDLE_FIELD
    assert accepted["properties"]["fields"]["description"] == _SCOPE


async def test_the_default_retries():
    toolset = _toolset()
    tools = await toolset.get_tools(run_context())

    with pytest.raises(ModelRetry) as rejected:
        await toolset.call_tool(
            "list_widgets", {"fields": "{items}"}, run_context(), tools["list_widgets"]
        )
    with pytest.raises(ModelRetry) as missing:
        await toolset.call_tool("get_widget", {}, run_context(), tools["get_widget"])

    assert rejected.value.message == (
        f"`fields` was rejected while rendering the result: Unknown field `items`. {_SCOPE}"
    )
    assert missing.value.message == "Missing required argument(s): `pk`."


@pytest.mark.parametrize("constructor", [SpecToolset, SpecCapability])
def test_the_wording_is_changed_through_one_keyword_on_both_constructors(constructor):
    """``conventions=``, defaulting to ``None``: the default wording, with nothing to import.

    Asserted on the public signature so it fails on its assertion, rather than at
    import, against a tree that has no way to change the wording at all.
    """
    parameters = inspect.signature(constructor.__init__).parameters

    assert "conventions" in parameters
    assert parameters["conventions"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["conventions"].default is None
