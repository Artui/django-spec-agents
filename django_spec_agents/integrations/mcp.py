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
# way, and the model is taught one sentence for a missing operation;
# ``test_the_missing_tools_heading_is_the_one_spec_tools_says`` asserts it against
# ``spec_tools``' real default, and
# ``test_a_missing_tool_reads_the_same_through_both_bridges`` compares the whole
# list across the routes, so a change on either side fails there rather than
# drifting.
_UNAVAILABLE_INSTRUCTION = (
    "- These operations exist but cannot be performed right now, so they are not among your "
    "tools. If the user asks for one, say it is unavailable at the moment and give the reason "
    "listed for it, rather than guessing why:"
)


def _unavailable_instruction(unavailable: dict[str, Any]) -> str:
    """The heading, then one line per tool left out: its name and its ``reason``."""
    lines = [_UNAVAILABLE_INSTRUCTION]
    lines.extend(f"  - `{name}`: {affordance.reason}" for name, affordance in unavailable.items())
    return "\n".join(lines)


def _parse_tool_error(result: dict[str, Any]) -> dict[str, Any]:
    """Extract the ``{"error": {...}}`` payload from an ``isError`` result."""
    content = result.get("content") or []
    text: Any = content[0].get("text", "") if content else ""
    try:
        error = json.loads(text)["error"]
    except (ValueError, KeyError, TypeError):
        return {"type": "unknown", "message": str(text) or "tool error"}
    return error if isinstance(error, dict) else {"type": "unknown", "message": str(error)}


def _tool_value(result: dict[str, Any]) -> Any:
    """The value a successful result hands the model."""
    if "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content")
    if not (isinstance(content, list) and len(content) == 1 and content[0].get("type") == "text"):
        return content
    text: str = content[0].get("text", "")
    try:
        return json.loads(text)
    except ValueError:
        return text


# The error keys that name a failure, in suffix order, with the label each is
# written under. ``code`` is a refusal's stable name (the server sends it for an
# ``ActionUnavailable``).
_FAILURE_LABELS: tuple[tuple[str, str], ...] = (("code", "code"),)


def _failure_message(error: dict[str, Any]) -> str:
    """Compose the ``ToolFailed`` text: the server's sentence, then what names it."""
    message: str = str(error.get("message") or "tool error")
    labels: list[str] = [
        f"{label}: {error[key]}" for key, label in _FAILURE_LABELS if error.get(key)
    ]
    if not labels:
        return message
    return f"{message} ({', '.join(labels)})"


def _missing_input_prompt(error: dict[str, Any]) -> str:
    """Compose the ``ModelRetry`` text for ``input_required``."""
    message: str = str(error.get("message") or "additional input required")
    requested: Any = error.get("requestedInput")
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
    """Compose the ``ModelRetry`` text: the server's message, plus the helpful detail."""
    if available is not None:
        names: str = ", ".join(sorted(available)) or "none"
        return f"{message}. Available tools: {names}."
    if not detail or _message_says(message, detail):
        return message
    return f"{message}: {json.dumps(detail, default=str)}"


def _message_says(message: str, detail: Any) -> bool:
    """Whether every name the detail carries appears in the message as a token."""
    names: list[str] = _detail_names(detail)
    return all(_names_token(message, name) for name in names)


def _detail_names(detail: Any, *, under_a_field: bool = False) -> list[str]:
    """What a detail names: every dict key at any depth, and the strings under none."""
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
    """Whether ``name`` appears in ``message`` with no word character either side."""
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", message) is not None


class MCPToolset(AbstractToolset[Any]):
    """Exposes a django-spec-mcp ``MCPServer``'s tools as a Pydantic-AI toolset."""

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
        # Loaded lazily: ``tools/list`` may touch the DB, which Django forbids on the event loop.
        self._tool_defs: list[ToolDefinition] | None = None

    @property
    def id(self) -> str | None:
        return "mcp"

    async def get_tools(self, ctx: Any) -> dict[str, ToolsetTool[Any]]:
        """The tools this step may call: every definition, less the unavailable."""
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
        """Name each tool left out this step, with the reason its condition gives."""
        unavailable = await self._unavailable_tools()
        if not unavailable:
            return None
        return _unavailable_instruction(unavailable)

    async def _unavailable_tools(self) -> dict[str, Any]:
        """Each tool django-spec-mcp would leave out of a listing now, with the condition."""
        unavailable: dict[str, Any] = await self._server.aunavailable_tools(
            user=self._request.user, request=self._request
        )
        return {
            name: affordance
            for name, affordance in unavailable.items()
            if name not in self._exclude_names
        }

    def _load_tool_defs(self) -> list[ToolDefinition]:
        """Page through django-spec-mcp's ``tools/list``, mapping each tool to a def."""
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
                metadata = (
                    {DESTRUCTIVE_METADATA_KEY: True}
                    if annotations
                    and annotations.get("readOnlyHint") is not True
                    and annotations.get("destructiveHint") is not False
                    else None
                )
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
                # `-32602` is a call the service could not dispatch: an unknown tool or bad arguments.
                raise ModelRetry(
                    _retry_message(
                        result.message,
                        (result.data or {}).get("detail"),
                        available=self._advertised_names(name),
                    )
                )
            if result.code == JsonRpcErrorCode.FORBIDDEN:
                raise PermissionDenied(f"MCP tool {name!r} failed: {result.message}")
            raise RuntimeError(f"MCP tool {name!r} failed: {result.message}")
        if result.get("isError"):
            error = _parse_tool_error(result)
            error_type: Any = error.get("type")
            if error_type == "validation_error":
                retry = _retry_message(
                    error.get("message", "invalid arguments"),
                    error.get("detail"),
                )
                raise ModelRetry(retry)
            if error_type == "input_required":
                prompt = _missing_input_prompt(error)
                raise ModelRetry(prompt)
            failure = _failure_message(error)
            raise ToolFailed(failure)
        return _tool_value(result)

    def _advertised_names(self, name: str) -> list[str] | None:
        """The tools this toolset offers, when the failure looks like a bad name."""
        if self._tool_defs is None:
            return None
        names: list[str] = [d.name for d in self._tool_defs]
        return None if name in names else names


__all__ = ["MCPToolset"]
