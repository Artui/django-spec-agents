from __future__ import annotations

import inspect
import re
import warnings
from typing import Any

from django_spec_agents.registry.tool_registry import ToolRegistry


def build_tool_catalog(
    registry: ToolRegistry,
    *,
    mcp_server: Any = None,
    drf_mcp_server: Any = None,
    service_specs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The agent's server-tool catalog for the frontend to label tool-call cards.

    Server-side tools execute server-side, so their JSON Schema never reaches the
    browser and a web component cannot read an ``x-summary`` off it. This catalog
    is that channel: the component fetches it and maps tool name to label.

    Each entry is ``{"name", "summary", "description"?}``. ``summary`` is always
    present, resolved from ``@tool(summary=...)`` for registry tools and from
    ``display_name`` then ``title`` for an MCP server's, falling back to a
    prettified name. ``description`` is carried through when the source has one.

    **The catalog covers these sources and no others.** A tool attached through
    a transport's ``capabilities=`` / ``toolsets=`` is not listed and its card
    falls back to a prettified name — a degraded label, not a broken call.
    Enumerating those is not possible here: pydantic-ai exposes tool names only
    through ``AbstractToolset.get_tools``, which is async and needs a
    ``RunContext``, while this runs at configuration time with no run in sight.
    Route spec tools through a transport's ``service_specs=`` to keep labels.

    Args:
        registry: The ``@tool`` registry, listed first; it wins name collisions.
        mcp_server: An MCP server whose registered tools are appended: a
            django-spec-mcp ``MCPServer``, or a drf-mcp one, whose bindings
            carry the same four label fields.
        drf_mcp_server: Deprecated, removed in the next minor: the name
            ``mcp_server`` had while the only server was drf-mcp's. It takes
            the same server; passing both raises ``TypeError``.
        service_specs: A ``name -> spec`` mapping whose tools are appended, each
            described by its service or selector callable's docstring.

    Returns:
        One entry per tool, in that source order.

    Raises:
        TypeError: Both ``mcp_server`` and ``drf_mcp_server`` were passed.
    """
    # Each conjunct is held by a test: without the first, ``drf_mcp_server=``
    # alone raises (``test_the_old_keyword_still_lists_the_servers_tools``);
    # without the second, ``mcp_server=`` alone raises
    # (``test_a_spec_mcp_servers_tools_resolve_their_labels``). The refusal
    # itself is held by ``test_both_server_keywords_are_refused``.
    if mcp_server is not None and drf_mcp_server is not None:
        raise TypeError("build_tool_catalog() takes mcp_server or drf_mcp_server, not both")
    server: Any = mcp_server
    if drf_mcp_server is not None:
        warnings.warn(
            "build_tool_catalog(drf_mcp_server=) is deprecated and is removed in the next "
            "minor: pass the server as mcp_server=.",
            DeprecationWarning,
            stacklevel=2,
        )
        server = drf_mcp_server
    catalog: list[dict[str, Any]] = []
    seen: set[str] = set()
    for binding in registry:
        spec = binding.spec
        catalog.append(_entry(spec.name, spec.summary, spec.description))
        seen.add(spec.name)
    if server is not None:
        for binding in server.tools.all():
            if binding.name in seen:
                continue
            summary = getattr(binding, "display_name", None) or getattr(binding, "title", None)
            description = getattr(binding, "display_description", None) or binding.description
            catalog.append(_entry(binding.name, summary, description))
            seen.add(binding.name)
    if service_specs is not None:
        for name, spec in service_specs.items():
            if name in seen:
                continue
            catalog.append(_entry(name, None, _spec_description(spec)))
            seen.add(name)
    return catalog


def _spec_description(spec: Any) -> str | None:
    """A spec tool's blurb: the docstring of its service / selector callable.

    Read through ``getattr`` rather than ``isinstance`` so this never imports the
    kernel's spec classes; the ``[specs]`` extra stays optional.
    """
    callable_ = getattr(spec, "service", None) or getattr(spec, "selector", None)
    return inspect.getdoc(callable_) if callable_ is not None else None


def _entry(name: str, summary: str | None, description: str | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "summary": summary or _prettify(name)}
    if description:
        entry["description"] = description
    return entry


def _prettify(name: str) -> str:
    """Fallback label from a tool name: ``query_model`` → ``"Query model"``."""
    text = " ".join(word for word in re.split(r"[_\-\s]+", name) if word)
    return text[:1].upper() + text[1:]


__all__ = ["build_tool_catalog"]
