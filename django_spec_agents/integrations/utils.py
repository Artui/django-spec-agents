"""How the in-process MCP bridge words a server's answer for a model.

``MCPToolset`` bridges a django-spec-mcp server; the wording it teaches the model
for each failure is composed here. This module imports neither the server nor
Django REST Framework, so it is tested without either: the bridge passes the one
server value it needs, the key its server writes a detail under no field with.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic_core import SchemaValidator, core_schema

# A no-op validator: the parameter schemas advertised to the model come verbatim
# from the server's ``tools/list`` (advisory, not a Pydantic model), and the real
# validation is the server's own at call time, the same split the HTTP
# transport has.
TOOL_ARGS_VALIDATOR = SchemaValidator(schema=core_schema.any_schema())

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


def unavailable_instruction(unavailable: dict[str, Any]) -> str:
    """The heading, then one line per tool left out: its name and its ``reason``.

    The ``code`` is left out, as the spec-tools route leaves it out: it is for
    programs and for tying a refusal to a row's ``affordances``, and a model
    relaying this to a person has no use for it.
    """
    lines = [_UNAVAILABLE_INSTRUCTION]
    lines.extend(f"  - `{name}`: {affordance.reason}" for name, affordance in unavailable.items())
    return "\n".join(lines)


def parse_tool_error(result: dict[str, Any]) -> dict[str, Any]:
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


def failure_message(error: dict[str, Any]) -> str:
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


def missing_input_prompt(error: dict[str, Any]) -> str:
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


def retry_message(
    message: str,
    detail: Any,
    *,
    non_field_key: str,
    available: list[str] | None = None,
) -> str:
    """Compose the ``ModelRetry`` text: the server's message, plus whichever of
    field-level detail or the available tool names helps.

    Never both: a `-32602` is about either the name or the arguments, and the
    caller already decided which by whether the name was advertised.

    The detail is left off when the message already says it, and kept whole
    otherwise; see ``_message_says``. ``non_field_key`` is the key the bridge's
    server writes a reason under no field with.
    """
    if available is not None:
        names: str = ", ".join(sorted(available)) or "none"
        return f"{message}. Available tools: {names}."
    if not detail or _message_says(message, detail, non_field_key):
        return message
    return f"{message}: {json.dumps(detail, default=str)}"


def _message_says(message: str, detail: Any, non_field_key: str) -> bool:
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

    Each condition is held by a test in ``tests/integrations/test_utils.py``:
    every name rather than any by
    ``test_a_detail_the_message_names_only_in_part_is_kept_whole``; the
    whole-token match by ``test_a_name_counts_only_as_a_whole_token``; and the
    non-field key by ``test_a_reason_under_no_field_counts_as_its_own_words``,
    and that it is the caller's by ``test_the_non_field_key_is_the_one_passed``.
    Which key the bridge passes is held at its call site, by
    ``test_the_non_field_key_is_the_kernels`` in ``test_mcp.py``.
    """
    names: list[str] = _detail_names(detail, non_field_key)
    return all(_names_token(message, name) for name in names)


def _detail_names(detail: Any, non_field_key: str, *, under_a_field: bool = False) -> list[str]:
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
            is_field: bool = str(key) != non_field_key
            if is_field:
                names.append(str(key))
            names.extend(_detail_names(value, non_field_key, under_a_field=is_field))
        return names
    if isinstance(detail, list):
        return [
            name
            for item in detail
            for name in _detail_names(item, non_field_key, under_a_field=under_a_field)
        ]
    return [] if under_a_field else [str(detail)]


def _names_token(message: str, name: str) -> bool:
    """Whether ``name`` appears in ``message`` with no word character either side.

    Lookarounds rather than ``\\b``: a word boundary needs a word character on
    one side, so a name that starts or ends with punctuation, ``tags[]`` say,
    would never match. A substring match would find ``a`` inside ``arguments``.
    """
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", message) is not None


__all__ = [
    "TOOL_ARGS_VALIDATOR",
    "failure_message",
    "missing_input_prompt",
    "parse_tool_error",
    "retry_message",
    "unavailable_instruction",
]
