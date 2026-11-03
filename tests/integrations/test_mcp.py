"""The ``[mcp]`` bridge, ``MCPToolset``, over django-spec-mcp servers of kernel specs.

Nothing here imports Django REST Framework: the bridge, its fixtures and these
tests hold on the kernel and django-spec-mcp alone.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest
from django.test import RequestFactory
from django_service_specs import (
    NON_FIELD_ERRORS,
    Parameter,
    Parameters,
    SelectorKind,
    SelectorSpec,
    Unrestricted,
)
from django_spec_mcp import JsonRpcError, JsonRpcErrorCode, MCPServer
from django_spec_mcp.schema import PAGED_QUERY_PARAM_SCOPE
from pydantic_ai import Agent, ModelRetry, ToolFailed
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.integrations.build_spec_capability import build_spec_capability
from django_spec_agents.integrations.mcp import MCPToolset
from django_spec_agents.types.destructive_metadata_key import DESTRUCTIVE_METADATA_KEY
from tests.integrations.helpers import LOOKUP_CALLS, run_context, run_omission_then_correction
from tests.integrations.kernel_specs import LOOKUP_SPECS
from tests.integrations.mcp_server import (
    BOOKS,
    REFUSED_SPEC,
    SELECTOR_ASKS_SPEC,
    bare_server,
    hinted_server,
    lookup_server,
    paged_server,
    server,
)
from tests.tool_retries import is_tool_answer, is_tool_retry, tool_answers, tool_retries


def _parts(result: Any, part_type: type) -> list[Any]:
    return [
        part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, part_type)
    ]


def _request() -> HttpRequest:
    request = RequestFactory().post("/agent/")
    request.user = AnonymousUser()  # type: ignore[attr-defined]
    return request


def _error_of(result: Any) -> dict[str, Any]:
    """The ``{"error": {...}}`` payload of an ``isError`` result, as the server wrote it."""
    assert isinstance(result, dict), result
    assert result["isError"] is True
    return json.loads(result["content"][0]["text"])["error"]


async def test_toolset_exposes_the_servers_tools_with_schemas() -> None:
    toolset = MCPToolset(server, _request())
    tools = await toolset.get_tools(None)  # type: ignore[arg-type]
    tool_def = tools["add"].tool_def
    assert tool_def.name == "add"
    assert tool_def.description == "Add two numbers."
    schema = tool_def.parameters_json_schema
    # Sourced from django-spec-mcp's own tools/list, verbatim.
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["a", "b"]
    # An in-process function rather than a deferred `external` call, so
    # pydantic-ai's run loop actually invokes our `call_tool`.
    assert tool_def.kind == "function"


async def test_a_list_tools_schema_is_the_servers_verbatim() -> None:
    # The pagination arguments a paged list tool takes are the server's, not
    # re-derived here, so the model is asked for what the HTTP transport asks.
    listed = paged_server.list_tools(None, user=AnonymousUser(), include_unavailable=True)
    assert not isinstance(listed, JsonRpcError)
    (tool,) = listed["tools"]
    tools = await MCPToolset(paged_server, _request()).get_tools(None)  # type: ignore[arg-type]
    schema = tools["list_rows"].tool_def.parameters_json_schema
    assert schema == tool["inputSchema"]
    assert {"page", "fields"} <= set(schema["properties"])
    # The output schema is carried too, so a code-mode stub is typed.
    assert tool["outputSchema"]
    tool_def = tools["list_rows"].tool_def
    assert tool_def.return_schema == tool["outputSchema"]
    assert tool_def.include_return_schema is True
    # A tool the server lists without one says so, rather than carrying ``None`` as a schema.
    bare = await MCPToolset(bare_server, _request()).get_tools(None)  # type: ignore[arg-type]
    assert bare["close_the_books"].tool_def.return_schema is None
    assert bare["close_the_books"].tool_def.include_return_schema is False


async def test_a_service_is_destructive_and_a_selector_is_not() -> None:
    # django-spec-mcp marks a selector ``readOnlyHint: true`` and a service
    # ``false``; the bridge carries the latter as the key ``ToolGuard`` reads.
    tools = await MCPToolset(lookup_server, _request()).get_tools(None)  # type: ignore[arg-type]
    assert tools["get_row"].tool_def.metadata is None
    assert tools["rename_row"].tool_def.metadata == {DESTRUCTIVE_METADATA_KEY: True}


def _listed_annotations(hinted: MCPServer) -> dict[str, Any]:
    listed = hinted.list_tools(None, user=AnonymousUser())
    assert not isinstance(listed, JsonRpcError)
    return {tool["name"]: tool["annotations"] for tool in listed["tools"]}


async def _hinted_metadata(name: str) -> dict[str, Any] | None:
    tools = await MCPToolset(hinted_server, _request()).get_tools(None)  # type: ignore[arg-type]
    return tools[name].tool_def.metadata


def test_the_producer_publishes_the_override_under_the_keys_the_bridge_reads() -> None:
    # The bridge's rule is only as true as the wire it reads: django-spec-mcp
    # merges a registration's ``annotations=`` over its derived defaults, and
    # keeps ``destructiveHint: true`` beside a read-only override.
    assert _listed_annotations(hinted_server) == {
        "plain": {"readOnlyHint": False, "destructiveHint": True},
        "read_only": {"readOnlyHint": True, "destructiveHint": True},
        "non_destructive": {"readOnlyHint": False, "destructiveHint": False},
        "destructive": {"readOnlyHint": False, "destructiveHint": True},
    }


async def test_a_read_only_override_is_not_destructive() -> None:
    # ``destructiveHint`` stays true on the wire here; ``readOnlyHint`` decides.
    assert await _hinted_metadata("read_only") is None


async def test_a_non_destructive_override_is_not_destructive() -> None:
    # A mutation registered ``annotations={"destructiveHint": False}`` is exempt,
    # as the MCP spec reads it, and as django-spec-mcp documents the override.
    assert await _hinted_metadata("non_destructive") is None


async def test_a_mutation_is_destructive_unless_it_says_otherwise() -> None:
    assert await _hinted_metadata("plain") == {DESTRUCTIVE_METADATA_KEY: True}
    assert await _hinted_metadata("destructive") == {DESTRUCTIVE_METADATA_KEY: True}


async def test_a_tool_listed_without_annotations_is_not_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unchanged from before the rule: a tool that says nothing is not claimed to
    # mutate, so an absent or empty ``annotations`` leaves it ungated.
    tools = [
        {"name": "absent", "inputSchema": {"type": "object"}},
        {"name": "empty", "inputSchema": {"type": "object"}, "annotations": {}},
    ]
    monkeypatch.setattr(server, "list_tools", lambda *_a, **_k: {"tools": tools})
    listed = await MCPToolset(server, _request()).get_tools(None)  # type: ignore[arg-type]
    assert listed["absent"].tool_def.metadata is None
    assert listed["empty"].tool_def.metadata is None


async def test_a_tool_that_leaves_read_only_unsaid_is_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MCP defaults ``readOnlyHint`` to false, so annotations that speak but do
    # not claim the tool reads (only a title, or only ``destructiveHint``) leave
    # it a mutation unless ``destructiveHint`` is false.
    tools = [
        {"name": "titled", "inputSchema": {"type": "object"}, "annotations": {"title": "T"}},
        {
            "name": "unsaid_false",
            "inputSchema": {"type": "object"},
            "annotations": {"destructiveHint": False},
        },
    ]
    monkeypatch.setattr(server, "list_tools", lambda *_a, **_k: {"tools": tools})
    listed = await MCPToolset(server, _request()).get_tools(None)  # type: ignore[arg-type]
    assert listed["titled"].tool_def.metadata == {DESTRUCTIVE_METADATA_KEY: True}
    assert listed["unsaid_false"].tool_def.metadata is None


@pytest.mark.django_db
async def test_agent_run_executes_the_tool_in_process() -> None:
    toolset = MCPToolset(server, _request())
    agent = Agent(TestModel(call_tools=["add"]), toolsets=[toolset])
    result = await agent.run("add two numbers")
    returns = [part for part in _parts(result, ToolReturnPart) if part.tool_name == "add"]
    assert returns, "the 'add' tool was deferred, not executed in-process"
    assert "result" in returns[0].content


@pytest.mark.django_db
async def test_agent_run_recovers_from_model_retry() -> None:
    def model_fn(messages: list, info: object) -> ModelResponse:
        last = messages[-1]
        if any(is_tool_retry(part) for part in last.parts):
            return ModelResponse(parts=[ToolCallPart(tool_name="add", args={"a": 5, "b": 3})])
        if any(is_tool_answer(part) for part in last.parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name="add", args={"a": "not_a_number", "b": 3})]
        )

    agent = Agent(FunctionModel(model_fn), toolsets=[MCPToolset(server, _request())])
    result = await agent.run("add two numbers")
    assert result.output == "done"
    assert [part.tool_name for part in tool_retries(result.all_messages())] == ["add"]
    assert [part.outcome for part in tool_answers(result.all_messages())] == ["success"]


async def test_the_id_and_the_retry_budget() -> None:
    toolset = MCPToolset(server, _request())
    assert toolset.id == "mcp"
    tools = await toolset.get_tools(None)  # type: ignore[arg-type]
    assert tools["add"].max_retries == 1
    tools = await MCPToolset(server, _request(), max_retries=3).get_tools(None)  # type: ignore[arg-type]
    assert tools["add"].max_retries == 3


async def test_loads_all_pages_from_tools_list(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        {"tools": [{"name": "p1", "inputSchema": {"type": "object"}}], "nextCursor": "c2"},
        {
            "tools": [{"name": "p2", "inputSchema": {"type": "object"}, "description": "two"}],
            "nextCursor": "c3",
        },
        {"tools": []},
    ]
    calls: list[str | None] = []

    def fake_list(cursor: str | None = None, **_kwargs: object) -> dict[str, object]:
        calls.append(cursor)
        return pages[len(calls) - 1]

    monkeypatch.setattr(server, "list_tools", fake_list)
    toolset = MCPToolset(server, _request())
    tools = await toolset.get_tools(None)  # type: ignore[arg-type]
    assert {"p1", "p2"} <= set(tools)
    assert calls == [None, "c2", "c3"]
    # A second call is memoised: no further tools/list round-trips.
    await toolset.get_tools(None)  # type: ignore[arg-type]
    assert calls == [None, "c2", "c3"]


async def test_tools_list_error_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    error = JsonRpcError(JsonRpcErrorCode.INVALID_PARAMS, "bad request")
    monkeypatch.setattr(server, "list_tools", lambda *_a, **_k: error)
    with pytest.raises(RuntimeError, match="^MCP tools/list failed: bad request$"):
        await MCPToolset(server, _request()).get_tools(None)  # type: ignore[arg-type]


@pytest.mark.django_db
async def test_toolset_invokes_the_tool_as_acting_user() -> None:
    toolset = MCPToolset(server, _request())
    tools = await toolset.get_tools(None)  # type: ignore[arg-type]
    assert await toolset.call_tool("add", {"a": 5, "b": 3}, None, tools["add"]) == {"result": 8}


async def test_an_unknown_tool_is_retryable_not_fatal() -> None:
    # django-spec-mcp answers an unknown tool with ``-32602``, a fault in the
    # request the model produced and something it can change.
    with pytest.raises(ModelRetry, match="Unknown tool"):
        await MCPToolset(server, _request()).call_tool("nope", {}, None, None)


async def test_a_bad_name_retry_names_the_real_tools() -> None:
    toolset = MCPToolset(server, _request())
    await toolset.get_tools(None)  # type: ignore[arg-type]
    with pytest.raises(ModelRetry, match="Available tools:.*add"):
        await toolset.call_tool("nope", {}, None, None)


async def test_a_bad_name_retry_says_nothing_extra_when_the_cache_is_cold() -> None:
    with pytest.raises(ModelRetry) as caught:
        await MCPToolset(server, _request()).call_tool("nope", {}, None, None)
    assert "Available tools" not in str(caught.value)


async def test_a_refusing_permission_ends_the_run() -> None:
    # django-spec-mcp answers a permission check that refuses the principal with
    # JSON-RPC ``-32006``. Nothing the model writes changes that, so the run
    # stops rather than burning its budget.
    with pytest.raises(RuntimeError) as caught:
        await MCPToolset(server, _request()).call_tool("forbidden", {"a": 1, "b": 2}, None, None)
    assert str(caught.value) == "MCP tool 'forbidden' failed: Insufficient permission"


async def test_a_refusing_permission_is_forbidden_on_the_wire() -> None:
    # The code the test above rests on, read off the real server.
    result = await server.acall_tool("forbidden", {"a": 1, "b": 2}, user=AnonymousUser())
    assert isinstance(result, JsonRpcError)
    assert result.code == JsonRpcErrorCode.FORBIDDEN


async def test_a_read_the_spec_does_not_declare_ends_the_run() -> None:
    # A service asking for input its spec does not take could never be answered:
    # django-spec-mcp refuses with JSON-RPC ``-32603`` rather than asking, and
    # the bridge raises it, as it raises every protocol fault.
    with pytest.raises(RuntimeError, match="^MCP tool 'undeclared_input' failed: .*'reason'"):
        await MCPToolset(server, _request()).call_tool("undeclared_input", {}, None, None)


async def test_malformed_arguments_raise_model_retry_with_detail() -> None:
    with pytest.raises(ModelRetry) as excinfo:
        await MCPToolset(server, _request()).call_tool(
            "add", {"a": "not_a_number", "b": 1}, None, None
        )
    assert str(excinfo.value) == 'Invalid arguments: {"a": ["Expected an integer."]}'


async def test_invalid_params_error_raises_model_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    # Payload-level twin of the ``-32602`` branch: on Python 3.11 the C tracer
    # can drop the bridge frame across the server's executor hop, leaving the
    # branch uncovered there even though it runs.
    async def fake_call(name: str, arguments: object = None, **_kwargs: object) -> JsonRpcError:
        return JsonRpcError(JsonRpcErrorCode.INVALID_PARAMS, "Invalid arguments")

    monkeypatch.setattr(server, "acall_tool", fake_call)
    with pytest.raises(ModelRetry, match="Invalid arguments"):
        await MCPToolset(server, _request()).call_tool("add", {"a": 1, "b": 2}, None, None)


async def test_a_protocol_fault_payload_ends_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # Payload-level twin of the permission test above, for the same tracer reason.
    async def denied(*_a: object, **_k: object) -> JsonRpcError:
        return JsonRpcError(JsonRpcErrorCode.FORBIDDEN, "Insufficient permission")

    monkeypatch.setattr(server, "acall_tool", denied)
    with pytest.raises(RuntimeError, match="^MCP tool 'add' failed: Insufficient permission$"):
        await MCPToolset(server, _request()).call_tool("add", {}, None, None)


async def test_service_validation_error_result_raises_model_retry() -> None:
    with pytest.raises(ModelRetry, match="must be even"):
        await MCPToolset(server, _request()).call_tool("invalid", {"a": 1, "b": 2}, None, None)


async def test_service_error_result_raises_tool_failed() -> None:
    with pytest.raises(ToolFailed) as excinfo:
        await MCPToolset(server, _request()).call_tool("denied", {"a": 1, "b": 2}, None, None)
    # Nothing names a plain ``ServiceError``, so no suffix is invented for one.
    assert excinfo.value.message == "denied by policy"


async def test_a_refusal_carries_its_code() -> None:
    with pytest.raises(ToolFailed) as excinfo:
        await MCPToolset(server, _request()).call_tool("refused", {}, None, None)
    assert excinfo.value.message == "The books are closed. (code: books_closed)"


async def test_a_missing_row_fails_the_call() -> None:
    # A selector's ``ObjectDoesNotExist`` is django-spec-mcp's ``not_found``,
    # which the model reads and the run records as a failed call.
    with pytest.raises(ToolFailed) as excinfo:
        await MCPToolset(server, _request()).call_tool("missing_row", {"pk": 9}, None, None)
    assert excinfo.value.message == "missing_row: no matching instance found"


async def test_a_refusal_reads_the_same_through_both_bridges() -> None:
    """The same refused kernel spec, called in process and over the bridge."""
    capability = build_spec_capability({"refused": REFUSED_SPEC})
    ctx = run_context(AgentDeps(user=AnonymousUser()))
    with pytest.raises(ToolFailed) as in_process:
        await capability.get_toolset().call_tool("refused", {}, ctx, None)
    with pytest.raises(ToolFailed) as bridged:
        await MCPToolset(server, _request()).call_tool("refused", {}, None, None)
    assert bridged.value.message == in_process.value.message


@pytest.mark.django_db
async def test_agent_run_records_a_refusal_as_a_failed_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The books close while the model is choosing, so the offered tool is refused.
    def model_fn(messages: list, info: AgentInfo) -> ModelResponse:
        if any(part.part_kind == "tool-return" for part in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        monkeypatch.setitem(BOOKS, "open", False)
        return ModelResponse(parts=[ToolCallPart(tool_name="refused", args={})])

    monkeypatch.setitem(BOOKS, "open", True)
    agent = Agent(FunctionModel(model_fn), toolsets=[MCPToolset(server, _request())])
    result = await agent.run("close the books")
    returns = [part for part in _parts(result, ToolReturnPart) if part.tool_name == "refused"]
    assert [(part.outcome, part.content) for part in returns] == [
        ("failed", "The books are closed. (code: books_closed)")
    ]


# ---------- availability, per step ----------

_MISSING = [
    "- These operations exist but cannot be performed right now, so they are not among your "
    "tools. If the user asks for one, say it is unavailable at the moment and give the reason "
    "listed for it, rather than guessing why:",
    "  - `refused`: The books are closed.",
]


async def test_a_tool_whose_condition_is_unmet_is_not_offered() -> None:
    tools = await MCPToolset(server, _request()).get_tools(None)  # type: ignore[arg-type]
    assert "add" in tools
    assert "refused" not in tools


async def test_a_tool_that_becomes_available_is_offered_without_listing_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listings: list[object] = []
    list_tools = server.list_tools

    def counting_list(*args: object, **kwargs: object) -> object:
        listings.append(kwargs.get("include_unavailable"))
        return list_tools(*args, **kwargs)

    monkeypatch.setattr(server, "list_tools", counting_list)
    toolset = MCPToolset(server, _request())
    closed = await toolset.get_tools(None)  # type: ignore[arg-type]
    monkeypatch.setitem(BOOKS, "open", True)
    opened = await toolset.get_tools(None)  # type: ignore[arg-type]
    assert "refused" not in closed
    assert opened["refused"].tool_def.name == "refused"
    assert listings == [True]


async def test_instructions_name_each_missing_tool_with_its_reason() -> None:
    instructions = await MCPToolset(server, _request()).get_instructions(None)
    assert instructions is not None
    assert instructions.splitlines() == _MISSING


async def test_no_instructions_when_nothing_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(BOOKS, "open", True)
    assert await MCPToolset(server, _request()).get_instructions(None) is None


async def test_a_name_the_registry_claimed_is_never_explained() -> None:
    toolset = MCPToolset(server, _request(), exclude_names=frozenset({"refused"}))
    assert await toolset.get_instructions(None) is None


async def test_a_missing_tool_reads_the_same_through_both_bridges() -> None:
    capability = build_spec_capability({"refused": REFUSED_SPEC})
    ctx = run_context(AgentDeps(user=AnonymousUser()))
    in_process = await capability.get_toolset().get_instructions(ctx)
    bridged = await MCPToolset(server, _request()).get_instructions(None)
    assert in_process is not None
    assert bridged is not None
    assert in_process.endswith(f"\n{bridged}")


@pytest.mark.django_db
async def test_each_step_offers_and_explains_what_is_available_then(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[bool, str | None]] = []

    def model_fn(messages: list, info: AgentInfo) -> ModelResponse:
        offered = "refused" in {tool.name for tool in info.function_tools}
        seen.append((offered, info.instructions))
        if len(seen) == 1:
            monkeypatch.setitem(BOOKS, "open", False)
            return ModelResponse(parts=[ToolCallPart(tool_name="add", args={"a": 1, "b": 2})])
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setitem(BOOKS, "open", True)
    agent = Agent(FunctionModel(model_fn), toolsets=[MCPToolset(server, _request())])
    await agent.run("close the books")
    (first_offered, first_said), (second_offered, second_said) = seen
    assert (first_offered, first_said) == (True, None)
    assert second_offered is False
    assert second_said is not None
    assert "\n".join(_MISSING) in second_said


# ---------- input required ----------


async def test_input_required_result_raises_model_retry() -> None:
    # In process django-spec-mcp cannot ask anyone, so it degrades the request to
    # an ``input_required`` result, which the bridge hands back as a retry.
    with pytest.raises(ModelRetry) as excinfo:
        await MCPToolset(server, _request()).call_tool("needs_input", {}, None, None)
    assert excinfo.value.message == (
        "Say why the books are being closed. Call this tool again, additionally supplying: `reason`."
    )


async def test_a_selectors_request_for_input_diverges_between_the_routes() -> None:
    """A selector's ``AdditionalInputRequired`` is a retry in process and a failure bridged.

    This documents a divergence rather than a goal. django-spec-mcp builds no
    input round for a selector (``tool_refusal_scene`` sets ``input_round`` to
    ``None``), so it answers the request as a ``service_error`` result and the
    bridge raises ``ToolFailed``; ``spec_tools`` maps the same exception, raised
    in process, to ``ModelRetry``. A service's request agrees on both routes
    (``test_input_required_result_raises_model_retry``).
    """
    capability = build_spec_capability({"selector_asks": SELECTOR_ASKS_SPEC})
    ctx = run_context(AgentDeps(user=AnonymousUser()))
    with pytest.raises(ModelRetry):
        await capability.get_toolset().call_tool("selector_asks", {}, ctx, None)
    with pytest.raises(ToolFailed) as bridged:
        await MCPToolset(server, _request()).call_tool("selector_asks", {}, None, None)
    assert bridged.value.message == "Say which row."
    assert (
        _error_of(await server.acall_tool("selector_asks", {}, user=AnonymousUser()))["type"]
        == "service_error"
    )


async def test_input_required_is_degraded_on_the_wire() -> None:
    # The answer the test above rests on, read off the real server.
    result = await server.acall_tool("needs_input", {}, user=AnonymousUser())
    assert _error_of(result) == {
        "type": "input_required",
        "message": "Say why the books are being closed.",
        "requestedInput": {"reason": {"type": "string"}},
    }


def _serve_text(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Make ``acall_tool`` answer with an ``isError`` result carrying ``text``."""

    async def fake_call(
        name: str, arguments: object = None, **_kwargs: object
    ) -> dict[str, object]:
        return {"isError": True, "content": [{"type": "text", "text": text}]}

    monkeypatch.setattr(server, "acall_tool", fake_call)


def _serve_error(monkeypatch: pytest.MonkeyPatch, error: dict[str, object]) -> None:
    """Make ``acall_tool`` answer with ``error``, encoded as django-spec-mcp encodes one."""
    _serve_text(monkeypatch, json.dumps({"error": error}))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            {"type": "input_required", "message": "Say why.", "requestedInput": {"a": {}, "b": {}}},
            "Say why. Call this tool again, additionally supplying: `a`, `b`.",
        ),
        ({"type": "input_required", "message": "Say why."}, "Say why."),
    ],
)
async def test_input_required_payload_raises_model_retry(
    monkeypatch: pytest.MonkeyPatch, error: dict[str, object], expected: str
) -> None:
    # Payload-level twin of the test above, for the Python 3.11 tracer reason.
    _serve_error(monkeypatch, error)
    with pytest.raises(ModelRetry) as excinfo:
        await MCPToolset(server, _request()).call_tool("add", {"a": 1, "b": 2}, None, None)
    assert excinfo.value.message == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            {"type": "not_found", "message": "add: no matching instance found"},
            "add: no matching instance found",
        ),
        (
            {"type": "service_error", "message": "Closed.", "code": "books_closed"},
            "Closed. (code: books_closed)",
        ),
        ({"type": "timeout"}, "tool error"),
    ],
)
async def test_other_error_payloads_raise_tool_failed(
    monkeypatch: pytest.MonkeyPatch, error: dict[str, object], expected: str
) -> None:
    _serve_error(monkeypatch, error)
    with pytest.raises(ToolFailed) as excinfo:
        await MCPToolset(server, _request()).call_tool("add", {"a": 1, "b": 2}, None, None)
    assert excinfo.value.message == expected


async def test_validation_error_payload_raises_model_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    # Payload-level twin of the integration tests above, for the tracer reason.
    _serve_error(
        monkeypatch, {"type": "validation_error", "message": "bad", "detail": {"a": ["nope"]}}
    )
    with pytest.raises(ModelRetry, match="bad.*nope"):
        await MCPToolset(server, _request()).call_tool("add", {"a": 1, "b": 2}, None, None)


@pytest.mark.parametrize(
    ("text", "message"),
    [("not json", "not json"), ('{"error": "boom"}', "boom")],
    ids=["unparseable", "non-dict-error"],
)
async def test_an_error_text_that_is_not_a_payload_falls_back_to_the_text(
    monkeypatch: pytest.MonkeyPatch, text: str, message: str
) -> None:
    _serve_text(monkeypatch, text)
    with pytest.raises(ToolFailed) as excinfo:
        await MCPToolset(server, _request()).call_tool("add", {"a": 1, "b": 2}, None, None)
    assert excinfo.value.message == message


# ---------- the non-field key ----------


async def test_a_bare_dispatch_error_is_detailed_under_the_kernels_non_field_key() -> None:
    # django-spec-mcp writes a bare ``DispatchError``'s sentence under the
    # kernel's ``NON_FIELD_ERRORS``, the key the bridge treats as structure.
    result = await server.acall_tool("undispatchable", {}, user=AnonymousUser())
    assert _error_of(result) == {
        "type": "validation_error",
        "message": "Invalid arguments",
        "detail": {NON_FIELD_ERRORS: ["The call could not be dispatched."]},
    }
    # The generic message quotes none of it, so the detail rides along whole.
    with pytest.raises(ModelRetry) as excinfo:
        await MCPToolset(server, _request()).call_tool("undispatchable", {}, None, None)
    assert str(excinfo.value) == (
        'Invalid arguments: {"non_field_errors": ["The call could not be dispatched."]}'
    )


async def test_the_non_field_key_is_the_kernels(monkeypatch: pytest.MonkeyPatch) -> None:
    # A message quoting the reason under the kernel's non-field key says all the
    # detail does, so the detail is dropped: the key is structure, not a field
    # the message would have to name. Under any other key it would be a name.
    sentence = "Unexpected argument(s): 'notify_owner'."
    _serve_error(
        monkeypatch,
        {"type": "validation_error", "message": sentence, "detail": {NON_FIELD_ERRORS: [sentence]}},
    )
    with pytest.raises(ModelRetry) as excinfo:
        await MCPToolset(server, _request()).call_tool("add", {"a": 1, "b": 2}, None, None)
    assert str(excinfo.value) == sentence


async def test_excluded_names_are_skipped_registry_wins() -> None:
    toolset = MCPToolset(server, _request(), exclude_names=frozenset({"add"}))
    tools = await toolset.get_tools(None)  # type: ignore[arg-type]
    assert "add" not in tools
    assert "denied" in tools


# ---------- rendering ----------


async def test_a_selection_refused_while_rendering_is_one_retry_then_the_page() -> None:
    # django-spec-mcp answers a ``shapes`` value its presenter refuses with an
    # ``isError`` ``validation_error`` result, and the bridge retries it. The
    # selection is the likeliest wrong one on a paged tool: the envelope.
    selections = iter(["items", "name"])

    def model_fn(messages: list, info: AgentInfo) -> ModelResponse:
        if any(is_tool_answer(part) for part in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name="list_rows", args={"fields": next(selections)})]
        )

    toolset = MCPToolset(paged_server, _request())
    result = await Agent(FunctionModel(model_fn), toolsets=[toolset]).run("list the rows")

    retries = [part.content for part in tool_retries(result.all_messages())]
    # The detail, keyed by ``fields``, is left off: the message names it and
    # quotes its reason.
    assert retries == [
        "`fields` was rejected while rendering the result: Unknown field `items`. "
        + PAGED_QUERY_PARAM_SCOPE
    ]
    page = [part.content for part in tool_answers(result.all_messages())]
    assert page[0]["items"] == [{"name": "a"}, {"name": "b"}]


# ---------- both routes, one spec ----------


@pytest.mark.parametrize("name", ["get_row", "rename_row"])
async def test_a_required_read_is_required(name: str) -> None:
    # The service tool advertises the target lookup its row is resolved through.
    tools = await MCPToolset(lookup_server, _request()).get_tools(None)  # type: ignore[arg-type]
    schema = tools[name].tool_def.parameters_json_schema
    assert "pk" in schema["properties"]
    assert "pk" in schema.get("required", [])


@pytest.mark.parametrize(
    ("mcp_server", "name", "arguments", "message", "detail"),
    [
        pytest.param(
            lookup_server,
            "get_row",
            {},
            "Missing required argument(s): `pk`.",
            {"pk": ["This field is required."]},
            id="missing",
        ),
        pytest.param(
            server,
            "add",
            {"a": "not_a_number", "b": 1},
            "Invalid arguments",
            {"a": ["Expected an integer."]},
            id="wrong-type",
        ),
    ],
)
async def test_refused_arguments_arrive_as_a_validation_error_result(
    mcp_server: MCPServer,
    name: str,
    arguments: dict[str, Any],
    message: str,
    detail: dict[str, Any],
) -> None:
    # The answer the retries here ride on, read off the real server.
    result = await mcp_server.acall_tool(name, arguments, user=AnonymousUser())
    assert _error_of(result) == {"type": "validation_error", "message": message, "detail": detail}


@pytest.mark.parametrize(("name", "omitted", "corrected", "row"), LOOKUP_CALLS)
async def test_a_call_missing_a_required_read_is_one_retry_then_the_row(
    name: str, omitted: dict[str, Any], corrected: dict[str, Any], row: dict[str, Any]
) -> None:
    retries, returns = await run_omission_then_correction(
        name, omitted, corrected, toolsets=[MCPToolset(lookup_server, _request())]
    )
    # The message names the one argument the detail is keyed by, so the detail
    # is left off: the model reads the sentence once, as on the spec-tools route.
    assert retries == ["Missing required argument(s): `pk`."]
    assert returns == [row]


@pytest.mark.parametrize(("name", "omitted", "corrected", "row"), LOOKUP_CALLS)
async def test_both_routes_hand_back_an_omission_in_the_same_words(
    name: str, omitted: dict[str, Any], corrected: dict[str, Any], row: dict[str, Any]
) -> None:
    bridged, _ = await run_omission_then_correction(
        name, omitted, corrected, toolsets=[MCPToolset(lookup_server, _request())]
    )
    in_process, _ = await run_omission_then_correction(
        name, omitted, corrected, capabilities=[build_spec_capability(LOOKUP_SPECS)]
    )
    assert len(bridged) == 1
    assert bridged == in_process


@pytest.mark.parametrize("name", ["get_row", "rename_row"])
async def test_both_bridges_require_the_same_arguments(name: str) -> None:
    ctx = run_context(AgentDeps(user=AnonymousUser()))
    in_process = await build_spec_capability(LOOKUP_SPECS).get_toolset().get_tools(ctx)
    bridged = await MCPToolset(lookup_server, _request()).get_tools(None)  # type: ignore[arg-type]
    required = bridged[name].tool_def.parameters_json_schema.get("required", [])
    assert sorted(required) == sorted(
        in_process[name].tool_def.parameters_json_schema.get("required", [])
    )
    assert "pk" in required


def _recent_rows(*, page: int = 1) -> list[dict[str, Any]]:
    """List rows, reading a parameter the list pipeline takes from every call."""
    return []


async def test_both_routes_refuse_a_list_selector_reading_page() -> None:
    """One spec neither route can serve, refused by both before a call is made.

    A list tool's ``page`` is its pagination argument, which both routes take
    out of the call before the selector runs, so a read named ``page`` would
    never receive the caller's value.
    """
    spec = SelectorSpec(
        kind=SelectorKind.LIST,
        selector=_recent_rows,
        reads=Parameters.of(Parameter("page", "integer")),
        permissions=[Unrestricted()],
    )
    with pytest.raises(ImproperlyConfigured, match=r"\['page'\], but `page` and `limit`"):
        MCPServer(name="refused").register_tool(
            spec, name="recent_rows", description="List rows.", paginate=True
        )
    with pytest.raises(ImproperlyConfigured, match=r"\['page'\], but `page` and `limit` are"):
        build_spec_capability({"recent_rows": spec})
