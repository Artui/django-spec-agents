"""In-process bridge from a django-spec-mcp server's registry to a Pydantic-AI toolset.

Requires the ``django-spec-agents[mcp]`` extra, so consumers import this module
lazily and the dependency on ``django_spec_mcp`` stays optional. django-spec-mcp
needs a ready app registry to import, which is one more reason this module is
never on a host import path. It imports nothing from Django REST Framework.
"""

from __future__ import annotations

import json
import re
from typing import Any

from asgiref.sync import sync_to_async
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest
from django_service_specs import NON_FIELD_ERRORS
from django_spec_mcp import JsonRpcError, JsonRpcErrorCode
from pydantic_ai import ModelRetry, ToolFailed
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from django_spec_agents.types.destructive_metadata_key import DESTRUCTIVE_METADATA_KEY

# A no-op validator: the parameter schemas advertised to the model come verbatim
# from the server's ``tools/list`` (advisory, not a Pydantic model), and the real
# validation is the server's own at call time, the same split the HTTP
# transport has.
_TOOL_ARGS_VALIDATOR = SchemaValidator(schema=core_schema.any_schema())

# The heading of the per-step list of tools an unmet operation condition left
# out, worded as ``spec_tools`` words it. A consumer can expose one spec either
# way, and the model is taught one sentence for a missing operation; a test
# asserts it against ``spec_tools``' real output, so a change on either side
# fails there rather than drifting.
_UNAVAILABLE_INSTRUCTION = (
    "- These operations exist but cannot be performed right now, so they are not among your "
    "tools. If the user asks for one, say it is unavailable at the moment and give the reason "
    "listed for it, rather than guessing why:"
)


def _unavailable_instruction(unavailable: dict[str, Any]) -> str:
    """The heading, then one line per tool left out: its name and its ``reason``.

    The ``code`` is left out, as the spec-tools route leaves it out: it is for
    programs and for tying a refusal to a row's ``affordances``, and a model
    relaying this to a person has no use for it.
    """
    lines = [_UNAVAILABLE_INSTRUCTION]
    lines.extend(f"  - `{name}`: {affordance.reason}" for name, affordance in unavailable.items())
    return "\n".join(lines)


def _parse_tool_error(result: dict[str, Any]) -> dict[str, Any]:
    """Extract the ``{"error": {...}}`` payload from an ``isError`` result.

    Both servers encode it as JSON text in ``content[0]``. Falls back to a
    generic shape rather than raising, so a changed encoding cannot turn
    reporting an error into a second error.
    """
    content = result.get("content") or []
    text: Any = content[0].get("text", "") if content else ""
    try:
        error = json.loads(text)["error"]
    except (ValueError, KeyError, TypeError):
        return {"type": "unknown", "message": str(text) or "tool error"}
    return error if isinstance(error, dict) else {"type": "unknown", "message": str(error)}


# The error keys that name a failure, in suffix order, with the label each is
# written under. ``code`` is a refusal's stable name (the server sends it for an
# ``ActionUnavailable``).
_FAILURE_LABELS: tuple[tuple[str, str], ...] = (("code", "code"),)


def _failure_message(error: dict[str, Any]) -> str:
    """Compose the ``ToolFailed`` text: the server's sentence, then what names it.

    ``ToolFailed`` carries a string and nothing else, so ``code`` rides as a
    suffix rather than being dropped. The
    ``(code: ...)`` form is the one ``spec_tools`` writes for the same refusal
    raised in process, so a model reads one wording whichever route a tool
    arrived by, and can match it to the ``code`` a row's ``affordances`` answer
    advertised. It is written for the model and for a person reading the tool
    call; a program wanting the code reads it where the server serves it, not
    out of this sentence.
    """
    message: str = str(error.get("message") or "tool error")
    labels: list[str] = [
        f"{label}: {error[key]}" for key, label in _FAILURE_LABELS if error.get(key)
    ]
    if not labels:
        return message
    return f"{message} ({', '.join(labels)})"


def _missing_input_prompt(error: dict[str, Any]) -> str:
    """Compose the ``ModelRetry`` text for ``input_required``: the service's
    message, plus the names it wants the answer back under.

    Worded as ``spec_tools`` words the same request, for its reason:
    ``requestedInput`` is a JSON-Schema *properties* mapping keyed by
    input name, the model is about to call the same tool again, and that tool's
    parameter schema already describes each argument. The names are what to add;
    a second, differently shaped description in prose is how a model ends up
    inventing a nested object.
    """
    message: str = str(error.get("message") or "additional input required")
    requested: Any = error.get("requestedInput")
    # Absent when the service named no schema; the server sends a mapping otherwise.
    if not requested:
        return message
    names: str = ", ".join(f"`{name}`" for name in requested)
    return f"{message} Call this tool again, additionally supplying: {names}."


def _retry_message(
    message: str,
    detail: Any,
    *,
    available: list[str] | None = None,
) -> str:
    """Compose the ``ModelRetry`` text: the server's message, plus whichever of
    field-level detail or the available tool names helps.

    Never both: a `-32602` is about either the name or the arguments, and the
    caller already decided which by whether the name was advertised.

    The detail is left off when the message already says it, and kept whole
    otherwise; see ``_message_says``.
    """
    if available is not None:
        names: str = ", ".join(sorted(available)) or "none"
        return f"{message}. Available tools: {names}."
    if not detail or _message_says(message, detail):
        return message
    return f"{message}: {json.dumps(detail, default=str)}"


def _message_says(message: str, detail: Any) -> bool:
    """Whether every name the detail carries appears in the message as a token.

    Appending a detail the message already states makes a model read one
    sentence twice, and makes this route read differently from the spec-tools
    route, which raises the toolset's sentence alone for the same omission.
    So the detail is dropped when the message names everything in it, and kept
    whole when it names anything less: a part of a detail is never sent, and
    the generic ``Invalid arguments``, which names nothing, keeps all of its
    detail.

    A field's name stands for its reasons. The only messages that name fields
    are the servers' missing-argument line, whose reason is always that the
    field is required, which the line says, and their render-time line, which
    quotes the reason after the name. That is the cost of this rule: a future
    message naming a field without its reason would drop the reason with the
    detail. Matching the reasons' words instead would keep the missing-argument
    detail, whose ``This field is required.`` that line paraphrases rather than
    quotes, and the duplicate with it.

    Each condition is held by a test in ``tests/integrations/test_mcp.py``:
    every name rather than any by
    ``test_a_detail_the_message_names_only_in_part_is_kept_whole``; the
    whole-token match by ``test_a_name_counts_only_as_a_whole_token``; and the
    non-field key, the kernel's ``NON_FIELD_ERRORS`` which django-spec-mcp
    writes a reason under no field with, by
    ``test_a_reason_under_no_field_counts_as_its_own_words`` and, through the
    bridge, ``test_the_non_field_key_is_the_kernels``.
    """
    names: list[str] = _detail_names(detail)
    return all(_names_token(message, name) for name in names)


def _detail_names(detail: Any, *, under_a_field: bool = False) -> list[str]:
    """What a detail names: every dict key at any depth, and the strings under none.

    A key is a field, so naming ``items`` says nothing about which of its own
    fields was refused, and a nested key counts as one more name. A value under
    no field key, a bare list or string or one under the non-field key, has no
    name to stand for it, so its own words are what the message must quote. The
    non-field key is structure rather than a field.
    """
    if isinstance(detail, dict):
        names: list[str] = []
        for key, value in detail.items():
            is_field: bool = str(key) != NON_FIELD_ERRORS
            if is_field:
                names.append(str(key))
            names.extend(_detail_names(value, under_a_field=is_field))
        return names
    if isinstance(detail, list):
        return [
            name for item in detail for name in _detail_names(item, under_a_field=under_a_field)
        ]
    return [] if under_a_field else [str(detail)]


def _names_token(message: str, name: str) -> bool:
    """Whether ``name`` appears in ``message`` with no word character either side.

    Lookarounds rather than ``\\b``: a word boundary needs a word character on
    one side, so a name that starts or ends with punctuation, ``tags[]`` say,
    would never match. A substring match would find ``a`` inside ``arguments``.
    """
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", message) is not None


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

    Failures split four ways, along MCP's protocol-vs-tool boundary:

    - JSON-RPC ``-32602`` and tool-level ``validation_error`` and
      ``input_required`` results raise ``pydantic_ai.ModelRetry``, so the model
      calls again with the field errors fixed or the missing input supplied,
      instead of the run dying;
    - every other tool-level failure (``service_error``, ``not_found``, a
      timeout, an oversized result) raises ``pydantic_ai.ToolFailed``, so the
      model reads the sentence and the call is recorded ``outcome="failed"``;
    - a permission refusing the user (``-32006``) raises Django's
      ``PermissionDenied``, which ``ToolFailurePolicy`` passes through by
      default, so the run ends;
    - every other protocol fault raises ``RuntimeError``: auth, rate limits,
      and an internal error (``-32603``), which is also the answer to a
      service asking for an input its spec does not declare in ``reads=``,
      since no answer to it could reach the service. ``ToolFailurePolicy``
      converts it to a failed call, so the run goes on.

    Running in process, the server has no client to ask, so a service's
    ``AdditionalInputRequired`` for inputs its spec declares arrives degraded,
    as ``input_required``.

    The mapping is the one ``spec_tools`` applies to the same exceptions raised
    in process, except for a selector's ``AdditionalInputRequired``:
    django-spec-mcp builds no input round for a selector, so it arrives as a
    service error and raises ``ToolFailed``, where ``spec_tools`` raises
    ``ModelRetry``. A refusal reads identically through both:
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
                args_validator=_TOOL_ARGS_VALIDATOR,
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
        return _unavailable_instruction(unavailable)

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
                # bridged mutation. MCP reads ``destructiveHint`` only on a tool
                # that is not read-only, and defaults it to true, so a tool is
                # marked unless ``readOnlyHint`` is true or ``destructiveHint``
                # is false: either a project's per-registration ``annotations``
                # override (django-spec-mcp keeps ``destructiveHint: true`` beside
                # a read-only one) exempts it. A tool listed with no
                # ``annotations`` says nothing and is left alone.
                metadata = (
                    {DESTRUCTIVE_METADATA_KEY: True}
                    if annotations
                    and annotations.get("readOnlyHint") is not True
                    and annotations.get("destructiveHint") is not False
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
                    _retry_message(
                        result.message,
                        (result.data or {}).get("detail"),
                        available=self._advertised_names(name),
                    )
                )
            if result.code == JsonRpcErrorCode.FORBIDDEN:
                # `-32006` is a permission check refusing the acting user.
                # Django's class, which ``ToolFailurePolicy`` passes through
                # by default, so the run ends: converted, it would leave the
                # model free to try the next id, and a refusal reading
                # differently from a missing row is an existence oracle.
                raise PermissionDenied(f"MCP tool {name!r} failed: {result.message}")
            # Everything else — auth, rate limits, an internal fault — is not
            # something the model can rewrite its way out of.
            raise RuntimeError(f"MCP tool {name!r} failed: {result.message}")
        if result.get("isError"):
            error = _parse_tool_error(result)
            error_type: Any = error.get("type")
            if error_type == "validation_error":
                # Kept on separate lines: Python 3.11's tracer attributes a
                # multi-line ``raise X(...)`` to the argument line, leaving the
                # ``raise`` line uncovered and tripping the 100% gate.
                retry = _retry_message(
                    error.get("message", "invalid arguments"),
                    error.get("detail"),
                )
                raise ModelRetry(retry)
            if error_type == "input_required":
                # django-spec-mcp degrades a service's request for more input to this
                # result when the caller cannot be asked mid-call, which an
                # in-process caller never can. "Call me again with these" is the
                # retry channel's own meaning, and it is what the spec-tools
                # route raises for the same exception.
                prompt = _missing_input_prompt(error)
                raise ModelRetry(prompt)
            # **Raised, not returned.** A returned ``{"error": error}`` is a
            # tool value, which pydantic-ai marks ``outcome="success"``: every
            # refusal, missing row and timeout would reach a transport streaming
            # the run as a completed call, told apart from a real result only by
            # the payload's wording. ``ToolFailed`` hands the model the same
            # sentence, spends no retry budget and prepends no correction
            # instructions, and marks the return ``outcome="failed"``.
            failure = _failure_message(error)
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
