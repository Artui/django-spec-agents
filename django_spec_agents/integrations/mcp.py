"""In-process bridge from a django-spec-mcp server's registry to a Pydantic-AI toolset.

Requires the ``django-spec-agents[mcp]`` extra, so consumers import this module
lazily and the dependency on ``django_spec_mcp`` stays optional. django-spec-mcp
needs a ready app registry to import, which is one more reason this module is
never on a host import path. It imports nothing from Django REST Framework.
"""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async
from django.http import HttpRequest
from django_service_specs import NON_FIELD_ERRORS
from django_spec_mcp import JsonRpcError, JsonRpcErrorCode
from pydantic_ai import ModelRetry, ToolFailed
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool

from django_spec_agents.integrations.utils import (
    TOOL_ARGS_VALIDATOR,
    failure_message,
    missing_input_prompt,
    parse_tool_error,
    retry_message,
    unavailable_instruction,
)
from django_spec_agents.types.destructive_metadata_key import DESTRUCTIVE_METADATA_KEY


class MCPToolset(AbstractToolset[Any]):
    """Exposes a django-spec-mcp ``MCPServer``'s tools as a Pydantic-AI toolset.

    Built per request, so the agent acts as the request's logged-in user. Both
    schemas and execution route through django-spec-mcp's public in-process
    surface (``MCPServer.list_tools`` / ``acall_tool``), so the advertised
    parameters, the kernel's argument validation and the spec's permissions
    match the HTTP transport exactly, without the network hop. Tool definitions carry the default
    ``kind="function"``, the in-process kind the run loop calls itself; an
    ``external`` tool would instead be deferred to the client and never run.

    **Definitions once, availability every step.** django-spec-mcp leaves a tool
    out of ``tools/list`` while an operation-scope affordance refuses it, and
    whether one does can change mid-run — an admin closes the books. So the
    definitions are listed once, with every tool the user may see, and each step
    asks django-spec-mcp which of them a fresh listing would leave out
    (``aunavailable_tools``):
    those are not offered that step, and the instructions name
    each with its ``reason``, so a model asked for one can say why rather than
    guess or deny the operation exists. A server declaring no such condition
    answers that without leaving the event loop.

    Failures split three ways, along MCP's protocol-vs-tool boundary:

    - JSON-RPC ``-32602`` and tool-level ``validation_error`` and
      ``input_required`` results raise ``pydantic_ai.ModelRetry``, so the model
      calls again with the field errors fixed or the missing input supplied,
      instead of the run dying;
    - every other tool-level failure (``service_error``, ``not_found``, a
      timeout, an oversized result) raises ``pydantic_ai.ToolFailed``, so the
      model reads the sentence and the call is recorded ``outcome="failed"``;
    - protocol faults raise ``RuntimeError`` and abort the run: auth, a
      permission refusing the user (``-32006``), rate limits, and an internal
      error (``-32603``), which is also the answer to a service asking for an
      input its spec does not declare in ``reads=``, since no answer to it
      could reach the service.

    Running in process, the server has no client to ask, so a service's
    ``AdditionalInputRequired`` always arrives degraded, as ``input_required``.

    The mapping is the one ``spec_tools`` applies to the same
    exceptions raised in process, and a refusal reads identically through both:
    ``The books are closed. (code: books_closed)``.

    Args:
        server: The django-spec-mcp ``MCPServer`` whose registry is bridged.
        request: The request carried into every call; its ``user`` is the
            acting user.
        exclude_names: Names the ``@tool`` registry has already claimed. A
            colliding server tool is skipped, so the registry wins — the rule
            ``build_tool_catalog`` applies — because pydantic-ai raises
            ``UserError`` for a duplicate name at run time.
        max_retries: Per-tool retry budget: how many times a ``ModelRetry`` is
            fed back to the model before the run aborts. The default matches
            pydantic-ai's own function-tool default.
    """

    def __init__(
        self,
        server: Any,
        request: HttpRequest,
        *,
        exclude_names: frozenset[str] = frozenset(),
        max_retries: int = 1,
    ) -> None:
        self._server = server
        self._request = request
        self._exclude_names = exclude_names
        self._max_retries = max_retries
        # Loaded lazily in ``get_tools``: django-spec-mcp's ``tools/list`` may touch the
        # DB for per-user listing permissions, which Django forbids on the async
        # event loop this is constructed in. Every tool the user may see, the
        # unavailable ones included, so one that becomes available mid-run has a
        # definition to offer.
        self._tool_defs: list[ToolDefinition] | None = None

    @property
    def id(self) -> str | None:
        return "mcp"

    async def get_tools(self, ctx: Any) -> dict[str, ToolsetTool[Any]]:
        """The tools this step may call: every definition, less the unavailable.

        Definitions come from django-spec-mcp's ``tools/list`` once per toolset, which is
        once per request. Availability is asked again here every step, because
        pydantic-ai calls this once per step and a condition may flip between
        two: offering a tool django-spec-mcp would leave out only invites a refusal, and
        withholding one that became available hides an operation the model could
        now perform. The call enforces every condition whatever was offered.
        """
        if self._tool_defs is None:
            self._tool_defs = await sync_to_async(self._load_tool_defs)()
        unavailable = await self._unavailable_tools()
        return {
            tool_def.name: ToolsetTool(
                toolset=self,
                tool_def=tool_def,
                max_retries=self._max_retries,
                args_validator=TOOL_ARGS_VALIDATOR,
            )
            for tool_def in self._tool_defs
            if tool_def.name not in unavailable
        }

    async def get_instructions(self, ctx: Any) -> str | None:
        """Name each tool left out this step, with the reason its condition gives.

        ``get_tools`` withholds such a tool, so without this the model sees
        neither it nor any sign it exists, and a user asking for it gets a guess
        where a sentence was available. ``None`` when nothing is left out, which
        is every step for a server declaring no operation condition.

        Asked separately from ``get_tools``, as the spec-tools route asks: the
        run context carries no key that could safely scope one answer to one
        step, and asking twice costs a second evaluation of conditions that read
        only seeds. A condition flipping between the two reads can leave one
        step's tools and instructions disagreeing about a tool; the call's own
        enforcement is authoritative either way.
        """
        unavailable = await self._unavailable_tools()
        if not unavailable:
            return None
        return unavailable_instruction(unavailable)

    async def _unavailable_tools(self) -> dict[str, Any]:
        """Each tool django-spec-mcp would leave out of a listing now, with the condition.

        In django-spec-mcp's registry order, so a step always reads the same way. A name
        ``exclude_names`` claimed is dropped: the registry's tool of that name is
        the one offered, and telling the model it cannot run would be about a
        tool it never sees.
        """
        unavailable: dict[str, Any] = await self._server.aunavailable_tools(
            user=self._request.user, request=self._request
        )
        return {
            name: affordance
            for name, affordance in unavailable.items()
            if name not in self._exclude_names
        }

    def _load_tool_defs(self) -> list[ToolDefinition]:
        """Page through django-spec-mcp's ``tools/list``, mapping each tool to a def.

        The merged ``inputSchema`` is used verbatim, so nothing the model could
        send over HTTP is silently dropped in process. ``include_unavailable``
        lists the tools an unmet condition would leave out too, since this list
        outlives the step it is taken in; ``get_tools`` filters them per step.
        """
        defs: list[ToolDefinition] = []
        cursor: str | None = None
        while True:
            payload = self._server.list_tools(
                cursor,
                user=self._request.user,
                request=self._request,
                include_unavailable=True,
            )
            if isinstance(payload, JsonRpcError):
                raise RuntimeError(f"MCP tools/list failed: {payload.message}")
            for tool in payload["tools"]:
                if tool["name"] in self._exclude_names:
                    continue
                annotations = tool.get("annotations") or {}
                # Destructiveness rides ``metadata`` so ``ToolGuard`` can gate a
                # bridged mutation. Key on ``readOnlyHint``, not
                # ``destructiveHint``: django-spec-mcp omits the latter on read-only
                # tools, so only the former lets a project's per-registration
                # ``annotations`` override exempt a mutation.
                metadata = (
                    {DESTRUCTIVE_METADATA_KEY: True}
                    if annotations.get("readOnlyHint") is False
                    else None
                )
                # Passing django-spec-mcp's ``outputSchema`` through as ``return_schema``
                # is what lets a harness ``CodeMode`` capability render the tool
                # as a typed stub rather than ``-> Any``. Absent when a project
                # turns ``INCLUDE_OUTPUT_SCHEMA`` off.
                output_schema = tool.get("outputSchema")
                defs.append(
                    ToolDefinition(
                        name=tool["name"],
                        description=tool.get("description"),
                        parameters_json_schema=tool["inputSchema"],
                        metadata=metadata,
                        return_schema=output_schema,
                        include_return_schema=output_schema is not None,
                    )
                )
            cursor = payload.get("nextCursor")
            if cursor is None:
                break
        return defs

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: Any,
        tool: Any,
    ) -> Any:
        result = await self._server.acall_tool(
            name, tool_args, user=self._request.user, request=self._request
        )
        if isinstance(result, JsonRpcError):
            if result.code == JsonRpcErrorCode.INVALID_PARAMS:
                # `-32602` is a call django-spec-mcp could not dispatch at all: an
                # unknown tool or `arguments` that are not an object. Argument
                # values a tool refuses, a missing, unexpected or wrongly typed
                # one, arrive as a `validation_error` result instead and are
                # retried below. This is retried deliberately: `-32602` is by
                # definition a fault in the request the model produced, and a
                # wrong name is something it can change. pydantic-ai bounds the
                # retries, so an unfixable call still ends the run.
                raise ModelRetry(
                    retry_message(
                        result.message,
                        (result.data or {}).get("detail"),
                        non_field_key=NON_FIELD_ERRORS,
                        available=self._advertised_names(name),
                    )
                )
            # Everything else — auth, rate limits, an internal fault — is not
            # something the model can rewrite its way out of.
            raise RuntimeError(f"MCP tool {name!r} failed: {result.message}")
        if result.get("isError"):
            error = parse_tool_error(result)
            error_type: Any = error.get("type")
            if error_type == "validation_error":
                # Kept on separate lines: Python 3.11's tracer attributes a
                # multi-line ``raise X(...)`` to the argument line, leaving the
                # ``raise`` line uncovered and tripping the 100% gate.
                # The non-field key is the kernel's, which django-spec-mcp
                # writes a reason under no field with.
                retry = retry_message(
                    error.get("message", "invalid arguments"),
                    error.get("detail"),
                    non_field_key=NON_FIELD_ERRORS,
                )
                raise ModelRetry(retry)
            if error_type == "input_required":
                # django-spec-mcp degrades a service's request for more input to this
                # result when the caller cannot be asked mid-call, which an
                # in-process caller never can. "Call me again with these" is the
                # retry channel's own meaning, and it is what the spec-tools
                # route raises for the same exception.
                prompt = missing_input_prompt(error)
                raise ModelRetry(prompt)
            # **Raised, not returned.** A returned ``{"error": error}`` is a
            # tool value, which pydantic-ai marks ``outcome="success"``: every
            # refusal, missing row and timeout would reach a transport streaming
            # the run as a completed call, told apart from a real result only by
            # the payload's wording. ``ToolFailed`` hands the model the same
            # sentence, spends no retry budget and prepends no correction
            # instructions, and marks the return ``outcome="failed"``.
            failure = failure_message(error)
            raise ToolFailed(failure)
        return result.get("structuredContent", result.get("content"))

    def _advertised_names(self, name: str) -> list[str] | None:
        """The tools this toolset offers, when the failure looks like a bad name.

        Naming them is what makes retrying an invented name worth doing rather
        than merely survivable. ``None`` when there is nothing useful to add:
        the name *was* advertised, so the fault is the arguments, or
        ``get_tools`` has not run and the cache is empty.
        """
        if self._tool_defs is None:
            return None
        names: list[str] = [d.name for d in self._tool_defs]
        return None if name in names else names


__all__ = ["MCPToolset"]
