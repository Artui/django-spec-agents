from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import warnings
from collections.abc import Callable, Collection, Generator, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import cached_property, lru_cache
from types import MappingProxyType
from typing import Any, cast

from asgiref.sync import sync_to_async
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import connections
from django_service_specs import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_POOL_SEEDS,
    NON_FIELD_KEYS,
    UNSET,
    ActionUnavailable,
    AdditionalInputRequired,
    Affordance,
    AudienceProjection,
    DispatchResult,
    FieldAudience,
    Grant,
    InvalidArguments,
    NotPermitted,
    OutputPage,
    Parameter,
    PoolSeeds,
    PrincipalUnavailable,
    ProgressReporter,
    SelectorKind,
    ServerArguments,
    ServiceError,
    ServiceValidationError,
    SpecRegistry,
    UnknownArguments,
    apply_server_arguments,
    audience_projection_for_spec,
    build_call_context,
    check_server_arguments,
    dispatch,
    kernel_spec,
    operation_affordances,
    paginate_output,
    present_for_audience,
    seeded_pool,
    spec_caller_input_schema,
    spec_output_schema,
    unmet_operation_affordance,
)
from django_service_specs import SelectorSpec as KernelSelectorSpec
from django_service_specs import ServiceSpec as KernelServiceSpec
from django_service_specs import SpecSource as KernelSpecSource
from django_service_specs import server_owned_keys as kernel_server_owned_keys
from pydantic_ai import ModelRetry, RunContext, ToolFailed
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from django_spec_agents.spec_tools.types.agent_conventions import AgentConventions
from django_spec_agents.spec_tools.types.agent_transport import AGENT_TRANSPORT

# A spec of the kernel's, or a declaration that compiles to one, read through
# ``kernel_spec`` wherever the toolset asks it anything.
Spec = KernelServiceSpec | KernelSelectorSpec | KernelSpecSource
# Widening the parameter beats a ``from_registry`` constructor, which would have
# to restate every keyword of the signature it forwards to (and then drift).
SpecSource = Mapping[str, Spec] | SpecRegistry
UserExtractor = Callable[[RunContext[Any]], Any]
ProgressExtractor = Callable[[RunContext[Any]], ProgressReporter | None]

_ExceptionTranslator = Callable[[BaseException], "ExceptionHandler | None"]
# The three result-shaping seams. ``Callable[..., X]`` rather than a written-out
# signature because each is bound through a forwarding lambda that injects
# ``ctx``, and a precise type would have to describe the *unbound* shape while
# the call site uses the bound one.
_PageShaper = Callable[..., "OutputPage"]
_OutputRenderer = Callable[..., Any]
_ResultBounder = Callable[..., Any]

ExceptionHandler = Callable[[BaseException], Any]
"""Turns one exception into a tool result.

Three answers, and the choice is about what the model should do next. Raise
``ToolFailed`` for something it should report and stop on — that is what the
built-in arms do, and it marks the tool return ``outcome="failed"`` so a
transport can say so too. Raise ``ModelRetry`` to hand the call back for another
attempt. Or **return** a value, which becomes the tool's result verbatim and is
marked ``outcome="success"``: the escape hatch for an exception that is not
really a failure — a "nothing matched" a caller would rather express as an empty
payload than as a refusal.

Raising anything else aborts the run, which is the right answer for a genuine
bug.
"""

logger = logging.getLogger("django_spec_agents.spec_tools")
"""The package's one logger.

A dispatch behind an HTTP view leaves an access-log line; the same spec called
by a model leaves nothing, and that includes a permission denial, which over HTTP
is a 403 in the log and here is an exception the run loop absorbs into a
message. Timings go to ``DEBUG``, denials to ``WARNING``; named for the package
so ``LOGGING`` can set the two independently.
"""


def _run_extra(ctx: RunContext[Any]) -> dict[str, Any]:
    """Correlation fields for one tool call's log lines."""
    return {
        "run_id": ctx.run_id,
        "conversation_id": ctx.conversation_id,
        "run_step": ctx.run_step,
        "tool_call_id": ctx.tool_call_id,
    }


def _usage_extra(ctx: RunContext[Any]) -> dict[str, Any]:
    """The run's usage so far, as of this tool call."""
    usage = ctx.usage
    return {
        "run_input_tokens": usage.input_tokens,
        "run_output_tokens": usage.output_tokens,
        "run_requests": usage.requests,
        "run_tool_calls": usage.tool_calls,
    }


def _resolve_specs(specs: SpecSource) -> Mapping[str, Spec]:
    """Normalise a ``SpecSource`` to the plain mapping the internals expect, each spec compiled."""
    resolved: Mapping[str, Spec] = specs.specs() if isinstance(specs, SpecRegistry) else specs
    for name, spec in resolved.items():
        with _refusals_name_the_tool(name):
            try:
                kernel_spec(spec)
            except TypeError as exc:
                raise TypeError(f"SpecToolset tool {name!r}: {exc}") from exc
    return resolved


@contextmanager
def _refusals_name_the_tool(name: str) -> Generator[None, None, None]:
    """Prefix a refusal raised while building one tool with that tool."""
    try:
        yield
    except ImproperlyConfigured as exc:
        raise ImproperlyConfigured(f"SpecToolset tool {name!r}: {exc}") from exc


# List-selector pagination args own these names, so a spec's shape may not take
# one: a shape is no callable's argument, so ``page`` and ``limit`` would be
# taken out of the call to serve the page and never reach the presenter, and a
# shape named ``ordering`` would be advertised as the tool's sort while nothing
# sorts (``_validate_declared_reads``).
_RESERVED_PARAM_NAMES = frozenset({"page", "limit", "ordering"})

# Tool names are surfaced verbatim to the model provider, which constrains them
# to this shape (OpenAI / Anthropic function-name rules).
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# A no-op: the advertised parameter schemas are advisory (the kernel's
# ``spec_caller_input_schema``, not a Pydantic model) and the real validation is the
# kernel's shape check and the spec's own Validator at dispatch time.
_TOOL_ARGS_VALIDATOR = SchemaValidator(schema=core_schema.any_schema())

# The ``page`` tool arg a list selector accepts on top of its filter fields;
# ``limit`` is ``_limit_param_schema``, because its wording depends on the
# toolset's ceiling. Ordering is not here: it belongs to the spec whenever the
# spec's own schema advertises it (see ``_spec_ordering_argument``).
_PAGE_PARAM_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "description": "1-based page number.",
}
# ``page`` does not say "requires `limit`": every list result is a page, so an
# omitted ``limit`` is the default page size rather than "everything".


def _served_page_size(max_page_size: int | None) -> int:
    """The rows a list tool serves to a call that names no ``limit``."""
    if max_page_size is None:
        return DEFAULT_PAGE_SIZE
    return min(DEFAULT_PAGE_SIZE, max_page_size)


def _limit_param_schema(
    max_page_size: int | None, *, include_total: bool = False
) -> dict[str, Any]:
    """A list tool's ``limit`` arg: the default it is served, and its ceiling."""
    reported = (
        "`total`, `totalPages` and `hasNext`" if include_total else "`totalPages` and `hasNext`"
    )
    schema: dict[str, Any] = {
        "type": "integer",
        "minimum": 1,
        "description": (
            f"Maximum number of items per page. Defaults to {_served_page_size(max_page_size)}; "
            f"the result reports {reported}."
        ),
    }
    if max_page_size is not None:
        schema["maximum"] = max_page_size
    return schema


@dataclass(frozen=True)
class _PageArgs:
    """A list selector's stripped pagination tool args."""

    page: int | None
    limit: int | None


@dataclass(frozen=True)
class _CountedPage(OutputPage):
    """A page whose envelope also says how many rows matched, across every page."""

    def envelope(self, rendered: Any) -> dict[str, Any]:
        return {**super().envelope(rendered), "total": self.total}


_ENVELOPE: tuple[tuple[str, str], ...] = (
    ("items", "[...]"),
    ("page", "1"),
    ("totalPages", "N"),
    ("hasNext", "true|false"),
)
"""The kernel's page envelope as the model is shown it: each key, and what it holds.

In the order ``OutputPage.envelope`` writes them, the order both renderings of
``{envelope}`` keep. ``include_total`` adds ``total`` last, where
``_CountedPage`` writes it.
"""

_ENVELOPE_TOTAL = ("total", "T")


def _envelope_keys(include_total: bool) -> tuple[tuple[str, str], ...]:
    """The envelope's keys and sample values, with ``total`` under ``include_total``."""
    return (*_ENVELOPE, _ENVELOPE_TOTAL) if include_total else _ENVELOPE


def _envelope_shape(include_total: bool) -> str:
    """``{envelope}`` as ``pagination`` receives it: the page, written as an object."""
    shown = ", ".join(f'"{key}": {sample}' for key, sample in _envelope_keys(include_total))
    return f"{{{shown}}}"


def _envelope_listed(include_total: bool) -> str:
    """``{envelope}`` as the two read-shaping fields receive it: the keys, listed."""
    return _listed([key for key, _ in _envelope_keys(include_total)])


class SpecToolset(AbstractToolset[Any]):
    """Exposes specs as a Pydantic-AI toolset."""

    def __init__(
        self,
        specs: SpecSource,
        *,
        id: str = "spec-tools",
        instructions: str | None = None,
        conventions: AgentConventions | None = None,
        get_user: UserExtractor | None = None,
        get_progress: ProgressExtractor | None = None,
        unknown_arguments: UnknownArguments = UnknownArguments.REJECT,
        tool_server_arguments: Mapping[str, ServerArguments] | None = None,
        max_retries: int = 1,
        max_result_bytes: int | None = None,
        tool_max_result_bytes: Mapping[str, int | None] | None = None,
        max_page_size: int | None = None,
        include_total: bool = False,
        dispatch_timeout: float | None = None,
        require_permissions: bool = True,
        descriptions: Mapping[str, str] | None = None,
        exception_map: Mapping[type[BaseException], ExceptionHandler] | None = None,
        pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
        thread_sensitive: bool = True,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        # First, because it reads only the argument: a refused policy is wrong whatever the specs.
        _validate_unknown_arguments(unknown_arguments)
        resolved = _resolve_specs(specs)
        _validate_tool_names(resolved)
        _validate_permissions(resolved, require=require_permissions)
        self._tool_server_arguments: dict[str, ServerArguments] = _validate_server_arguments(
            tool_server_arguments, resolved, pool_seeds=pool_seeds
        )
        self._descriptions: dict[str, str] = _validate_descriptions(resolved, descriptions)
        self._id = id
        self._instructions_override = instructions
        self._conventions = conventions if conventions is not None else _DEFAULT_CONVENTIONS
        _validate_conventions_beside_instructions(self._conventions, instructions)
        self._specs: dict[str, Spec] = dict(resolved)
        self._get_user: UserExtractor = get_user or _default_get_user
        self._get_progress: ProgressExtractor = get_progress or _default_get_progress
        self._exception_map: dict[type[BaseException], ExceptionHandler] = dict(exception_map or {})
        self._unknown_arguments: UnknownArguments = unknown_arguments
        self._pool_seeds = pool_seeds
        self._max_retries = max_retries
        self._max_page_size = max_page_size
        self._include_total = include_total
        self._dispatch_timeout = dispatch_timeout
        self._thread_sensitive = thread_sensitive
        self._executor = executor
        overrides: Mapping[str, int | None] = tool_max_result_bytes or {}
        for tool_name in overrides:
            _refuse_an_unknown_tool("tool_max_result_bytes", tool_name, self._specs)
        self._tool_max_result_bytes: dict[str, int | None] = {
            name: overrides.get(name, max_result_bytes) for name in self._specs
        }
        self._tool_shapes: dict[str, frozenset[str]] = {
            name: kernel_spec(spec).shapes.names() for name, spec in self._specs.items()
        }
        for name, spec in self._specs.items():
            _validate_declared_reads(name, spec, seeds=pool_seeds)
            _validate_many_argument(name, spec, seeds=pool_seeds)
        _validate_inputs_pagination_takes(self._specs)
        self._projections: dict[str, AudienceProjection] = {}
        for name, spec in self._specs.items():
            with _refusals_name_the_tool(name):
                self._projections[name] = audience_projection_for_spec(spec)
        self._tool_defs: dict[str, ToolDefinition] = {}
        self._offered: dict[str, frozenset[str]] = {}
        for name, spec in self._specs.items():
            with _refusals_name_the_tool(name):
                try:
                    stamp = build_call_context(AGENT_TRANSPORT)
                    self._tool_defs[name] = _build_tool_def(
                        name,
                        spec,
                        self._descriptions.get(name),
                        self._max_page_size,
                        include_total=include_total,
                        shapes=self._tool_shapes[name],
                        projection=self._projections[name],
                        pool_seeds=pool_seeds,
                        conventions=self._conventions,
                        server_names=self._server_names(name),
                        unknown_arguments=unknown_arguments,
                        call_context=stamp,
                    )
                    self._offered[name] = kernel_spec(spec).parameters(call_context=stamp).names()
                except Exception as exc:
                    if not isinstance(exc, ImproperlyConfigured):
                        exc.add_note(f"SpecToolset tool {name!r}")
                    raise
        self._conditioned: tuple[str, ...] = tuple(
            name for name, spec in self._specs.items() if operation_affordances(spec)
        )
        self._instructions_without: Callable[[frozenset[str]], str | None] = lru_cache(
            maxsize=_INSTRUCTIONS_MEMO_SIZE
        )(self._derive_instructions_without)

    @property
    def id(self) -> str | None:
        return self._id

    @property
    def specs(self) -> Mapping[str, Spec]:
        """The resolved ``name -> spec`` mapping this toolset exposes."""
        return MappingProxyType(self._specs)

    def _server_names(self, name: str) -> frozenset[str]:
        """The names ``tool_server_arguments`` answers for on tool ``name``: none, unbound."""
        server = self._tool_server_arguments.get(name)
        return frozenset() if server is None else server.names()

    def _offered_shapes(self, name: str) -> frozenset[str]:
        """The names of tool ``name``'s read-shaping arguments the model is offered."""
        return self._tool_shapes[name] - self._server_names(name)

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        """The tool catalog this run is offered, one entry per listed spec."""
        unavailable = await self._unavailable_operations(ctx)
        return {
            name: ToolsetTool(
                toolset=self,
                tool_def=tool_def,
                max_retries=self._max_retries,
                args_validator=_TOOL_ARGS_VALIDATOR,
            )
            for name, tool_def in self._tool_defs.items()
            if name not in unavailable and await self.is_tool_listed(name, ctx)
        }

    async def is_tool_listed(self, name: str, ctx: RunContext[Any]) -> bool:
        """Whether ``name`` belongs in this run's catalog."""
        del name, ctx
        return True

    async def get_instructions(self, ctx: RunContext[Any]) -> str | None:
        """Teach the model this toolset's conventions."""
        unavailable = await self._unavailable_operations(ctx)
        block: str | None
        if self._instructions_override is not None:
            block = self._instructions_override
        elif unavailable:
            block = self._instructions_without(frozenset(unavailable))
        else:
            block = self._derived_instructions
        heading = self._conventions.unavailable_heading
        if not unavailable or heading is None:
            return block
        listed = _unavailable_instruction(unavailable, heading)
        return listed if block is None else f"{block}\n{listed}"

    @cached_property
    def _derived_instructions(self) -> str | None:
        """The conventions block, built once and kept."""
        return self._derive_instructions_without(frozenset())

    def _derive_instructions_without(self, omitted: frozenset[str]) -> str | None:
        """The conventions block for the tools a step offers, ``omitted`` aside."""
        offered = {name: spec for name, spec in self._specs.items() if name not in omitted}
        return _derive_instructions(
            offered,
            {name: self._offered_shapes(name) for name in offered},
            {name: self._projections[name] for name in offered},
            page_size=_served_page_size(self._max_page_size),
            include_total=self._include_total,
            pool_seeds=self._pool_seeds,
            conventions=self._conventions,
        )

    async def _unavailable_operations(self, ctx: RunContext[Any]) -> dict[str, Affordance]:
        """Each tool an operation condition refuses right now, with that condition."""
        if not self._conditioned:
            return {}
        user = self._get_user(ctx)
        return await sync_to_async(
            self._answer_operation_conditions,
            thread_sensitive=self._thread_sensitive,
            executor=self._executor,
        )(user, ctx)

    def _answer_operation_conditions(
        self, user: Any, ctx: RunContext[Any]
    ) -> dict[str, Affordance]:
        """Ask every conditioned spec, once, against the pool its call would see."""
        with _releasing_connections_opened_here():
            unavailable: dict[str, Affordance] = {}
            for name in self._conditioned:
                call_context = _call_context(ctx, name)
                seeds = _with_call_seeds(
                    self._pool_seeds, call_context, self.call_seeds(ctx=ctx, tool_name=name)
                )
                unmet = unmet_operation_affordance(
                    self._specs[name],
                    seeded_pool(user=user, seeds=seeds, call_context=call_context),
                    reserved=seeds.reserved,
                    call_context=call_context,
                )
                if unmet is not None:
                    unavailable[name] = unmet
            return unavailable

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        spec = self._specs[name]
        user = self._get_user(ctx)
        started: float = time.perf_counter()
        try:
            # The whole pipeline touches the ORM, which Django forbids on the async event loop.
            result = await _with_deadline(
                sync_to_async(
                    self._call_spec_releasing_connections,
                    thread_sensitive=self._thread_sensitive,
                    executor=self._executor,
                )(
                    spec,
                    user,
                    dict(tool_args),
                    ctx=ctx,
                    unknown_arguments=self._unknown_arguments,
                    server_arguments=self._tool_server_arguments.get(name),
                    max_page_size=self._max_page_size,
                    max_result_bytes=self._tool_max_result_bytes[name],
                    projection=self._projections[name],
                    offered=self._offered[name],
                    label=name,
                    progress=self._get_progress(ctx),
                ),
                self._dispatch_timeout,
                label=name,
                extra=_run_extra(ctx),
            )
        except (NotPermitted, PrincipalUnavailable, PermissionDenied):
            logger.warning(
                "Permission denied calling tool %r on toolset %r",
                name,
                self._id,
                extra=_run_extra(ctx),
            )
            raise
        logger.debug(
            "Tool %r on toolset %r took %.1f ms",
            name,
            self._id,
            (time.perf_counter() - started) * 1000,
            extra=_run_extra(ctx) | _usage_extra(ctx),
        )
        return result

    # The front half of a call: what it carries beside its arguments.

    def call_seeds(
        self, *, ctx: RunContext[Any], tool_name: str
    ) -> Mapping[str, Callable[..., Any]]:
        """The seeds one call registers beside the mount's ``pool_seeds``, by name."""
        del ctx, tool_name
        return {}

    def translate_exception(
        self, exc: BaseException, *, ctx: RunContext[Any]
    ) -> ExceptionHandler | None:
        """Return a handler for ``exc``, or ``None`` to leave it to the defaults."""
        del ctx
        for klass in type(exc).__mro__:
            handler = self._exception_map.get(cast(type[BaseException], klass))
            if handler is not None:
                return handler
        return None

    # The back half of a call: everything between the dispatch and the tool result.

    def shape_page(
        self,
        rows: Any,
        *,
        ctx: RunContext[Any],
        page: int | None,
        limit: int | None,
        max_page_size: int | None,
    ) -> OutputPage:
        """Slice a list selector's rows into the page this call serves."""
        del ctx
        shaped = _shape_list(rows, page=page, limit=limit, max_page_size=max_page_size)
        if not self._include_total:
            return shaped
        return _CountedPage(
            items=shaped.items, page=shaped.page, limit=shaped.limit, total=shaped.total
        )

    def render_output(
        self,
        spec: Spec,
        result: DispatchResult,
        *,
        ctx: RunContext[Any],
        projection: AudienceProjection | None,
    ) -> Any:
        """Turn a dispatch result into the payload the model reads."""
        del ctx
        return present_for_audience(spec, result, projection=projection)

    def enforce_result_bytes(
        self, payload: Any, *, ctx: RunContext[Any], max_bytes: int | None, label: str
    ) -> Any:
        """Return ``payload``, or raise a model-readable refusal when it is over budget."""
        return _enforce_result_bytes(
            payload, max_bytes=max_bytes, label=label, extra=_run_extra(ctx)
        )

    def _call_spec(
        self,
        spec: Spec,
        user: Any,
        args: dict[str, Any],
        *,
        ctx: RunContext[Any],
        label: str,
        **kw: Any,
    ) -> Any:
        """Build the call's context and seeds, bind the seams to this run, then run the pipeline."""
        call_context = _call_context(ctx, label)
        hook_seeds = self.call_seeds(ctx=ctx, tool_name=label)
        pool_seeds = _with_call_seeds(self._pool_seeds, call_context, hook_seeds)
        server = kw.get("server_arguments")
        if server is not None and hook_seeds:
            with _refusals_name_the_tool(label):
                check_server_arguments(
                    spec, server, pool_seeds=self._pool_seeds.extend(**hook_seeds)
                )
        return _call_spec(
            spec,
            user,
            args,
            label=label,
            pool_seeds=pool_seeds,
            call_context=call_context,
            translate_exception=lambda exc: self.translate_exception(exc, ctx=ctx),
            shape_page=lambda *a, **kwargs: self.shape_page(*a, ctx=ctx, **kwargs),
            render_output=lambda *a, **kwargs: self.render_output(*a, ctx=ctx, **kwargs),
            enforce_result_bytes=lambda *a, **kwargs: self.enforce_result_bytes(
                *a, ctx=ctx, **kwargs
            ),
            conventions=self._conventions,
            include_total=self._include_total,
            **kw,
        )

    def _call_spec_releasing_connections(
        self, spec: Spec, user: Any, args: dict[str, Any], *, ctx: RunContext[Any], **kw: Any
    ) -> Any:
        """``_call_spec``, plus the connection cleanup the thread hop owes."""
        with _releasing_connections_opened_here():
            return self._call_spec(spec, user, args, ctx=ctx, **kw)


@contextmanager
def _releasing_connections_opened_here() -> Generator[None]:
    """Close, on the way out, each connection this thread opened inside the block."""
    held_before = {
        conn.alias for conn in connections.all(initialized_only=True) if conn.connection is not None
    }
    try:
        yield
    finally:
        for conn in connections.all(initialized_only=True):
            if conn.alias not in held_before:
                conn.close()


def _validate_permissions(specs: Mapping[str, Spec], *, require: bool) -> None:
    """Refuse — or warn about — specs with nothing gating them off HTTP."""
    unguarded = [name for name, spec in specs.items() if kernel_spec(spec).permissions is None]
    if not unguarded:
        return
    names: str = ", ".join(repr(name) for name in sorted(unguarded))
    problem = (
        f"SpecToolset was given spec(s) with no permissions: {names}. "
        "A toolset dispatches off HTTP, where no view supplies permissions of its "
        "own (neither a view's permissions nor a project default apply), so "
        "nothing gates these calls and the model can make any of them. Declare "
        "permissions on each spec."
    )
    if require:
        raise ImproperlyConfigured(
            f"{problem} To downgrade this to a warning while you migrate, pass "
            "require_permissions=False."
        )
    warnings.warn(
        f"{problem} This is a warning because require_permissions=False.",
        UnguardedSpecWarning,
        stacklevel=3,
    )


class UnguardedSpecWarning(UserWarning):
    """A spec was exposed as a tool with no ``permissions``."""


class UndescribedToolWarning(UserWarning):
    """A spec was exposed as a tool with nothing to tell the model it exists for."""


def _validate_descriptions(
    specs: Mapping[str, Spec],
    descriptions: Mapping[str, str] | None,
) -> dict[str, str]:
    """Resolve each tool's description, warning about the ones that say nothing."""
    for tool_name in descriptions or {}:
        _refuse_an_unknown_tool("descriptions", tool_name, specs)
    resolved: dict[str, str] = {}
    blank: list[str] = []
    for name, spec in specs.items():
        text: str = ((descriptions or {}).get(name) or _spec_description(spec) or "").strip()
        if not text:
            blank.append(name)
            continue
        resolved[name] = text
    if blank:
        names: str = ", ".join(repr(name) for name in sorted(blank))
        warnings.warn(
            f"SpecToolset tool(s) {names} have no description: neither a "
            "descriptions={...} entry nor a docstring on the spec's callable. A "
            "model picks tools almost entirely by description, so an undescribed "
            "tool is one it will call at the wrong time or not at all.",
            UndescribedToolWarning,
            stacklevel=3,
        )
    return resolved


def _refuse_an_unknown_tool(knob: str, tool_name: str, specs: Mapping[str, Spec]) -> None:
    """``ValueError`` when a per-tool mapping's key names a tool this toolset does not expose."""
    if tool_name not in specs:
        raise ValueError(
            f"{knob} references unknown tool {tool_name!r}; known tools: {sorted(specs)}."
        )


def _validate_tool_names(specs: Mapping[str, Spec]) -> None:
    """Fail fast when a tool name violates the model provider's name constraint."""
    invalid = sorted(name for name in specs if not _TOOL_NAME_RE.match(name))
    if invalid:
        raise ValueError(
            "SpecToolset tool names must match ^[a-zA-Z0-9_-]{1,64}$ (model provider "
            f"function-name constraint); invalid name(s): {invalid}."
        )


def _validate_server_arguments(
    tool_server_arguments: Mapping[str, ServerArguments] | None,
    specs: Mapping[str, Spec],
    *,
    pool_seeds: PoolSeeds,
) -> dict[str, ServerArguments]:
    """Each tool's binding, checked against its spec, as a mapping of the toolset's own."""
    bindings = dict(tool_server_arguments or {})
    pool_seeds = pool_seeds.extend(**{name: _no_value for name in _CALL_SEEDS - pool_seeds.names})
    for tool_name, server in bindings.items():
        _refuse_an_unknown_tool("tool_server_arguments", tool_name, specs)
        with _refusals_name_the_tool(tool_name):
            check_server_arguments(specs[tool_name], server, pool_seeds=pool_seeds)
    return bindings


# The names ``_pop_pagination`` takes out of every list tool's call. Not
# ``_RESERVED_PARAM_NAMES``: ``ordering`` is left in the call for a spec that
# advertises it, so a selector taking ``ordering`` receives it.
_PAGINATION_ARGUMENTS = frozenset({"page", "limit"})


def _validate_inputs_pagination_takes(specs: Mapping[str, Spec]) -> None:
    """Refuse a list selector's parameter that the pagination arguments take away."""
    for tool_name, spec in specs.items():
        with _refusals_name_the_tool(tool_name):
            own = kernel_spec(spec).parameters().names()
            for phase in _SCHEMA_PHASES:
                _schema_fragment(spec, phase)
            _refuse_unsendable_fields(spec)
        pagination = sorted(own & _PAGINATION_ARGUMENTS) if _is_list_selector(spec) else []
        if pagination:
            raise ImproperlyConfigured(
                f"SpecToolset tool {tool_name!r}: the selector takes parameter(s) "
                f"{pagination!r}, but `page` and "
                "`limit` are a list tool's pagination arguments, which the toolset takes "
                "out of the call before the selector runs, so the parameter would never "
                "receive the model's value. Rename the parameter."
            )


def _refuse_unsendable_fields(spec: Spec) -> None:
    """Refuse an input field the spec's Validator requires and no call can send."""
    compiled = kernel_spec(spec)
    if not isinstance(compiled, KernelServiceSpec) or compiled.partial:
        return
    unsendable = sorted(getattr(compiled.validator, "unsendable", frozenset()))
    if unsendable:
        raise ImproperlyConfigured(
            f"the input requires {unsendable!r}, which no call can send: the spec's "
            "Validator fills the name itself and declares no parameter for it, so "
            "the tool does not advertise the field, a call sending it is refused under "
            "REJECT or has it dropped under IGNORE, and the Validator then fails every "
            "call for the missing field. Rename the field or make it optional."
        )


# ``metadata["json_schema"]``, keyed by phase: a schema fragment a spec
# declares over its derived schema. The kernel's schema builders read no
# metadata, so the toolset reads the fragment itself, to keep a consumer's
# declaration reaching the model.
_JSON_SCHEMA_METADATA_KEY = "json_schema"
_SCHEMA_PHASES = ("input", "output")


def _schema_fragment(spec: Spec, phase: str) -> Mapping[str, Any] | None:
    """The spec's ``metadata["json_schema"][phase]`` fragment, or ``None`` if undeclared."""
    declared = (kernel_spec(spec).metadata or {}).get(_JSON_SCHEMA_METADATA_KEY)
    if declared is None:
        return None
    label = f"{type(spec).__name__}.metadata[{_JSON_SCHEMA_METADATA_KEY!r}]"
    if not isinstance(declared, Mapping):
        raise ImproperlyConfigured(
            f"{label} must be a mapping keyed by schema phase "
            f"({' / '.join(repr(name) for name in _SCHEMA_PHASES)}); got "
            f"{type(declared).__name__}."
        )
    unknown = sorted(str(key) for key in declared if key not in _SCHEMA_PHASES)
    if unknown:
        raise ImproperlyConfigured(
            f"{label} declares {', '.join(repr(name) for name in unknown)}, which name no "
            f"schema phase. Nest the fragment under "
            f"{' or '.join(repr(name) for name in _SCHEMA_PHASES)} \u2014 an input schema and an "
            "output schema describe different things and cannot share one title or one "
            "description."
        )
    fragment = declared.get(phase)
    if fragment is None:
        return None
    if not isinstance(fragment, Mapping):
        raise ImproperlyConfigured(
            f"{label}[{phase!r}] must be a mapping of JSON Schema keys to merge; got "
            f"{type(fragment).__name__}."
        )
    return fragment


def _declared_reads(compiled: KernelServiceSpec | KernelSelectorSpec) -> tuple[Parameter, ...]:
    """The reads ``compiled`` declares, in every seat a caller's argument reaches."""
    seats = [compiled.reads]
    target = compiled.target_selector_spec() if isinstance(compiled, KernelServiceSpec) else None
    if target is not None:
        seats.append(target.reads)
    declared = (parameter for reads in seats for parameter in reads if not parameter.reflected)
    return (*declared, *compiled.shapes)


def _validate_declared_reads(tool_name: str, spec: Spec, *, seeds: PoolSeeds) -> None:
    """Refuse a read the spec declares under a name the toolset or a pool seed fills."""
    compiled = kernel_spec(spec)
    declared = _declared_reads(compiled)
    shapes = compiled.shapes.names()
    taken = sorted(
        {parameter.name for parameter in declared} & seeds.reserved | shapes & _RESERVED_PARAM_NAMES
    )
    if taken:
        raise ImproperlyConfigured(
            f"SpecToolset tool {tool_name!r}: read name(s) {taken} collide with reserved "
            f"transport keys {sorted(seeds.reserved | _RESERVED_PARAM_NAMES)}: a pool seed "
            "fills each such name on every call, and the toolset takes page, limit and "
            "ordering for itself. Rename them."
        )


def _validate_many_argument(tool_name: str, spec: Spec, *, seeds: PoolSeeds) -> None:
    """Refuse a ``many`` spec whose list is named after a seed the mount fills."""
    compiled = kernel_spec(spec)
    if not isinstance(compiled, KernelServiceSpec) or not compiled.many:
        return
    if compiled.many_argument in seeds.reserved | _CALL_SEEDS:
        raise ImproperlyConfigured(
            f"SpecToolset tool {tool_name!r}: the list argument {compiled.many_argument!r} "
            "is also a pool seed's name, which fills it on every call, so no call could "
            "send the list. Rename the spec's many_argument or the seed."
        )


async def _with_deadline(
    awaitable: Any,
    seconds: float | None,
    *,
    label: str,
    extra: Mapping[str, Any] | None = None,
) -> Any:
    """Await ``awaitable``, answering the model instead of hanging past ``seconds``."""
    if seconds is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except TimeoutError as exc:
        logger.warning("Tool %r exceeded its %.1fs dispatch timeout", label, seconds, extra=extra)
        raise ToolFailed(
            f"This call took longer than the {seconds:g}s limit and was "
            "abandoned. Narrow the request — add or tighten a filter, or "
            "lower `limit` — and call again."
        ) from exc


def _default_get_progress(ctx: RunContext[Any]) -> ProgressReporter | None:
    """Read ``ctx.deps.progress``, tolerating a deps type that has no such field."""
    return getattr(getattr(ctx, "deps", None), "progress", None)


def _default_get_user(ctx: RunContext[Any]) -> Any:
    """Read the acting user off ``ctx.deps.user`` (the ``AgentDeps`` default)."""
    return ctx.deps.user


_DEFAULT_CONVENTIONS = AgentConventions()
"""The wording a toolset given no ``conventions=`` says, and the module-level default.

Built once because it is frozen and validated at construction; private because
``AgentConventions()`` is the public way to say the same thing.
"""

_BLOCK_CONVENTIONS = (
    "base",
    "pagination",
    "ordering",
    "handles",
    "read_shaping",
    "read_shaping_on_pages",
)
"""The fields only the derived block says, so ``instructions=`` would ignore them.

The other four land somewhere an override does not reach: the heading of the
per-step unavailable list (appended after the override), a handle field's output
schema, a shape's schema and the render retry, and the missing-argument
retry. ``test_the_fixture_reaches_every_field`` and the ``_LANDS`` table beside
it say where each field lands.
"""

_INSTRUCTIONS_MEMO_SIZE = 32
"""How many combinations of omitted tools one toolset keeps a derived block for.

Every subset of the conditioned tools is a possible key, which is why there is a
bound at all; in practice a deployment moves between a handful -- the books open
or closed -- and an evicted entry costs one derivation, tens of microseconds.
"""


_ACCEPTED_UNKNOWN_ARGUMENTS = (UnknownArguments.REJECT, UnknownArguments.IGNORE)
"""The ``unknown_arguments`` policies a toolset is built with.

An allow-list rather than a check for the one refused member, because
``UnknownArguments`` is a ``str`` enum and a policy arrives as whatever a
project's settings hold: ``"passthrough"`` compares equal to the member. A
member a later django-service-specs release adds is refused too, until this
toolset is taught what to do with it. ``test_reject_and_ignore_still_build``
holds each member here, one parameter apiece.
"""


def _validate_unknown_arguments(unknown_arguments: UnknownArguments) -> None:
    """Refuse a policy this toolset cannot carry out, at construction."""
    if unknown_arguments in _ACCEPTED_UNKNOWN_ARGUMENTS:
        return
    spelled = (
        unknown_arguments.value
        if isinstance(unknown_arguments, UnknownArguments)
        else unknown_arguments
    )
    raise ImproperlyConfigured(
        f"SpecToolset: unknown_arguments={spelled!r} is not accepted. Use "
        "UnknownArguments.REJECT or UnknownArguments.IGNORE. A tool call has no "
        "channel that carries a name the spec does not declare through to the "
        "callable, and the kernel's dispatch refuses PASSTHROUGH on every call. Declare "
        "the argument instead: a reads entry or a Validator field."
    )


def _validate_conventions_beside_instructions(
    conventions: AgentConventions, instructions: str | None
) -> None:
    """Refuse a block line changed beside the override that replaces the block."""
    if instructions is None:
        return
    changed = [
        name
        for name in _BLOCK_CONVENTIONS
        if getattr(conventions, name) != getattr(_DEFAULT_CONVENTIONS, name)
    ]
    if changed:
        raise ImproperlyConfigured(
            f"conventions= changes {', '.join(changed)}, which only the derived instructions "
            "block says, and instructions= replaces that block, so the change would be "
            "ignored. Drop instructions= to keep the derived block with your wording, or "
            "change only unavailable_heading, handle_field_description, query_param_on_pages "
            "or missing_arguments beside it."
        )


def _render(template: str | None, **values: Any) -> str | None:
    """One field of ``AgentConventions`` as the model reads it, or ``None`` if dropped."""
    return None if template is None else template.format(**values)


def _listed(names: Sequence[str]) -> str:
    """Names as a ``{names}`` placeholder receives them: each in backticks, comma-joined."""
    return ", ".join(f"`{name}`" for name in names)


def _unavailable_instruction(unavailable: Mapping[str, Affordance], heading: str) -> str:
    """The heading, then one line per tool left out: its name and its ``reason``."""
    lines = [heading.format()]
    lines.extend(f"  - `{name}`: {affordance.reason}" for name, affordance in unavailable.items())
    return "\n".join(lines)


def _has_handle(projection: AudienceProjection) -> bool:
    """Whether any field on this tool's output is an opaque identifier."""
    return any(marking.audience is FieldAudience.HANDLE for marking in projection.fields.values())


def _derive_instructions(
    specs: Mapping[str, Spec],
    tool_shapes: Mapping[str, Collection[str]],
    projections: Mapping[str, AudienceProjection] | None = None,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    include_total: bool = False,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
    conventions: AgentConventions = _DEFAULT_CONVENTIONS,
) -> str | None:
    """Build the conventions block from the specs / shapes / ordering."""
    lines: list[str | None] = [_render(conventions.base)]
    if any(_is_list_selector(spec) for spec in specs.values()):
        lines.append(
            _render(
                conventions.pagination,
                page_size=page_size,
                envelope=_envelope_shape(include_total),
            )
        )
    ordering_names: list[str] = []
    for spec in specs.values():
        advertised = _spec_ordering_argument(spec, pool_seeds=pool_seeds)
        if advertised is not None and advertised not in ordering_names:
            ordering_names.append(advertised)
    if ordering_names:
        lines.append(_render(conventions.ordering, names=_listed(ordering_names)))
    if any(_has_handle(projection) for projection in (projections or {}).values()):
        lines.append(_render(conventions.handles))
    shape_names = sorted({name for names in tool_shapes.values() for name in names})
    if shape_names:
        line = _render(conventions.read_shaping, names=_listed(shape_names))
        scope = (
            _render(conventions.read_shaping_on_pages, envelope=_envelope_listed(include_total))
            if any(
                _is_list_selector(spec) and tool_shapes.get(name) for name, spec in specs.items()
            )
            else None
        )
        if line is not None and scope is not None:
            line = f"{line} {scope}"
        lines.append(line)
    said = [line for line in lines if line is not None]
    return "\n".join(said) if said else None


def _build_tool_def(
    name: str,
    spec: Spec,
    description: str | None = None,
    max_page_size: int | None = None,
    *,
    include_total: bool = False,
    projection: AudienceProjection | None = None,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
    conventions: AgentConventions = _DEFAULT_CONVENTIONS,
    server_names: frozenset[str] = frozenset(),
    shapes: frozenset[str] = frozenset(),
    unknown_arguments: UnknownArguments = UnknownArguments.REJECT,
    call_context: dict[str, Any] | None = None,
) -> ToolDefinition:
    """One tool definition: what the model may send, and what it gets back."""
    return ToolDefinition(
        name=name,
        description=description,
        parameters_json_schema=_input_schema(
            spec,
            max_page_size,
            include_total=include_total,
            pool_seeds=pool_seeds,
            conventions=conventions,
            server_names=server_names,
            shapes=shapes,
            unknown_arguments=unknown_arguments,
            call_context=call_context,
        ),
        return_schema=_return_schema(
            spec, include_total=include_total, projection=projection, conventions=conventions
        ),
        metadata={
            "annotations": {"readOnlyHint": isinstance(kernel_spec(spec), KernelSelectorSpec)}
        },
    )


def _return_schema(
    spec: Spec,
    *,
    include_total: bool = False,
    projection: AudienceProjection | None = None,
    conventions: AgentConventions = _DEFAULT_CONVENTIONS,
) -> dict[str, Any] | None:
    """The shape of what this tool returns, or ``None`` when the spec declares none."""
    fragment = _schema_fragment(spec, "output")
    paginate = _is_list_selector(spec)
    derived = spec_output_schema(
        spec,
        paginate=paginate,
        projection=projection,
        handle_description=_render(conventions.handle_field_description),
    )
    if derived is not None and paginate and include_total:
        derived = {
            **derived,
            "properties": {**derived["properties"], "total": _TOTAL_SCHEMA},
            "required": [*derived["required"], "total"],
        }
    if derived is not None and not paginate and _is_a_list(derived):
        derived = _rows_under_items(derived)
    if derived is None or fragment is None:
        return derived
    return {**derived, **fragment}


def _is_a_list(schema: dict[str, Any]) -> bool:
    """Whether the kernel describes what a spec presents as a list."""
    return schema.get("type") in ("array", ["array", "null"])


def _rows_under_items(array: dict[str, Any]) -> dict[str, Any]:
    """An unpaged list's schema, held under ``items`` in an object."""
    return {
        "type": "object",
        "properties": {"items": {**array, "type": "array"}},
        "required": ["items"],
    }


def _unpaged_rows(spec: Spec, rendered: Any) -> Any:
    """A rendered list as the object the model is handed: its rows under ``items``."""
    if rendered is None:
        schema = spec_output_schema(spec)
        if schema is None or schema.get("type") != ["array", "null"]:
            return None
        rendered = []
    if isinstance(rendered, list | tuple):
        return {"items": list(rendered)}
    return rendered


_TOTAL_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "description": "How many items match, across every page.",
}


def _spec_description(spec: Spec) -> str | None:
    """The tool description: the docstring of the compiled spec's selector / service."""
    compiled = kernel_spec(spec)
    callable_ = compiled.selector if isinstance(compiled, KernelSelectorSpec) else compiled.service
    return inspect.getdoc(callable_) if callable_ is not None else None


def _input_schema(
    spec: Spec,
    max_page_size: int | None = None,
    *,
    include_total: bool = False,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
    conventions: AgentConventions = _DEFAULT_CONVENTIONS,
    server_names: frozenset[str] = frozenset(),
    shapes: frozenset[str] = frozenset(),
    unknown_arguments: UnknownArguments = UnknownArguments.REJECT,
    call_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The tool's parameter schema: what the spec takes, with a list selector's pagination."""
    schema = _caller_schema(
        spec,
        pool_seeds=pool_seeds,
        unknown_arguments=unknown_arguments,
        call_context=call_context,
    )
    fragment = _schema_fragment(spec, "input")
    if fragment is not None:
        schema = {**schema, **fragment}
    if _is_list_selector(spec):
        schema = {
            **schema,
            "properties": {
                **_with_scope(
                    schema["properties"],
                    shapes,
                    _render(
                        conventions.query_param_on_pages, envelope=_envelope_listed(include_total)
                    ),
                ),
                "page": _PAGE_PARAM_SCHEMA,
                "limit": _limit_param_schema(max_page_size, include_total=include_total),
            },
        }
    return _without(schema, server_names)


def _with_scope(
    properties: dict[str, Any], shapes: frozenset[str], scope: str | None
) -> dict[str, Any]:
    """``properties`` with ``scope`` appended to each of ``shapes``' descriptions."""
    if scope is None:
        return properties
    scoped = dict(properties)
    for name in shapes & properties.keys():
        declared = scoped[name].get("description")
        scoped[name] = {**scoped[name], "description": f"{declared} {scope}" if declared else scope}
    return scoped


def _without(schema: dict[str, Any], names: frozenset[str]) -> dict[str, Any]:
    """``schema`` with ``names`` out of its ``properties`` and its ``required``."""
    if not names:
        return schema
    cut: dict[str, Any] = {key: value for key, value in schema.items() if key != "required"}
    cut["properties"] = {
        key: value for key, value in schema.get("properties", {}).items() if key not in names
    }
    required = [name for name in schema.get("required", []) if name not in names]
    if required:
        cut["required"] = required
    return cut


def _caller_schema(
    spec: Spec,
    *,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
    unknown_arguments: UnknownArguments = UnknownArguments.REJECT,
    call_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What ``spec`` itself asks of the model, before this toolset's own arguments."""
    return spec_caller_input_schema(
        kernel_spec(spec),
        pool_seeds=pool_seeds,
        unknown_arguments=unknown_arguments,
        call_context=call_context,
    )


def _required_arguments(
    spec: Spec,
    *,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
) -> tuple[str, ...]:
    """The reads a call to ``spec`` cannot go without, as its schema requires them."""
    compiled = kernel_spec(spec)
    reads = compiled.reads.names()
    target = compiled.target_selector_spec() if isinstance(compiled, KernelServiceSpec) else None
    if target is not None:
        reads |= target.reads.names()
    required = _caller_schema(spec, pool_seeds=pool_seeds).get("required", [])
    return tuple(name for name in required if name in reads)


def _is_list_selector(spec: Spec) -> bool:
    compiled = kernel_spec(spec)
    return isinstance(compiled, KernelSelectorSpec) and compiled.kind == SelectorKind.LIST


def _spec_ordering_argument(
    spec: Spec,
    *,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
) -> str | None:
    """The argument name a list spec advertises its sort under, or ``None``."""
    if not _is_list_selector(spec):
        return None
    properties = _caller_schema(spec, pool_seeds=pool_seeds).get("properties", {})
    filter_set = getattr(spec, "filter_set", None)
    for name, declared in getattr(filter_set, "base_filters", {}).items():
        if hasattr(declared, "get_ordering_value") and name in properties:
            return cast("str", name)
    return "ordering" if "ordering" in properties else None


def _call_spec(
    spec: Spec,
    user: Any,
    args: dict[str, Any],
    *,
    unknown_arguments: UnknownArguments = UnknownArguments.REJECT,
    server_arguments: ServerArguments | None = None,
    max_page_size: int | None = None,
    max_result_bytes: int | None = None,
    projection: AudienceProjection | None = None,
    offered: frozenset[str] = frozenset(),
    label: str,
    progress: ProgressReporter | None = None,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
    call_context: dict[str, Any] | None = None,
    translate_exception: _ExceptionTranslator,
    shape_page: _PageShaper,
    render_output: _OutputRenderer,
    enforce_result_bytes: _ResultBounder,
    conventions: AgentConventions = _DEFAULT_CONVENTIONS,
    include_total: bool = False,
) -> Any:
    """Dispatch ``spec`` through the kernel and render the result for the model."""
    server_names: frozenset[str] = frozenset()
    if server_arguments is not None:
        server_names = server_arguments.names()
        with _refusals_name_the_tool(label):
            args = _apply_server_arguments(
                spec, args, server_arguments, label=label, call_context=call_context
            )
    missing = [
        name for name in _required_arguments(spec, pool_seeds=pool_seeds) if name not in args
    ]
    if missing:
        raise _missing_arguments(missing, conventions)
    page_args = _pop_pagination(spec, args, pool_seeds=pool_seeds)
    try:
        compiled = kernel_spec(spec)
        shaped = [
            parameter.name
            for parameter in compiled.shapes
            if args.get(parameter.name) is not None and parameter.name not in server_names
        ]
        _fill_reads(compiled, args)
        declared = (
            compiled.parameters().names()
            | offered
            | compiled.parameters(call_context=call_context).names()
        )
        arguments = _call_arguments(
            args,
            declared,
            owned=kernel_server_owned_keys(
                compiled, pool_seeds=pool_seeds, call_context=call_context
            ),
        )
        result = dispatch(
            compiled,
            principal=user,
            arguments=arguments,
            grant=Grant(compiled, user) if compiled.permissions is None else None,
            pool_seeds=pool_seeds,
            unknown_arguments=unknown_arguments,
            progress=progress,
            call_context=call_context,
        )
    except BaseException as exc:
        handler = translate_exception(exc)
        if handler is not None:
            return handler(exc)
        if isinstance(exc, InvalidArguments | ServiceValidationError):
            raise ModelRetry(_format_validation_detail(exc.detail)) from exc
        if isinstance(exc, AdditionalInputRequired):
            raise ModelRetry(_missing_input_prompt(exc)) from exc
        if isinstance(exc, ServiceError):
            if isinstance(exc, ActionUnavailable):
                raise ToolFailed(_refusal_message(exc)) from exc
            raise ToolFailed(str(exc)) from exc
        raise

    if result.kind == "not_found":
        raise ToolFailed("not found")

    page: OutputPage | None = None
    if page_args is not None:
        page = shape_page(
            result.value,
            page=page_args.page,
            limit=page_args.limit,
            max_page_size=max_page_size,
        )
        result = replace(result, value=page.items)
    try:
        rendered = render_output(spec, result, projection=projection)
    except (InvalidArguments, ServiceValidationError) as exc:
        handler = translate_exception(exc)
        if handler is not None:
            return handler(exc)
        if not shaped:
            raise
        raise ModelRetry(
            _render_rejection_message(
                shaped,
                exc.detail,
                scope=(
                    _render(
                        conventions.query_param_on_pages, envelope=_envelope_listed(include_total)
                    )
                    if page is not None
                    else None
                ),
            )
        ) from exc
    rendered = page.envelope(rendered) if page is not None else _unpaged_rows(spec, rendered)
    return enforce_result_bytes(rendered, max_bytes=max_result_bytes, label=label)


def _apply_server_arguments(
    spec: Spec,
    args: dict[str, Any],
    server: ServerArguments,
    *,
    label: str,
    call_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """``args`` with ``server``'s values in place and its withheld names out."""
    dropped = sorted(server.names() & args.keys())
    if dropped:
        logger.warning(
            "Tool %r: dropped caller-supplied argument(s) %s, which tool_server_arguments "
            "keeps from the model",
            label,
            ", ".join(dropped),
        )
    return apply_server_arguments(spec, args, server, call_context=call_context)


def _enforce_result_bytes(
    payload: Any,
    *,
    max_bytes: int | None,
    label: str,
    extra: Mapping[str, Any],
) -> Any:
    """Return ``payload``, or raise a model-readable refusal when it is over budget."""
    if max_bytes is None:
        return payload
    size: int = len(json.dumps(payload, default=str).encode("utf-8"))
    if size <= max_bytes:
        return payload
    logger.warning(
        "Result bound exceeded: tool %r produced %d bytes over a %d byte ceiling",
        label,
        size,
        max_bytes,
        extra=extra,
    )
    raise ToolFailed(
        f"This result was {size} bytes, over the {max_bytes} byte ceiling. "
        "Narrow the request — add or tighten a filter, lower `limit`, or "
        "select fewer fields — and call again. The result was not "
        "truncated: a partial payload would look complete."
    )


def _missing_input_prompt(exc: AdditionalInputRequired) -> str:
    """The service's message, plus the names it wants the answer back under."""
    if not exc.schema:
        return str(exc)
    names: str = ", ".join(f"`{name}`" for name in exc.schema)
    return f"{exc} Call this tool again, additionally supplying: {names}."


def _refusal_message(exc: ActionUnavailable) -> str:
    """The sentence a refused call fails with: the reason, then the rule's code."""
    return f"{exc.message} (code: {exc.code})"


def _format_validation_detail(detail: Any) -> str:
    """A validation detail as text a model can act on."""
    return "\n".join(_validation_detail_lines(detail, path=""))


def _validation_detail_lines(detail: Any, *, path: str) -> Iterator[str]:
    """Walk a detail depth-first, yielding ``path: message`` for every leaf."""
    if isinstance(detail, Mapping):
        for key, value in detail.items():
            yield from _validation_detail_lines(value, path=_validation_detail_path(path, key))
    elif isinstance(detail, list | tuple):
        for index, value in enumerate(detail):
            nested = isinstance(value, Mapping | list | tuple)
            yield from _validation_detail_lines(
                value, path=_validation_detail_path(path, index) if nested else path
            )
    else:
        yield f"{path}: {detail}" if path else str(detail)


def _validation_detail_path(path: str, key: Any) -> str:
    """Extend ``path`` by one step: ``[i]`` for a position, ``.name`` for a field."""
    if isinstance(key, int):
        return f"{path}[{key}]"
    if key in NON_FIELD_KEYS:
        return path
    return f"{path}.{key}" if path else str(key)


def _render_rejection_message(names: Sequence[str], detail: Any, *, scope: str | None) -> str:
    """The retry for a render the caller's read-shaping values broke."""
    quoted = [f"`{name}`" for name in names]
    subject = quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} or {quoted[-1]}"
    message = (
        f"{subject} was rejected while rendering the result: {_format_validation_detail(detail)}"
    )
    if not message.endswith((".", "!", "?")):
        message += "."
    if scope is not None:
        message = f"{message} {scope}"
    return message


def _pop_pagination(
    spec: Spec,
    args: dict[str, Any],
    *,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
) -> _PageArgs | None:
    """Strip + validate the list-selector args this toolset owns."""
    if not _is_list_selector(spec):
        return None
    limit: int | None = _coerce_positive_int(args.pop("limit", None), "limit")
    if _spec_ordering_argument(spec, pool_seeds=pool_seeds) is None:
        _refuse_unadvertised_ordering(args.pop("ordering", None))
    return _PageArgs(page=_coerce_positive_int(args.pop("page", None), "page"), limit=limit)


_CALL_SEEDS = frozenset({"request", "view"})
"""The seeds each call registers itself, from the call's context."""


def _no_value() -> None:
    """A resolver standing in for a seed whose name is all a check reads."""


def _call_context(ctx: RunContext[Any], tool_name: str) -> dict[str, Any]:
    """This call's context: a copy of the run's, with the tool's name under ``action``."""
    return build_call_context(
        AGENT_TRANSPORT,
        base=getattr(getattr(ctx, "deps", None), "call_context", None),
        action=tool_name,
    )


def _with_call_seeds(
    pool_seeds: PoolSeeds, call_context: Mapping[str, Any], seeds: Mapping[str, Callable[..., Any]]
) -> PoolSeeds:
    """The mount's seeds, with this call's ``request``, ``view`` and own ``seeds`` beside them."""
    clash = sorted(pool_seeds.names & _CALL_SEEDS)
    if clash:
        raise ImproperlyConfigured(
            f"pool_seeds registers {', '.join(repr(name) for name in clash)}, which "
            "SpecToolset registers itself on every call from the call's context. Remove "
            "the registration, and put the value in the run's call_context or return it "
            "from call_seeds."
        )
    request, view = call_context["request"], call_context["view"]
    return pool_seeds.extend(**{"request": lambda: request, "view": lambda: view, **seeds})


def _fill_reads(compiled: KernelServiceSpec | KernelSelectorSpec, args: dict[str, Any]) -> None:
    """Read the call's arguments under the declared reads, as the schema advertises them."""
    declared = _declared_reads(compiled)
    for parameter in declared:
        if args.get(parameter.name) is None and not parameter.nullable:
            args.pop(parameter.name, None)
    for parameter in declared:
        default = parameter.default
        if parameter.name not in args and default is not UNSET and default is not None:
            args[parameter.name] = default


def _call_arguments(
    args: dict[str, Any],
    declared: frozenset[str],
    *,
    owned: frozenset[str],
) -> dict[str, Any]:
    """The call's arguments: the model's, less the ones the server answers."""
    arguments = {
        name: value for name, value in args.items() if not (name in declared and name in owned)
    }
    return arguments


def _missing_arguments(names: Sequence[str], conventions: AgentConventions) -> ModelRetry:
    """The retry for a call that left out arguments it cannot run without."""
    return ModelRetry(conventions.missing_arguments.format(names=_listed(sorted(names))))


def _coerce_positive_int(value: Any, name: str) -> int | None:
    """Coerce a pagination arg to a positive int; ``ModelRetry`` on anything else."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ModelRetry(f"`{name}` must be a positive integer.")
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, str) and value.strip().isdigit():
        coerced = int(value)
    else:
        raise ModelRetry(f"`{name}` must be a positive integer.")
    if coerced < 1:
        raise ModelRetry(f"`{name}` must be a positive integer.")
    return coerced


def _refuse_unadvertised_ordering(value: Any) -> None:
    """``ModelRetry`` when a model sends a sort to a tool that advertises none."""
    if value is not None:
        raise ModelRetry("This tool does not accept an `ordering` argument; omit it.")


def _shape_list(
    value: Any, *, page: int | None, limit: int | None, max_page_size: int | None
) -> OutputPage:
    """Paginate a list selector's queryset with ``paginate_output``, then force it to evaluate."""
    shaped: OutputPage = paginate_output(
        value,
        page=page,
        limit=limit,
        max_page_size=max_page_size,
    )
    return replace(shaped, items=list(shaped.items))


__all__ = ["SpecToolset"]
