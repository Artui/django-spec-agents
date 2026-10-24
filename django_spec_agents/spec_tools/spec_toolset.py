"""``SpecToolset`` — expose specs as a Pydantic-AI toolset.

A thin adapter that turns a ``name -> spec`` mapping into agent tools, executing
each call through the kernel's ``dispatch`` and describing each tool's arguments
with the kernel's ``spec_input_schema``. A spec is a django-service-specs
``ServiceSpec`` or ``SelectorSpec``, or any declaration that compiles to one;
every read goes through ``kernel_spec``, so the toolset asks the compiled spec
and nothing else. No MCP
server and no AG-UI bridge is in the path: a plain ``pydantic_ai.Agent`` calls
the specs in-process.

One call is ``_call_spec``: pop the args the transport owns (pagination), fill
each declared read the model left out with its default, build the call's
context and seeds, then ``dispatch`` -- which runs the spec's permission
checks before it calls anything -- and render. The failure-kind mapping onto the model loop lives in the
same function's arms, and is tabulated for callers in ``docs/spec-tools/quickstart.md``.

**Every failure this toolset decides is terminal is *raised*, never returned.**
Pydantic-AI marks an ordinary return ``outcome="success"``, so a refusal handed
back as a value is a failure only the model's reader can see: everything
downstream -- a transport streaming the result, a log, a client rendering the
call -- gets a successful call carrying prose. ``ToolFailed`` says the same
sentence to the model and marks the return ``outcome="failed"``, which is the
one field that makes the failure legible to anything that is not an LLM.
"""

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
    ProviderKeys,
    SelectorKind,
    ServerArguments,
    ServiceError,
    ServiceValidationError,
    SpecRegistry,
    UnknownArguments,
    apply_server_arguments,
    audience_projection_for_spec,
    base_pool,
    check_server_arguments,
    dispatch,
    kernel_spec,
    operation_affordances,
    paginate_output,
    present_for_audience,
    provider_keys,
    spec_input_schema,
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
    """Correlation fields for one tool call's log lines.

    A dispatch behind an HTTP view lands in an access log beside a request id; the
    same spec called by a model produced lines naming only the tool and the
    toolset. One chat turn does not need more than that -- there is one run --
    but the shape this package is otherwise undocumented for does: a worker
    fanning several runs out concurrently interleaved their lines with nothing
    to separate them by.

    Passed through ``extra=`` rather than formatted into the message so a
    structured handler can index the fields and a plain one stays readable. The
    four names are pydantic-ai's own, and none of them collides with a
    ``LogRecord`` attribute -- ``extra`` overwriting one of those raises.
    """
    return {
        "run_id": ctx.run_id,
        "conversation_id": ctx.conversation_id,
        "run_step": ctx.run_step,
        "tool_call_id": ctx.tool_call_id,
    }


def _usage_extra(ctx: RunContext[Any]) -> dict[str, Any]:
    """The run's usage so far, as of this tool call.

    **Cumulative for the run, not attributable to this call** -- a tool call
    spends no tokens itself. What it is good for is the shape a chatbox does not
    have: a long autonomous run where the interesting question is which tool call
    the budget was standing at when the run went wrong, and that is answerable
    only if the number is stamped on each line as it goes.

    Enforcing a budget is deliberately not done here. ``UsageLimits`` belongs to
    ``Agent.run``, which can stop the run; a toolset can only refuse the next
    tool call, which is the wrong instrument and a second place for the limit to
    live. ``ctx.usage_limits`` is readable from an
    [`enforce_result_bytes`][django_spec_agents.spec_tools.SpecToolset.enforce_result_bytes]
    override for a project that wants to taper its results as a run gets long.
    """
    usage = ctx.usage
    return {
        "run_input_tokens": usage.input_tokens,
        "run_output_tokens": usage.output_tokens,
        "run_requests": usage.requests,
        "run_tool_calls": usage.tool_calls,
    }


def _resolve_specs(specs: SpecSource) -> Mapping[str, Spec]:
    """Normalise a ``SpecSource`` to the plain mapping the internals expect, each spec compiled.

    Whatever ``kernel_spec`` compiles is a tool: a django-service-specs spec,
    which compiles to itself, or a declaration of another library's that
    compiles to one
    (``TestANativeSpecIsServed.test_whichever_way_it_arrives``). The mapping
    keeps each spec as it was handed, so ``specs`` answers with the caller's own
    objects; the kernel caches the compiled spec per object, so every later read
    asks the one compiled here.

    Compiled here, first, so a spec the kernel or its library refuses fails the
    mount naming its tool, before any check reads it: ``ImproperlyConfigured``
    for a declaration its library refuses
    (``test_a_selector_spec_with_no_selector_is_refused_when_the_toolset_is_built``),
    and ``TypeError`` for a value that compiles to no spec, the kernel's own
    refusal with the tool's name in front: an object with no ``as_kernel_spec``
    (``test_a_value_that_compiles_to_no_spec_is_refused_naming_the_tool``), or a
    declaration whose ``as_kernel_spec`` refuses to be one spec and says what to
    do instead, as a declaration of several specs names each
    (``test_a_polymorphic_spec_is_refused_naming_its_variants``).
    """
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
    """Prefix a refusal raised while building one tool with that tool.

    The kernel names what it refuses by the spec's own parts - the output,
    the Validator, the parameters - which a mapping of many specs leaves the reader to match to a
    tool by hand. The refusal is re-raised rather than
    replaced, so its own wording, and its cause, survive.
    """
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
# ``spec_input_schema``, not a Pydantic model) and the real validation is the
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
    """The rows a list tool serves to a call that names no ``limit``.

    ``paginate_output``'s own rule, stated here because the schema and the
    instructions are written at construction, long before any call: an omitted
    ``limit`` is ``DEFAULT_PAGE_SIZE``, then clamped *down* to the ceiling and
    never raised to it. So ``max_page_size=3`` serves 3 and ``max_page_size=500``
    still serves 100. Stated from ``DEFAULT_PAGE_SIZE`` alone, a toolset with a
    lower ceiling would tell the model "Defaults to 100" beside ``maximum: 3``.

    It describes the default ``shape_page``. An override that serves a different
    page size to an omitted ``limit`` has to say so in its own descriptions.
    ``test_the_limit_description_states_the_default_a_call_is_served`` holds the
    lower bound against what a call is served, and
    ``test_a_ceiling_above_the_default_page_size_leaves_the_stated_default_alone``
    holds the ``min``: replacing it with the ceiling fails that one.
    """
    if max_page_size is None:
        return DEFAULT_PAGE_SIZE
    return min(DEFAULT_PAGE_SIZE, max_page_size)


def _limit_param_schema(max_page_size: int | None) -> dict[str, Any]:
    """A list tool's ``limit`` arg: the default it is served, and its ceiling.

    The ceiling is advertised as well as clamped: a schema with no ``maximum``
    invites a request for 100 000 rows, and telling the model is cheaper than
    correcting it.
    """
    schema: dict[str, Any] = {
        "type": "integer",
        "minimum": 1,
        "description": (
            f"Maximum number of items per page. Defaults to {_served_page_size(max_page_size)}; "
            "the result reports `totalPages` and `hasNext`."
        ),
    }
    if max_page_size is not None:
        schema["maximum"] = max_page_size
    return schema


@dataclass(frozen=True)
class _PageArgs:
    """A list selector's stripped pagination tool args.

    Pagination only. Sorting is never carried here: the spec's own schema is what
    advertises a sort argument, and whatever declared it — a ``filter_set``'s
    ``OrderingFilter``, or the selector callable itself — is what applies it.
    """

    page: int | None
    limit: int | None


class SpecToolset(AbstractToolset[Any]):
    """Exposes specs as a Pydantic-AI toolset.

    A spec is anything django-service-specs' ``kernel_spec`` compiles: a
    django-service-specs ``ServiceSpec`` or ``SelectorSpec``, or a declaration
    that compiles to one. Every read of it is the
    compiled spec's. Build it from a ``name -> spec`` mapping and hand it to an
    ``Agent``:

        toolset = SpecToolset({
            "list_orders": orders_selector_spec,   # SelectorSpec -> read-only tool
            "create_order": create_order_spec,     # ServiceSpec  -> mutation tool
        })
        agent = Agent(model, deps_type=AgentDeps, toolsets=[toolset])

    Each key becomes one tool: the description is the spec's selector/service
    docstring, the parameter schema is the kernel's ``spec_input_schema`` (with a
    list selector's ``page`` / ``limit`` args merged in), the ``return_schema``
    comes from the same spec's projected output path, and the ``readOnlyHint``
    annotation is derived from the spec kind (selectors read, services mutate).

    **The parameter schema asks for what a call needs and nothing the toolset
    fills.** A selector parameter named after a pool seed or a key a typed
    ``kwargs=`` provider returns is not advertised; every other selector parameter without a default is required,
    except one the provider *may* fill, which stays advertised but optional:
    any parameter, beside a provider whose return annotation does not name a
    ``TypedDict``, and a key the provider may decline with ``UNSET``, both as
    the kernel's ``provider_keys`` reads the annotation. A name a
    ``call_seeds`` override fills is invisible here, so it has to be
    declared (see ``call_seeds``). A single-item service also advertises its
    target lookup -- the parameters of its ``collection_selector_spec``, or
    failing that its ``instance_selector_spec``, such as a ``pk`` -- beside its
    input's fields, the input's property winning a shared name, and less every
    key the kernel's ``server_owned_keys`` names, which dispatch fills itself. A
    call leaving out a required selector parameter is handed back as
    ``ModelRetry`` naming each one left out, before anything runs. A required
    input field is the Validator's to report, once the call is dispatched, so
    a call missing both hears about the field on its next turn.

    **A list selector's result is a page, always.** It comes back as
    ``{"items": [...], "page": 1, "totalPages": N, "hasNext": bool}`` — never a
    bare list — with at most
    [`DEFAULT_PAGE_SIZE`][django_service_specs.DEFAULT_PAGE_SIZE]
    rows unless ``max_page_size`` lowers it, which is what the ``page`` and
    ``limit`` arguments every list tool advertises promise. ``hasNext`` is what
    tells a model asking for a collection that more rows exist than it was
    shown.

    **Filtering needs no declaration here, and ordering belongs to the
    ``filter_set``.** A ``SelectorSpec.filter_set``'s fields are already
    generated into the tool's input schema and flow through as ordinary
    ``params``, which the kernel hands the FilterSet as ``filter_data``.
    That includes ordering: a FilterSet carrying an ``OrderingFilter`` named
    ``ordering`` advertises the argument itself — the kernel derives the
    filter's public choices into the schema as an enum — and the toolset keeps
    its hands off the value, which the FilterSet validates and applies through
    its own ``param_map``.

    **A ``many=True`` service takes its list as one named argument.** Its input
    validates as a JSON array and a model's arguments are always an object, so the
    list travels under the argument ``ServiceSpec.many_argument`` names --
    ``items`` unless the spec names another -- and nothing may be sent beside it
    but the spec's own ``reads``. An invalid item comes
    back as a ``ModelRetry`` keyed under that argument and then by the item's
    index. The result is the rendered list, advertised as an array.

    **What a tool reads off HTTP is the spec's to declare**, in its ``reads``,
    where every transport and every mount reads the same answer; the toolset
    reads them off the compiled spec, so a spec arriving in a ``SpecRegistry``
    and the same spec in a bare mapping are served alike. A read no callable
    takes is one of the compiled spec's ``shapes``: it is advertised as a tool
    argument and dispatched as one, and the kernel hands what the caller sent
    to the presenter alone, on ``PresentationContext.shapes``, never to the
    spec's callables, so a field selection is the presenter's to read, with no
    toolset awareness of how it parses one. A read a callable does take,
    by name or through ``**kwargs``, is that callable's argument: advertised as
    a plain one, and handed its declared default when the model leaves it out.
    A read standing for a URL route capture (``parent_pk``) is an argument of
    the spec, which a selector, a service or a ``kwargs=`` provider takes by
    its name. A
    ``null`` sent under a read that cannot be null is the model declining it,
    and its default answers. A value the model may not choose is not a read at
    all but ``tool_server_arguments``, which keeps it from the model rather
    than defaulting it.

    For anything the keywords below do not cover,
    [`call_seeds`][django_spec_agents.spec_tools.SpecToolset.call_seeds] and
    [`translate_exception`][django_spec_agents.spec_tools.SpecToolset.translate_exception]
    are overridable and both receive the live ``RunContext``, which is how
    per-run typed deps reach dispatch; the run's ``AgentDeps.call_context`` is
    copied into every call's context.

    Args:
        specs: The ``name -> spec`` mapping to expose, one tool per key. A
            ``SpecRegistry`` is accepted anywhere the mapping is — the shared
            declaration site for a project exposing the same specs over more than
            one transport, so the agent reads the source MCP and the HTTP views
            read. The registry and ``registry.specs()`` build the same tools:
            the toolset reads every spec's ``reads`` and its output projection
            off the compiled spec, which either carries. A filtered
            view is itself a registry, so several toolsets can be projected from
            one declaration with no shared state
            (``SpecToolset(registry.by_tag("read"), id="reads")``). Only the
            names come from it; everything else here is transport-specific, which
            the registry deliberately carries none of.
        id: Identifies this toolset, and keys a wrapping
            [`SpecCapability`][django_spec_agents.spec_tools.SpecCapability]'s
            ``defer_loading`` catalog entry — so give each projection of one
            registry its own.
        instructions: Replaces the conventions block
            [`get_instructions`][django_spec_agents.spec_tools.SpecToolset.get_instructions]
            derives from the specs. ``None`` derives it. What it does **not**
            replace is the per-step list of operations that are unavailable right
            now, which is appended after it whenever an operation condition leaves
            one out of the catalog, unless ``conventions`` sets
            ``unavailable_heading`` to ``None``, which drops that list with or
            without an override: the override replaces conventions, and which
            operations are offered on a given step is state no override written in
            advance could have described.

            Refused beside a ``conventions=`` that changes a line only the
            derived block says, because the override would ignore it without a
            word. The fields that land elsewhere -- ``unavailable_heading``,
            ``handle_field_description``, ``query_param_on_pages`` and
            ``missing_arguments`` -- apply with or without it.
        conventions: The wording the model is told, one line per field -- an
            [`AgentConventions`][django_spec_agents.spec_tools.AgentConventions].
            ``None`` is ``AgentConventions()``, the default wording. A field changes
            what its line says and never whether it appears, which the toolset
            still decides per step; ``None`` on a field drops its line. Unlike
            ``instructions=``, an override of one line leaves every other line
            derived, conditional and current.
        get_user: Reads the acting identity off the run context. Defaults to
            ``ctx.deps.user`` (the
            [`AgentDeps`][django_spec_agents.AgentDeps] shape).
        get_progress: Reads the run's ``ProgressReporter`` sink off the run
            context, for a spec that reports progress. Defaults to
            ``ctx.deps.progress``, tolerating a deps type without the field.
        unknown_arguments: What to do with a tool arg outside the spec's declared
            input set — a key the model invented. ``UnknownArguments.REJECT``
            (the default) surfaces it as a ``ModelRetry`` so the model
            self-corrects, and ``IGNORE`` drops it. Under either, the key never
            reaches the callable, which the kernel hands only what its spec
            declares. A ``filter_set``'s filters are declared names, so a key
            outside them and the selector's parameters is refused like any
            other; a selector taking a bare ``**kwargs`` declares no name, so
            every key sent to it is refused.
            ``PASSTHROUGH`` raises ``ImproperlyConfigured`` here: a tool call
            has no channel that carries an undeclared name through to the
            callable, and the kernel's dispatch refuses it on every call.
        tool_server_arguments: Values the server supplies for a tool's
            arguments, and names it keeps from the model, keyed by tool name --
            a django-service-specs
            [`ServerArguments`][django_service_specs.types.server_arguments.ServerArguments]
            per tool. Each name is one the tool's spec declares: a selector or
            target-lookup parameter, or one of its ``reads``. A name in
            ``values`` is bound: the call carries the server's value whatever
            the model sent, it satisfies a ``required`` parameter, and every
            reader of the call sees it, as one of the call's arguments.
            A ``withheld`` name is absent, so its declared default, or the
            callable's own, answers. Neither is advertised: both leave the
            tool's ``properties`` and ``required``, and the instructions name
            neither. What the model sends under one is dropped before the call
            is checked, never refused, and logged at ``WARNING`` with the tool
            and the names, never the values. Checked when the toolset is built:
            a name the tool does not expose is refused with ``ValueError``,
            and a binding ``check_server_arguments`` refuses -- an undeclared
            name, a seed or provider's name, a withheld name no call could do
            without, a bound value its parameter refuses -- with
            ``ImproperlyConfigured`` naming the tool. Static, like the tool
            definitions: build a toolset per scope, as a run scoped to one
            project builds one with that project bound.
        max_retries: Each tool's retry budget: how many times a
            ``ModelRetry`` is fed back to the model before the
            run aborts with ``UnexpectedModelBehavior``. The default matches
            pydantic-ai's own function-tool default.
        max_result_bytes: Ceiling on a rendered result, measured on the encoded
            payload because what is being protected is the model's context
            window. Over it the call **fails** with a model-readable
            ``ToolFailed`` — never truncates, because a partial payload looks
            complete.
        tool_max_result_bytes: ``max_result_bytes`` per tool. An explicit
            ``None`` opts that tool out; an absent key inherits the default.
        max_page_size: Clamps a list tool's ``limit`` *and* advertises the
            ceiling as JSON-Schema ``maximum``. It lowers the default page size
            only when it is below ``DEFAULT_PAGE_SIZE``: an omitted ``limit`` is
            served ``min(DEFAULT_PAGE_SIZE, max_page_size)``, and that is the
            default the ``limit`` description and the instructions state. A
            ceiling above it raises what a call may ask for, not what it gets by
            asking for nothing. Unset, a list tool still returns at most
            ``DEFAULT_PAGE_SIZE`` rows per page — the unbounded read is the one
            that hurts, and it is what a model produces by not thinking about
            pagination.
        thread_sensitive: Whether every dispatch shares one thread. ``True``
            (the default, and asgiref's) is what keeps Django's thread-local
            database connections coherent, and is why it is not flipped for you.

            **The cost is that concurrent tool calls serialise, process-wide.**
            asgiref's ``single_thread_executor`` is a *class* attribute, so it is
            one thread shared by every toolset instance and every concurrent run
            in the process -- not one per toolset. Pydantic-AI genuinely runs
            function tools in parallel within a segment, so four 0.30s calls
            under one model step take ~1.2s rather than ~0.3s. That is invisible
            in a chat turn calling one tool and severe in a fan-out.

            Set ``False`` only when you know the dispatched work is safe off the
            main thread -- typically because each call opens and closes its own
            connection, or you pass an ``executor`` you control.
        executor: A ``ThreadPoolExecutor`` to run dispatch on, instead of
            asgiref's shared single thread. Only consulted when
            ``thread_sensitive`` is ``False``, which is asgiref's own rule.
        dispatch_timeout: Seconds bounding one call, so the model gets an answer
            instead of a hang. It does not *stop* the work: the dispatch runs in
            a ``sync_to_async`` thread and asyncio cannot interrupt a thread
            parked in a database driver's socket read, so the query runs to
            completion regardless. Pair it with a database statement timeout.
        require_permissions: Refuse to construct a toolset containing a spec
            declaring no permissions, whose ``permissions`` compiles to
            ``None``. Over HTTP that may mean *inherit* from a view; here
            there is nothing to inherit from, so it means
            *ungated*. An empty list is a declaration that anyone may call, and
            is neither refused nor warned about. ``False``
            downgrades the refusal to an ``UnguardedSpecWarning`` while
            migrating.
        descriptions: Overrides ``spec.description`` per tool — the docstring an
            API developer reads is rarely the sentence a model needs. A tool left
            with no description anywhere gets an ``UndescribedToolWarning``.
        exception_map: Maps an exception type to a handler returning the tool's
            result (or raising ``ModelRetry``). Matched along
            the MRO, most specific first, and consulted **before** the built-in
            arms, so a project can override those too.
        pool_seeds: The project's own always-available pool seeds -- a
            [`PoolSeeds`][django_service_specs.PoolSeeds]
            registry of names such as a tenant, a locale or a clock, which over
            HTTP hang off ``request`` and off it have no channel. Handed to every
            dispatch this toolset makes, so a service, a selector or an
            affordance's condition declaring one receives it as it would from
            the kernel's ``dispatch(pool_seeds=)`` called directly; and to the
            per-step check that leaves out an operation whose condition is
            unmet, so the catalog and the instructions are decided against the
            same seeds the call is refused with. A registered name is also
            reserved: the model cannot supply it where the seed fills a
            parameter, so an argument of that name is dropped where the spec
            declares such a parameter and refused as unknown under ``REJECT``
            where the spec declares the name nowhere, no tool's input schema
            advertises it, and a read the spec declares under it, in any seat,
            is refused here with ``ImproperlyConfigured``. A Validator field
            of the name is the one exception, and the model's to send:
            it is validated into ``data``, while the service's parameter of the
            name still receives the seed's value. Toolset-wide, with no
            per-tool or per-call form: a seed is ambient to the deployment, and
            what varies per call belongs in its resolver, which declares
            ``user`` / ``request`` to receive them.

    Raises:
        ImproperlyConfigured: ``unknown_arguments`` is neither ``REJECT`` nor
            ``IGNORE``, a spec declares no permissions and
            ``require_permissions`` is set, a spec reads a registered pool seed's
            name, a selector takes a parameter that a list tool's ``page`` /
            ``limit`` takes out of the call before it runs (a spec's own
            read of an input's name is not one: it is where the input comes
            from, and is served as the input), a shape takes the
            name ``page``, ``limit`` or ``ordering``,
            ``tool_server_arguments`` names something django-service-specs'
            ``check_server_arguments`` refuses for the tool's spec,
            ``instructions=`` is given beside a
            ``conventions=`` that changes a line of the block it replaces, a
            spec's Validator names a required field no call can send, or the
            kernel or a spec's own library refuses a spec while the toolset
            compiles it, reads its input schema or projects its output -- a
            malformed ``metadata["json_schema"]`` fragment, or two output
            fields marked ``FieldMarking.label()``. The refusing message is
            prefixed ``SpecToolset tool '<name>': ``.
        TypeError: A value in ``specs`` is not one ``kernel_spec`` compiles
            -- a registry's entry rather than its spec, say, or a declaration
            of several specs, whose variants each take their own name. The
            kernel's message, prefixed with the tool's name.
        ValueError: A tool name is outside ``^[a-zA-Z0-9_-]{1,64}$``, or a
            per-tool mapping, ``tool_server_arguments`` included, names a tool
            this toolset does not expose.
    """

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
        dispatch_timeout: float | None = None,
        require_permissions: bool = True,
        descriptions: Mapping[str, str] | None = None,
        exception_map: Mapping[type[BaseException], ExceptionHandler] | None = None,
        pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
        thread_sensitive: bool = True,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        # First, because it reads nothing but the argument: a refused policy is
        # wrong whatever the specs are.
        _validate_unknown_arguments(unknown_arguments)
        resolved = _resolve_specs(specs)
        _validate_tool_names(resolved)
        _validate_permissions(resolved, require=require_permissions)
        # Checked once, here, with the seeds every call is dispatched with, so a
        # binding that cannot mean what it says fails the mount naming its tool.
        # A copy, so a mapping changed after this line binds nothing unchecked.
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
        self._dispatch_timeout = dispatch_timeout
        self._thread_sensitive = thread_sensitive
        self._executor = executor
        overrides: Mapping[str, int | None] = tool_max_result_bytes or {}
        for tool_name in overrides:
            _refuse_an_unknown_tool("tool_max_result_bytes", tool_name, self._specs)
        self._tool_max_result_bytes: dict[str, int | None] = {
            # ``.get`` with the toolset default, so a stored ``None`` wins over
            # it: an explicit "no ceiling here" is not an absent key.
            name: overrides.get(name, max_result_bytes)
            for name in self._specs
        }
        # Which of each spec's arguments shape its output, as the kernel classes
        # them: a read no callable takes is one of the compiled spec's
        # ``shapes``, and only a shape is told it applies to each item or named
        # as read-shaping in the instructions. A read a ``**kwargs`` selector or
        # a ``kwargs=`` provider takes is that callable's argument, advertised
        # as a plain one
        # (``test_a_read_a_kwargs_selector_takes_is_its_argument_with_its_default``).
        # Read off the compiled spec, so a tool built from a bare mapping is
        # told about its shapes as one built from a registry is
        # (``test_read_shaping_line_lists_declared_shapes_sorted[mapping]``).
        self._tool_shapes: dict[str, frozenset[str]] = {
            name: kernel_spec(spec).shapes.names() for name, spec in self._specs.items()
        }
        # The transport's own reserved names and this toolset's registered pool
        # seeds, neither of which the spec knows when it is built.
        for name, spec in self._specs.items():
            _validate_declared_reads(name, spec, seeds=pool_seeds)
        _validate_inputs_pagination_takes(self._specs)
        # Agent markings are pure in the presenter's output declaration, like
        # the schemas below, so they are resolved once rather than walking the
        # output on every tool call.
        #
        # **Built before the tool definitions, which read them.** A tool's
        # ``return_schema`` describes the payload the model actually receives,
        # and that payload is projected — so the schema has to be projected by
        # the same declaration, or it would advertise a field the render drops.
        #
        # The output's own refusal - two fields marked as the label - surfaces
        # here rather than at compile, so it is prefixed with the tool the same
        # way. Held by
        # ``test_a_field_audiences_clash_is_refused_when_the_toolset_is_built``.
        #
        # The markings are the compiled spec's, the ``FieldMarking``s on its
        # presenter's output, so the same spec projects the same fields whether
        # the toolset was built from a registry or from a bare mapping
        # (``test_field_audiences_reach_the_projection``).
        self._projections: dict[str, AudienceProjection] = {}
        for name, spec in self._specs.items():
            with _refusals_name_the_tool(name):
                self._projections[name] = audience_projection_for_spec(spec)
        # Schemas derive purely from the specs (no DB), so the tool defs are built
        # once up front. ``ToolDefinition`` defaults to ``kind="function"`` — the
        # in-process kind the run loop routes into ``call_tool``.
        self._tool_defs: dict[str, ToolDefinition] = {
            name: _build_tool_def(
                name,
                spec,
                self._descriptions.get(name),
                self._max_page_size,
                shapes=self._tool_shapes[name],
                projection=self._projections[name],
                pool_seeds=pool_seeds,
                conventions=self._conventions,
                server_names=self._server_names(name),
                unknown_arguments=unknown_arguments,
            )
            for name, spec in self._specs.items()
        }
        # The tools whose availability has to be *asked* each step: those with an
        # affordance answered without a row. Static, like everything above, so
        # the question "does anything here need asking?" costs a step nothing,
        # and a toolset where the answer is no never builds a pool or hops.
        #
        # The kernel's own selection rather than a restatement of it: which
        # conditions are callables and which are row expressions is one line it
        # draws in one place, and a second copy here could only drift from it.
        self._conditioned: tuple[str, ...] = tuple(
            name for name, spec in self._specs.items() if operation_affordances(spec)
        )
        # Per instance, so the cache dies with the toolset rather than a
        # module-level cache keeping every toolset it has seen alive. Keyed by
        # which tools are left out and nothing else: the block is a pure
        # function of that, so an entry holds no user's answer and serves any
        # run that arrives at the same combination. Bounded because the key
        # space is every subset of ``_conditioned``, however few of them a real
        # deployment visits.
        self._instructions_without: Callable[[frozenset[str]], str | None] = lru_cache(
            maxsize=_INSTRUCTIONS_MEMO_SIZE
        )(self._derive_instructions_without)

    @property
    def id(self) -> str | None:
        return self._id

    @property
    def specs(self) -> Mapping[str, Spec]:
        """The resolved ``name -> spec`` mapping this toolset exposes.

        The synchronous answer to "what tools are these?", for a caller composing
        this toolset at configuration time — a name-dedup pass, a tool catalog —
        with no run in sight, since ``get_tools`` is ``async`` and needs a
        ``RunContext``. Read-only (a ``MappingProxyType``), so enumerating it
        cannot add a tool that skipped the constructor's permission and
        description checks.
        """
        return MappingProxyType(self._specs)

    def _server_names(self, name: str) -> frozenset[str]:
        """The names ``tool_server_arguments`` answers for on tool ``name``: none, unbound."""
        server = self._tool_server_arguments.get(name)
        return frozenset() if server is None else server.names()

    def _offered_shapes(self, name: str) -> frozenset[str]:
        """The names of tool ``name``'s read-shaping arguments the model is offered.

        Only the spec's shapes: a read a callable takes is advertised as a plain
        argument, so the instructions do not call it read-shaping
        (``test_a_read_a_kwargs_selector_takes_is_its_argument_with_its_default``).
        Less every name the server answers for, which the tool's schema does
        not advertise either, so the instructions never name an argument the
        model was not given (``test_a_bound_shape_is_not_offered_in_the_instructions``).
        """
        return self._tool_shapes[name] - self._server_names(name)

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        """The tool catalog this run is offered, one entry per listed spec.

        **The catalog is not permission-filtered, by design.** Every tool is
        advertised to every run and the spec's ``permissions`` gate the *call*: a
        denied tool is one the model can see and cannot use. Filtering by
        default would be worse in three ways — a permission whose answer depends
        on the arguments has none to read at listing time and would deny a tool
        the caller can in fact invoke; ``get_tools`` runs once per model step, so
        a DB-backed check becomes a query per spec per step; and a tool the model
        never sees is one it cannot ask about, which is how a run ends in a
        guess instead of a denial. Nothing row-level is exposed either way — a
        listing carries a name, a description and an input schema.

        **An operation whose condition is unmet right now is left out, and that is
        not the same decision.** A spec's ``Affordance`` answered without a row --
        a callable ``when``, "the books are open" -- is asked each step, and a
        tool it fails is omitted until it holds again. Each of the three
        arguments above has an answer here. Such a condition reads only the
        seeds -- the user, the request, a registered seed -- and never an
        argument, so it cannot hide a tool the caller could in fact invoke:
        whatever the model would send, the call would be refused. Only the specs
        declaring one are asked, so a toolset that declares none pays nothing,
        not even a thread hop. And the model can still ask about what it cannot
        see, because
        [`get_instructions`][django_spec_agents.spec_tools.SpecToolset.get_instructions]
        names every tool left out this way together with its reason, which is
        what it needs to tell the user why rather than guess.

        A condition on the row (an ORM expression) is never asked here: there is
        no row at listing time, and it goes on being answered per object at the
        call. A condition that raises aborts the step, as it would the call --
        one that cannot be answered is not a ``no``.

        **The omission is applied beside ``is_tool_listed``, not inside it**, so
        an override of that seam -- which is free to return ``True`` -- cannot
        switch it off by not calling ``super()``. Neither is an authorization
        decision: the call enforces every affordance whatever this listed, and a
        model calling a name left out gets pydantic-ai's unknown-tool retry.

        This and ``get_instructions`` each ask the conditions for themselves,
        once per step apiece, rather than sharing one answer. The run context
        carries no key that could safely scope a shared answer to one run and one
        step: ``run_id`` may be supplied by the caller and is reused when a
        failed run is retried under it, and a context built by hand has none.
        A memo keyed on it could hand one run's -- one user's -- answer to
        another, where asking twice costs a second evaluation of conditions that
        read only seeds. The price is that a condition flipping between the two
        reads can leave one step's catalog and its instructions disagreeing
        about one tool; the call's own enforcement is authoritative either way.

        Override [`is_tool_listed`][django_spec_agents.spec_tools.SpecToolset.is_tool_listed]
        when a deployment does want a narrower catalog.
        """
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
        """Whether ``name`` belongs in this run's catalog. ``True`` by default.

        The seam for a deployment that wants a per-run catalog — hiding a
        staff-only tool from a non-staff run, or scoping the catalog to a
        tenant read off ``ctx.deps``. Hiding a tool is a *disclosure* decision,
        never an authorization one: the call is gated by
        ``spec.permissions`` whatever this returns, so an override that
        wrongly returns ``True`` grants nothing.

        Not consulted for a tool an unmet operation condition already left out
        of this step's catalog, and returning ``True`` does not put one back:
        that omission is applied by ``get_tools`` itself, so an override need
        not call ``super()`` to keep it.

        ``async`` because ``get_tools`` is, and it is called once per tool per
        model step. An override that queries the database must wrap that work in
        ``asgiref.sync.sync_to_async`` — Django refuses ORM access on the event
        loop, exactly as ``call_tool`` has to for dispatch.
        """
        del name, ctx  # unused by the default; present so an override has both
        return True

    async def get_instructions(self, ctx: RunContext[Any]) -> str | None:
        """Teach the model this toolset's conventions.

        The per-tool descriptions and parameter schemas say what each tool *is*,
        but not how the family behaves: that list tools accept ``page`` /
        ``limit`` / ``ordering``, that a business failure comes back as a readable
        failed result (a final answer, not a reason to retry) while a
        bad argument comes back as a retry request, and that a permission error
        is final. Pydantic-AI appends the block to the system prompt each turn,
        for a toolset attached directly *or* wrapped by a capability.

        **Per step, when an operation condition leaves a tool out.** The block is
        then derived from the tools this step offers, so no line advises about
        one it does not, and it ends by naming each tool left out with its
        ``reason``: the model sees neither the tool nor, otherwise, any sign it
        exists, and a user asking for it would get a guess where a sentence was
        available. The conditions are asked here and in ``get_tools``
        separately -- see that method for why, and for what it costs. With an
        ``instructions`` override, the override stands in for the derived block
        and the unavailable tools are still appended after it, because an
        override replaces the conventions and cannot have described which
        operations a given step would lack. ``unavailable_heading=None`` in the
        ``conventions`` drops that list either way, override or not.

        A toolset declaring no such condition is untouched by any of this: it
        asks nothing, and returns the same string every step.

        Each line is worded by the toolset's ``conventions``, which change what a
        line says and never whether it appears.

        Returns:
            The ``instructions`` override when one was given, else a block
            derived from the specs — each line conditional on something in this
            toolset being able to act on it, so the prompt carries no advice that
            cannot fire — followed in either case by the operations unavailable
            this step, when there are any and ``unavailable_heading`` is not
            ``None``. ``None`` when ``conventions`` dropped
            every line that would have been said and nothing is unavailable.
        """
        unavailable = await self._unavailable_operations(ctx)
        block: str | None
        if self._instructions_override is not None:
            block = self._instructions_override
        elif unavailable:
            block = self._instructions_without(frozenset(unavailable))
        else:
            block = self._derived_instructions
        heading = self._conventions.unavailable_heading
        # One arc for two reasons to say nothing more. ``not unavailable`` is held
        # by every toolset declaring no condition; ``heading is None`` by
        # ``test_none_drops_a_block_line_and_nothing_else[unavailable_heading]``,
        # where a tool *is* left out.
        if not unavailable or heading is None:
            return block
        listed = _unavailable_instruction(unavailable, heading)
        # A block with every line dropped is ``None``, and the list then stands
        # alone (``test_with_every_line_dropped_the_unavailable_list_stands_alone``).
        return listed if block is None else f"{block}\n{listed}"

    @cached_property
    def _derived_instructions(self) -> str | None:
        """The conventions block, built once and kept.

        A pure function of six attributes ``__init__`` assigns and nothing
        reassigns -- the specs, the query-param declarations, the projections,
        the schema registry, the page ceiling and the conventions, which are
        frozen -- so recomputing it is recomputing the same string. Pydantic-AI
        asks for instructions on **every model step**, and the derivation walks
        each list spec through ``spec_to_json_schema`` to find what that spec
        calls its sort.

        This is tidiness, not a fix: the measured cost is tens of microseconds
        per spec, against model round trips measured in seconds. What earns the
        memo is that a step should not pay for an answer that was settled at
        construction.

        ``cached_property`` rather than computing it in ``__init__`` so a toolset
        given an ``instructions=`` override never derives the block it replaces.
        The consequence is the ordinary one for a memo: a subclass that mutates
        ``_specs`` after the first ``get_instructions`` is describing tools the
        block will not mention. Nothing in this package mutates it, and the
        public ``specs`` property is read-only precisely so nothing outside can.

        Derived by the method the per-step memo wraps, called directly so the
        full set is kept here rather than in that memo.
        """
        return self._derive_instructions_without(frozenset())

    def _derive_instructions_without(self, omitted: frozenset[str]) -> str | None:
        """The conventions block for the tools a step offers, ``omitted`` aside.

        Reached through ``_instructions_without``, the per-instance memo
        ``__init__`` wraps it in, when some tool *is* omitted, and directly by
        ``_derived_instructions`` for the full set, cached for the toolset's life.
        The lines naming what was omitted are not part of it: they carry each
        tool's ``reason``, and rendering them per step keeps this memo a
        function of which tools are offered and nothing else.
        """
        offered = {name: spec for name, spec in self._specs.items() if name not in omitted}
        return _derive_instructions(
            offered,
            {name: self._offered_shapes(name) for name in offered},
            {name: self._projections[name] for name in offered},
            page_size=_served_page_size(self._max_page_size),
            pool_seeds=self._pool_seeds,
            conventions=self._conventions,
        )

    async def _unavailable_operations(self, ctx: RunContext[Any]) -> dict[str, Affordance]:
        """Each tool an operation condition refuses right now, with that condition.

        Returns before building anything when no spec declares such a
        condition, which is the ordinary toolset: no pool, no request, no thread
        hop. Otherwise one hop answers every conditioned spec, rather than one
        hop apiece -- ``aunmet_operation_affordance`` would take one per spec,
        and see ``_answer_operation_conditions`` for the reason this cannot use
        it anyway.

        The user is read here, on the loop, exactly where ``call_tool`` reads
        it, so an extractor that is only safe off the database is treated alike.
        """
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
        """Ask every conditioned spec, once, against the pool its call would see.

        **Run in the dispatch thread and released like a dispatch.** A condition
        is user code and may query, so it cannot run on the event loop; and a
        query here opens a connection on the thread it lands on, which off HTTP
        nothing closes -- the leak ``_call_spec_releasing_connections`` exists
        for. The kernel's ``aunmet_operation_affordance`` hops through
        ``sync_to_async`` on its own and leaves that connection open, and the
        leak it leaves would not stay its own: a connection already open on the
        shared thread is in every later dispatch's ``held_before``, so each of
        them would then leave it alone too. It also ignores this toolset's
        ``thread_sensitive`` and ``executor``, which a condition should honour
        as the call it stands in front of does. So the hop is this toolset's and
        the evaluation inside it is the kernel's synchronous
        ``unmet_operation_affordance``.

        **Each pool is built the way that tool's call builds its own**, per
        conditioned tool: the call's context for the tool's name, the run's
        context copied with the name under ``action``
        (``test_a_listing_hands_each_condition_its_own_tool_s_name_as_the_action``),
        and the call's seeds, the mount's ``pool_seeds`` with the context's
        ``request`` and ``view`` and what
        [`call_seeds`][django_spec_agents.spec_tools.SpecToolset.call_seeds]
        returns for the tool
        (``test_a_listing_asks_each_condition_with_its_own_tool_s_seeds``). The
        seeds are handed over twice, as the kernel asks: resolved into the pool
        by ``base_pool(seeds=)``, and admitted into what the condition is shown
        by ``unmet_operation_affordance(reserved=)``, which hands a condition
        only the reserved names. Drop either and a condition reading a
        registered seed fails to bind here while the call, given the same seeds
        by ``dispatch(pool_seeds=)``, answers it -- both halves are held by
        ``test_a_condition_reading_a_registered_seed_is_asked_with_it``. So the
        condition sees the names ``dispatch`` would give it.
        """
        with _releasing_connections_opened_here():
            unavailable: dict[str, Affordance] = {}
            for name in self._conditioned:
                call_context = _call_context(ctx, name)
                seeds = _with_call_seeds(
                    self._pool_seeds, call_context, self.call_seeds(ctx=ctx, tool_name=name)
                )
                unmet = unmet_operation_affordance(
                    self._specs[name],
                    base_pool(user=user, seeds=seeds),
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
            # The whole pipeline touches the ORM, which Django forbids on the
            # async event loop — run it in a thread. ``dict(tool_args)`` is a
            # private copy, so popping the transport's args never mutates the
            # caller's dict.
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
                    # The tool's name: the call's ``action``, the same identity
                    # the MCP transport reports for the same spec.
                    label=name,
                    progress=self._get_progress(ctx),
                ),
                self._dispatch_timeout,
                label=name,
                extra=_run_extra(ctx),
            )
        except (NotPermitted, PrincipalUnavailable, PermissionDenied):
            # One arc for three refusals, each held by a test raising it bare:
            # ``test_a_refusal_the_service_raises_is_logged_as_a_denial``,
            # ``test_a_deactivated_principal_is_logged_as_a_denial`` for the
            # kernel's refusal of a deactivated principal, which it raises
            # before any permission check, and
            # ``test_djangos_permission_denied_is_logged_as_a_denial`` for
            # Django's own class, which a service may raise bare.
            # The one failure with no other trace: a ``ModelRetry`` reaches the
            # model and a ``ToolFailed`` reaches the answer as a failed result,
            # but a denial aborts the run and is absorbed by whatever drives it.
            # Logged at the boundary, then re-raised untouched.
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

    # The front half of a call: what it carries beside its arguments. Deliberately
    # not offered beside this: a request or a view the toolset builds, which
    # would make one transport's objects the default posture for every spec.
    # What a call carries is the run's context and the seeds below, and an
    # override keeps the honest path the easy one -- which is why the back half
    # below is three more overrides rather than three more constructor knobs.

    def call_seeds(
        self, *, ctx: RunContext[Any], tool_name: str
    ) -> Mapping[str, Callable[..., Any]]:
        """The seeds one call registers beside the mount's ``pool_seeds``, by name.

        Each value is a resolver, as for
        [`PoolSeeds`][django_service_specs.PoolSeeds]' ``extend``: called once
        per call, with whatever it declares of the kernel's base pool
        (``user``, ``progress``). The default returns none. Override it for a
        value that depends on the run or on the tool being called, which
        ``pool_seeds=``, toolset-wide, cannot carry: a tenant read off
        ``ctx.deps``, or the role a permission check reads for this operation.

        **Called in the dispatch thread, before the call's arguments are
        checked**, so a name a seed fills is never asked of the model
        (``test_a_seed_the_hook_returns_reaches_the_call_and_is_not_asked_of_the_model``),
        and once per conditioned tool when the catalog is listed, so a condition
        reading a seed it returns is asked with the tool's own
        (``test_a_listing_asks_each_condition_with_its_own_tool_s_seeds``).

        A name the mount's ``pool_seeds`` registers already is refused with
        ``ValueError`` at the call, as ``PoolSeeds.extend`` refuses one
        registered twice
        (``test_a_hook_seed_the_mount_already_registers_is_refused``). Returning
        ``request`` or ``view`` replaces the one the call carries, which is
        otherwise the run's ``call_context`` entry of that name, ``None`` where
        the run put none: a ``view`` returned here is what a permission check
        or a presenter declaring ``view`` reads.

        **A name a seed fills has to be declared.** A selector parameter only a
        seed fills is, to the tool's schema, a parameter without a default like
        any other, so it is advertised as required. Leave it out of the
        spec's reads, as a library compiling a spec does for a parameter it
        marks as no client input: the name is left out of the schema, so the
        model is never asked for it, and the seed fills it. Under the default
        ``unknown_arguments`` a call that
        sends it anyway is handed back as an unexpected argument
        (``test_a_marked_name_sent_to_an_open_selector_is_refused``).
        ``test_a_name_a_call_seeds_override_fills_is_kept_from_the_model`` holds
        the marker, and
        ``test_a_name_a_call_seeds_override_fills_is_declared_to_the_toolset``
        a seed the mount registers instead.
        """
        del ctx, tool_name  # unused by the default; present so an override has them
        return {}

    def translate_exception(
        self, exc: BaseException, *, ctx: RunContext[Any]
    ) -> ExceptionHandler | None:
        """Return a handler for ``exc``, or ``None`` to leave it to the defaults.

        The default consults ``exception_map`` by walking the exception's MRO,
        so a handler registered for a base class catches its subclasses and the
        **most specific** registration wins. Override for a decision the map
        cannot express — one that has to read the run's deps.

        This runs ahead of every built-in arm, so it is also where a program reads
        a refusal's code: an ``ActionUnavailable`` arrives here with ``.code``
        intact, before the default renders it into the sentence
        ``<reason> (code: <code>)``. A handler returned for it replaces that
        sentence along with everything else the default would have done.
        """
        del ctx  # unused by the default; present so an override has it
        for klass in type(exc).__mro__:
            handler = self._exception_map.get(cast(type[BaseException], klass))
            if handler is not None:
                return handler
        return None

    # The back half of a call: everything between the dispatch returning and the
    # tool result going back to the model. Each step is its own override, so a
    # project changing one need not replace ``call_tool`` wholesale.

    def shape_page(
        self,
        rows: Any,
        *,
        ctx: RunContext[Any],
        page: int | None,
        limit: int | None,
        max_page_size: int | None,
    ) -> OutputPage:
        """Slice a list selector's rows into the page this call serves.

        Called only for list selectors -- the specs that advertise pagination
        arguments and return something sliceable. The default is the kernel's
        shared ``paginate_output`` (which counts the rows and clamps both bounds)
        followed by forcing evaluation, so nothing downstream holds a lazy
        queryset.

        **No sorting happens here, and an override should not add any.** Ordering
        belongs to whatever the spec declared it on, which has already applied it
        to the queryset by the time this runs; an ``order_by`` here would replace
        that sort rather than compose with it.
        """
        del ctx  # unused by the default; present so an override has it
        return _shape_list(rows, page=page, limit=limit, max_page_size=max_page_size)

    def render_output(
        self,
        spec: Spec,
        result: DispatchResult,
        *,
        ctx: RunContext[Any],
        projection: AudienceProjection | None,
    ) -> Any:
        """Turn a dispatch result into the payload the model reads.

        ``result`` is the kernel's
        [`DispatchResult`][django_service_specs.DispatchResult]: for a paged
        tool, with the page's rows as its ``value``, and the envelope is added
        after this returns. The default is the kernel's
        ``present_for_audience``: the spec's presenter, handed each value with
        the call's ``PresentationContext`` -- its seeds, its context and the
        read-shaping arguments the caller sent, on ``shapes`` -- then the
        output's audience markings, using the projection this toolset resolved
        once at registration. ``result.instance`` (the pre-mutation target),
        ``result.data`` (the validated input) and ``result.service_result``
        (the flags an upsert's ``created`` rides on) are here for an override
        that wants them.

        **This is the opt-out of projecting**, and the only one. Passing
        ``projection=None`` is not it -- ``present_for_audience`` reads ``None``
        as "derive one from the spec", so the payload is projected anyway. An
        override that wants the unprojected payload calls the kernel's
        ``present(spec, result)`` directly.

        Two cases want that. A **chaining pipeline** feeding one spec's output
        into the next needs the handles the next step reads by, and the kernel
        says so in ``present_for_audience``'s own docstring: project them away
        and the next step has nothing to key on. And an output field declaring
        ``choices`` whose display differs from its value is projected
        **without any marking being declared** -- ``choice_labels`` is derived
        from the field itself -- so "we marked nothing, so nothing is
        projected" is not true, and an override is how a project that means it
        says so.
        """
        del ctx  # unused by the default; present so an override has it
        return present_for_audience(spec, result, projection=projection)

    def enforce_result_bytes(
        self, payload: Any, *, ctx: RunContext[Any], max_bytes: int | None, label: str
    ) -> Any:
        """Return ``payload``, or raise a model-readable refusal when it is over budget.

        Measured on the envelope, because the envelope is what is sent. Override to
        bound on something other than serialized length -- a row count, a per-run
        running total read off ``ctx.usage``, a taper against ``ctx.usage_limits``
        as a run gets long -- or to shape the refusal differently.

        **The default raises ``ToolFailed``**; an override is free to return a
        value instead, and that value becomes the tool's result marked
        ``outcome="success"``. An override delegating to ``super()`` sees the
        refusal as an exception, not as a returned value.

        ``ctx`` is not merely passed through to an override here: the default
        reads this run's correlation fields off it and stamps them on the
        ``WARNING`` a fired bound emits, which is the one line saying why a
        result the model expected came back as a refusal.
        """
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
        """Build the call's context and seeds, bind the seams to this run, then run the pipeline.

        In the dispatch thread, so ``call_seeds`` runs where the call does, and
        before the module-level pipeline checks the arguments against the
        seeds. ``label`` is the tool's name, the call's ``action``. The
        toolset's ``conventions`` are bound here too, beside the seams, so
        every route into a call words its retries the same way.
        """
        call_context = _call_context(ctx, label)
        return _call_spec(
            spec,
            user,
            args,
            label=label,
            pool_seeds=_with_call_seeds(
                self._pool_seeds, call_context, self.call_seeds(ctx=ctx, tool_name=label)
            ),
            call_context=call_context,
            translate_exception=lambda exc: self.translate_exception(exc, ctx=ctx),
            shape_page=lambda *a, **kwargs: self.shape_page(*a, ctx=ctx, **kwargs),
            render_output=lambda *a, **kwargs: self.render_output(*a, ctx=ctx, **kwargs),
            enforce_result_bytes=lambda *a, **kwargs: self.enforce_result_bytes(
                *a, ctx=ctx, **kwargs
            ),
            conventions=self._conventions,
            **kw,
        )

    def _call_spec_releasing_connections(
        self, spec: Spec, user: Any, args: dict[str, Any], *, ctx: RunContext[Any], **kw: Any
    ) -> Any:
        """``_call_spec``, plus the connection cleanup the thread hop owes.

        Django opens a database connection **per thread** and closes one per
        *request* — the ``request_finished`` receiver, ``close_old_connections``.
        Off HTTP there is no request, and on the default
        ``thread_sensitive=True`` the dispatch runs on asgiref's process-wide
        ``single_thread_executor``, so the connection it opens there outlives
        the call, the toolset and the agent, with nothing that will ever close
        it. A project cannot fix this from outside, because
        ``django.db.connections`` is thread-local: only asgiref's thread can
        close asgiref's connection. The symptom lands somewhere else
        entirely — a test session that will not drop its database because it "is
        being accessed by other users", naming neither this package nor the
        thread holding the handle.

        **Only what this call opened is released**, which is what makes the
        cleanup safe on every thread it can land on rather than only the one it
        was written for. ``sync_to_async`` runs on the *caller's* thread whenever
        there is a synchronous frame above the loop (asgiref's
        ``current_thread_executor``) — a WSGI request driving an agent through
        ``async_to_sync`` — and that thread's connection is the request's, quite
        possibly mid-``atomic``. It was open before this call, so it is not ours
        and is not touched. An unconditional close here is the obvious fix and it
        is wrong: it severs the caller's own transaction.

        The bookkeeping stays true across calls rather than decaying: a dispatch
        that opened a connection closes it, so the next one on the same shared
        thread finds the thread as it was and owns what it opens in turn. A
        connection someone *else* opened on that thread — a consumer's own
        ``sync_to_async`` ORM work — stays theirs forever.

        ``close()`` rather than Django's ``close_if_unusable_or_obsolete()``,
        which is what ``close_old_connections`` calls: that one honours
        ``CONN_MAX_AGE``, a budget for reusing a connection *across requests*.
        There are no requests on this thread, so a connection held back for the
        next one is held forever — and the alias would then read as "not ours"
        on the following dispatch, quietly restoring the leak at any
        ``CONN_MAX_AGE`` above zero.

        Every clause above is held by a named test, because a 100% branch gate
        cannot see the difference between them: mutating each one out in turn
        says that ``test_a_dispatch_closes_the_connection_it_opened_on_the_shared_thread``
        holds the cleanup itself, its ``persistent-connections`` parameter alone
        holds ``close()`` over ``close_if_unusable_or_obsolete()``, and
        ``test_a_dispatch_leaves_a_connection_it_did_not_open_alone`` is the only
        thing standing between this and the unconditional version.

        The bookkeeping itself is ``_releasing_connections_opened_here``, shared
        with the listing-time evaluation of operation conditions, which makes the
        same hop and owes the same cleanup.
        """
        with _releasing_connections_opened_here():
            return self._call_spec(spec, user, args, ctx=ctx, **kw)


@contextmanager
def _releasing_connections_opened_here() -> Generator[None]:
    """Close, on the way out, each connection this thread opened inside the block.

    The cleanup ``SpecToolset._call_spec_releasing_connections`` documents,
    clause by clause. It is a helper rather than that method's body
    because two hops owe it -- a dispatch, and the listing-time evaluation of
    operation conditions -- and the second one leaking would disarm the first.
    """
    # ``initialized_only`` so asking the question does not itself build a
    # wrapper for every configured alias on this thread.
    held_before = {
        conn.alias for conn in connections.all(initialized_only=True) if conn.connection is not None
    }
    try:
        yield
    finally:
        for conn in connections.all(initialized_only=True):
            # No ``conn.connection is not None`` here: Django's ``close()``
            # returns immediately on a wrapper that never connected, so the
            # extra conjunct would change nothing and no test could hold it.
            if conn.alias not in held_before:
                conn.close()


def _validate_permissions(specs: Mapping[str, Spec], *, require: bool) -> None:
    """Refuse — or warn about — specs with nothing gating them off HTTP.

    **A spec declaring no permissions may be relying on a view to supply
    them, and off HTTP there is no view.** Over HTTP that can be a correct,
    working configuration, where the view's own permissions and the
    project's defaults apply. A toolset has neither, so a spec
    properly guarded behind a viewset, with passing HTTP tests, becomes callable
    by whatever the agent decides to call the moment it is handed to a model,
    with no signal anywhere.

    Read off the compiled spec, whose ``permissions`` is ``None`` exactly there,
    for every source. A registry refuses such a spec when it is registered, so
    only a mapping can hand one in. A spec declaring every principal welcome,
    the kernel's ``Unrestricted``,
    has said so, and is not refused: that is a decision, where ``None`` is an
    absence (``test_an_explicitly_unrestricted_spec_is_neither_refused_nor_warned_about``).
    The kernel's own ``unguarded_specs`` reports those, the other question.
    ``is None`` and a falsy check agree on every compiled spec, because the
    kernel refuses an empty ``permissions`` when the spec is declared.

    ``ImproperlyConfigured`` rather than the ``ValueError`` the checks below
    raise: this is a deployment misconfiguration rather than a coding error, and
    it is what the MCP transport raises for the same check, so a consumer running
    both catches one thing.
    """
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
    """A spec was exposed as a tool with no ``permissions``.

    Its own category so a consumer migrating a large registry can silence it
    deliberately — ``warnings.filterwarnings("ignore", category=…)`` — rather
    than by muting ``UserWarning`` across the process.
    """


class UndescribedToolWarning(UserWarning):
    """A spec was exposed as a tool with nothing to tell the model it exists for.

    Its own category, separate from ``UnguardedSpecWarning``, because the
    two are silenced by different people: one is a security posture, the other
    is prompt quality.
    """


def _validate_descriptions(
    specs: Mapping[str, Spec],
    descriptions: Mapping[str, str] | None,
) -> dict[str, str]:
    """Resolve each tool's description, warning about the ones that say nothing.

    A key naming a tool this toolset does not expose is a typo and raises, as
    every per-tool mapping's does: dropping it silently would leave the
    tool carrying the description its author thought they had replaced.

    Warning rather than raising for a blank one, unlike the permission check: an
    undescribed tool degrades an answer, an unguarded one exposes data.
    """
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
    """Each tool's binding, checked against its spec, as a mapping of the toolset's own.

    A tool name this toolset does not expose is refused as every per-tool
    mapping's is, with ``ValueError``. Each binding then answers to the kernel's
    ``check_server_arguments`` -- an undeclared name, a name both bound and
    withheld, one a seed or a typed provider occupies, a withheld name no call
    could do without, a bound value its parameter refuses -- with the pool
    seeds every call is dispatched with, since a registered seed is reserved
    as the dispatcher's own are
    (``test_a_name_a_registered_seed_occupies_is_refused``). Its refusals are
    prefixed with the tool, like a refusal when a spec compiles.
    """
    bindings = dict(tool_server_arguments or {})
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
    """Refuse a list selector's parameter that the pagination arguments take away.

    The inputs' side of the collision ``_validate_declared_reads`` refuses for
    a shape. That check keeps a shape off ``page`` / ``limit``; this one keeps
    the spec's inputs off them, because an
    input of such a name registers, is advertised, and then never receives what
    the model sent: ``_pop_pagination`` takes both out of every call to serve
    the page. A required one would fail every call carrying it; a defaulted one
    would run on its default whatever the call asked for. Only a list tool
    paginates, so a retrieve selector's ``page`` reaches it, and the condition is held by
    ``test_a_retrieve_selector_keeps_a_parameter_named_page``. Read off the
    compiled spec's own ``parameters()``, which no seed or provider narrows.

    A read the spec declares under an input's name is not refused here or
    anywhere: it stays in the call, where the kernel hands it to the input as
    its argument.

    The same pass refuses the two other declarations no call can serve: a
    malformed ``metadata["json_schema"]`` fragment (``_schema_fragment``), and
    an input field the spec's Validator requires and no call can send
    (``_refuse_unsendable_fields``).
    """
    for tool_name, spec in specs.items():
        # The first assembly of each spec's whole argument set, for every spec
        # and not only a list selector, so the refusals assembling it can raise
        # surface here, at construction, prefixed with the tool like the
        # compile's: a Validator field sharing a target lookup parameter's
        # name, which the kernel refuses as one name from two sources. Held by
        # ``test_a_field_sharing_a_lookup_parameter_s_name_is_refused_naming_the_tool``.
        with _refusals_name_the_tool(tool_name):
            own = kernel_spec(spec).parameters().names()
            # Both phases, so a malformed fragment is refused when the toolset
            # is built, naming the tool, whichever phase it is in and whether or
            # not that phase derives a schema
            # (``test_a_malformed_phase_is_refused_naming_the_tool``).
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
    """Refuse an input field the spec's Validator requires and no call can send.

    A Validator may leave a required field out of its ``parameters()``, under a
    name it fills itself, so the kernel's schema does not advertise the field
    and its ``dispatch`` refuses a value the model sends for it as an unknown
    argument under ``REJECT`` and drops it under ``IGNORE``. The Validator then
    fails every call for the missing field
    (``test_a_field_the_validator_calls_unsendable_is_refused_naming_the_tool``).
    Only the Validator can say such a field is there, which it does as an
    ``unsendable`` attribute: a collection of field names. Read duck-typed and
    naming no package, so any Validator answering it is read alike, and one
    that does not answer names none
    (``test_a_validator_that_does_not_answer_unsendable_builds_and_serves``).

    A spec compiled partial demands none of its fields of a call, so it is not
    refused, and the attribute names the field whether or not the spec is
    partial, so the compiled spec's own flag is read here
    (``test_a_partial_service_is_not_refused_for_an_unsendable_field``). A
    selector declares no fields; a service with no Validator answers nothing.
    """
    compiled = kernel_spec(spec)
    # One arc for two conditions: a selector has no ``partial`` to read, so
    # dropping the ``isinstance`` fails every selector tool at build
    # (``test_a_dispatcher_name_the_model_sends_is_refused_or_dropped``, among
    # many); dropping the ``partial`` read fails
    # ``test_a_partial_service_is_not_refused_for_an_unsendable_field``.
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
    """The spec's ``metadata["json_schema"][phase]`` fragment, or ``None`` if undeclared.

    Read off the compiled spec, which carries the declaring spec's
    ``metadata``, never a nested one's, and refused where it cannot be merged:
    a declaration
    that is not a mapping (``test_a_malformed_schema_fragment_is_refused_naming_the_tool``),
    one naming a key that is no phase, and a phase that is not a mapping
    (``test_a_malformed_phase_is_refused_naming_the_tool``).
    """
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
    """The reads ``compiled`` declares, in every seat a caller's argument reaches, and its shapes.

    A declared read is one not taken off a signature: every read of a
    kernel-native spec, and each read another library's spec declares, which
    compiles unmarked where a read it reflects off a callable is marked
    ``reflected``. A service's target lookup is its own
    seat, and a declared read the lookup takes compiles onto it rather than
    onto the service's ``reads``
    (``test_a_lookup_read_named_after_a_registered_seed_is_refused_at_build``).
    """
    seats = [compiled.reads]
    target = compiled.target_selector_spec() if isinstance(compiled, KernelServiceSpec) else None
    if target is not None:
        seats.append(target.reads)
    declared = (parameter for reads in seats for parameter in reads if not parameter.reflected)
    return (*declared, *compiled.shapes)


def _validate_declared_reads(tool_name: str, spec: Spec, *, seeds: PoolSeeds) -> None:
    """Refuse a read the spec declares under a name the toolset or a pool seed fills.

    **A registered pool seed is reserved like a built-in one.** The kernel
    refuses a declared read named after a seed on every call, sent or not, as
    an argument meeting a seed in one pool, so the tool would build, be
    advertised and serve nothing; refused here, it fails the mount naming its
    tool, whichever source the spec came from
    (``test_a_channel_named_after_a_registered_seed_is_refused``,
    ``test_a_mapping_read_named_after_a_registered_seed_is_refused_at_build``).
    A read the kernel reflects off a callable's signature is not one: the seed
    fills it.

    **A shape may not take the transport's own names**, ``page``, ``limit``
    and ``ordering`` (``test_a_reserved_read_name_is_rejected``). A read a
    callable takes may: a list selector's ``page`` or ``limit`` is refused by
    ``_validate_inputs_pagination_takes``, and a selector's ``ordering`` is its
    sort.
    """
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


async def _with_deadline(
    awaitable: Any,
    seconds: float | None,
    *,
    label: str,
    extra: Mapping[str, Any] | None = None,
) -> Any:
    """Await ``awaitable``, answering the model instead of hanging past ``seconds``.

    ``None`` awaits without a deadline, so the resolved bound goes straight in.

    **This does not stop the work.** The dispatch runs in a ``sync_to_async``
    thread and asyncio cannot interrupt a thread parked in a database driver's
    socket read, so the query runs to completion regardless; what the deadline
    buys is a terminal answer rather than a run that never returns.

    That answer is a ``ToolFailed`` rather than an ordinary exception, for the
    same reason the byte ceiling's is: the model can respond to it by asking for
    less, and killing the run denies it the chance. It is not a ``ModelRetry``
    either -- a retry budget is finite, and a run should not die because a model
    spent it on progressively narrower queries against a slow table. Raised
    rather than returned, as the module docstring says of every terminal
    failure.

    ``extra`` is the caller's log correlation, threaded in rather than derived:
    this is a module-level helper with no ``RunContext``, and its one call site
    inside ``call_tool`` has one. A timeout is a run misbehaving, which is
    precisely when a line that cannot be tied back to its run is worth least --
    concurrent runs interleave, and every one of them times out the same way.
    """
    if seconds is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        # The operator hears about it too: a tool that intermittently answers
        # "took too long" and logs nothing is indistinguishable from a bug.
        logger.warning("Tool %r exceeded its %.1fs dispatch timeout", label, seconds, extra=extra)
        raise ToolFailed(
            f"This call took longer than the {seconds:g}s limit and was "
            "abandoned. Narrow the request — add or tighten a filter, or "
            "lower `limit` — and call again."
        ) from exc


def _default_get_progress(ctx: RunContext[Any]) -> ProgressReporter | None:
    """Read ``ctx.deps.progress``, tolerating a deps type that has no such field.

    ``getattr`` rather than attribute access: a project with its own deps class
    need not declare the field, and a missing sink is the ordinary case.
    """
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
    """Refuse a policy this toolset cannot carry out, at construction.

    ``PASSTHROUGH`` is the one there is. The kernel's dispatch raises
    ``ValueError`` for it on every call, so the toolset refuses it here, at
    build, rather than on the first tool call. The reason is not the kernel's
    alone. A model's tool call is one flat object and the toolset hands the
    kernel every key in it as an argument, so there is no second channel an undeclared name could
    travel through to reach the callable; the declaration is what makes a name
    an argument.
    """
    if unknown_arguments in _ACCEPTED_UNKNOWN_ARGUMENTS:
        return
    # The value, not the member's repr: a policy read from settings arrives as
    # the string, and the message should name what the project wrote.
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
    """Refuse a block line changed beside the override that replaces the block.

    Ignored configuration fails loudly here, and the block's lines are exactly
    what ``instructions=`` stands in for. Compared against the defaults field by
    field, so ``AgentConventions()`` passed explicitly is not a change, and
    neither is changing only the fields that land outside the block. Every
    changed field is named at once, so one restart fixes them all.
    """
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
    """One field of ``AgentConventions`` as the model reads it, or ``None`` if dropped.

    Every field is rendered, placeholders or not, so a doubled brace means one
    brace in all of them alike (``test_a_doubled_brace_reaches_the_model_as_one``).
    The fields were validated against these exact values' names and types when
    the conventions were built, so this cannot raise.
    """
    return None if template is None else template.format(**values)


def _listed(names: Sequence[str]) -> str:
    """Names as a ``{names}`` placeholder receives them: each in backticks, comma-joined."""
    return ", ".join(f"`{name}`" for name in names)


def _unavailable_instruction(unavailable: Mapping[str, Affordance], heading: str) -> str:
    """The heading, then one line per tool left out: its name and its ``reason``.

    In the toolset's declaration order, which is the order ``unavailable`` was
    built in, so the same step always reads the same way. The ``code`` is left
    out on purpose: it is for programs and for tying a refusal to a row's
    ``affordances``, and a model relaying this to a person has no use for it.

    Said once, above the names, so each tool left out costs one short line. What
    the heading has to carry is the thing the catalog's silence cannot: the tool
    exists, and there is a reason it is absent that can be passed on to a person
    as written -- a condition's ``reason`` is a sentence written for people and
    models alike.
    """
    # Rendered like every other field, so a doubled brace reads as one here too
    # (``test_a_doubled_brace_reaches_the_model_as_one``).
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
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
    conventions: AgentConventions = _DEFAULT_CONVENTIONS,
) -> str | None:
    """Build the conventions block from the specs / shapes / ordering.

    Every line is conditional on something being able to act on it: pagination
    only with a list selector present, ordering only where some tool actually has
    a sort argument, read-shaping only with a shape. Advice a model
    cannot use is not neutral — it is budget spent teaching it about an argument
    that will be rejected.

    **The conditions are decided here and the words come from ``conventions``.**
    A consumer changes what a line says, never whether it appears, so an
    overridden pagination line is still absent from a toolset with no list tool
    and from a step that offers none. A field set to ``None`` drops its line;
    ``None`` comes back when every line was dropped, so a toolset that says
    nothing adds nothing to the prompt.

    **The specs are the only source of an ordering argument**, so this asks them
    and nothing else: whatever the schema advertises is what the model may send.
    ``pool_seeds`` is the toolset's, so a sort a registered seed fills is asked
    about as the schema was built.
    """
    lines: list[str | None] = [_render(conventions.base)]
    if any(_is_list_selector(spec) for spec in specs.values()):
        # The same number each ``limit`` description states, for the reason
        # ``_served_page_size`` gives.
        lines.append(_render(conventions.pagination, page_size=page_size))
    # Deduplicated, in first-seen order: one toolset can carry several tools
    # whose sorts are declared under different names, and a model reading one
    # block for the whole toolset has to be told each. Named rather than said as
    # ``ordering`` outright, because a toolset whose ``OrderingFilter`` is called
    # ``sorting`` would otherwise be told about an argument that does not exist.
    ordering_names: list[str] = []
    for spec in specs.values():
        advertised = _spec_ordering_argument(spec, pool_seeds=pool_seeds)
        if advertised is not None and advertised not in ordering_names:
            ordering_names.append(advertised)
    if ordering_names:
        lines.append(_render(conventions.ordering, names=_listed(ordering_names)))
    # A toolset with no handle anywhere gains nothing from being told how to
    # treat one, and this block is prepended to every run.
    if any(_has_handle(projection) for projection in (projections or {}).values()):
        lines.append(_render(conventions.handles))
    shape_names = sorted({name for names in tool_shapes.values() for name in names})
    if shape_names:
        line = _render(conventions.read_shaping, names=_listed(shape_names))
        # Keyed on a list tool *declaring* one, not on a list tool and a
        # shape both being present: a toolset whose only read-shaping argument
        # is on a retrieve tool returns no page for it to be misread against.
        scope = (
            _render(conventions.read_shaping_on_pages)
            if any(
                _is_list_selector(spec) and tool_shapes.get(name) for name, spec in specs.items()
            )
            else None
        )
        # The continuation goes with the line it continues. One arc: ``line is
        # not None`` is held by ``test_none_drops_a_block_line_and_nothing_else
        # [read_shaping]``; ``scope is not None`` by its ``[read_shaping_on_pages]``
        # case and by ``test_an_overridden_line_follows_a_tool_a_condition_leaves_out``,
        # whose read-shaping param is on a tool that returns no page.
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
    projection: AudienceProjection | None = None,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
    conventions: AgentConventions = _DEFAULT_CONVENTIONS,
    server_names: frozenset[str] = frozenset(),
    shapes: frozenset[str] = frozenset(),
    unknown_arguments: UnknownArguments = UnknownArguments.REJECT,
) -> ToolDefinition:
    """One tool definition: what the model may send, and what it gets back.

    ``include_return_schema`` is deliberately **left at its default**, which
    resolves to ``False``. Populating ``return_schema`` costs nothing; putting it
    on the wire costs context on every turn of every run, and whether that trade
    is worth it depends on the model and the size of the output — neither of
    which this package knows. Pydantic-AI already owns the opt-in, at both
    scopes: ``SpecToolset(...).include_return_schemas()`` for one toolset, or the
    ``IncludeToolReturnSchemas`` capability for a run. A knob here would be a
    third way to say the same thing, and the one a consumer composing through
    ``SpecCapability`` still could not reach.
    """
    return ToolDefinition(
        name=name,
        description=description,
        parameters_json_schema=_input_schema(
            spec,
            max_page_size,
            pool_seeds=pool_seeds,
            conventions=conventions,
            server_names=server_names,
            shapes=shapes,
            unknown_arguments=unknown_arguments,
        ),
        return_schema=_return_schema(spec, projection=projection, conventions=conventions),
        metadata={
            "annotations": {"readOnlyHint": isinstance(kernel_spec(spec), KernelSelectorSpec)}
        },
    )


def _return_schema(
    spec: Spec,
    *,
    projection: AudienceProjection | None = None,
    conventions: AgentConventions = _DEFAULT_CONVENTIONS,
) -> dict[str, Any] | None:
    """The shape of what this tool returns, or ``None`` when the spec declares none.

    The kernel's ``spec_output_schema`` over the compiled spec: the schema of
    what dispatch presents, so the same one every transport serving the spec
    states. Generated from the **projected** output rather than from the whole
    declaration, so it describes the payload the model is actually handed:
    ``present_for_audience`` drops hidden fields and substitutes a choice
    field's display value, and a schema still advertising either would be worse
    than no schema at all — a model asking for a field the render removes gets
    nothing back and no reason why.

    ``paginate`` is ``_is_list_selector`` and not "the result is a list",
    because it answers *does this toolset wrap the result*: the toolset pages
    list selectors only, so a service whose output is a list -- a ``LIST``
    re-read, a ``many`` service, or one that ``returns_many`` -- is a bare array
    here, and claiming the envelope for it would advertise a shape its payload
    never has (``test_a_service_whose_output_is_a_list_is_an_array_not_an_envelope``).
    Where it admits ``null``, and the ``affordances`` object each item carries,
    are the kernel's answers too, so this schema and dispatch cannot disagree
    about either.

    ``None`` for a spec with no presenter is the correct answer and not a gap:
    the kernel refuses to fabricate a shape it cannot derive, and a guessed one
    would be a claim the payload never has to honour.

    **A ``metadata["json_schema"]["output"]`` fragment wins, key by key**, and
    annotates a derived
    schema without conjuring one: a spec with no output keeps ``None``
    (``test_an_output_fragment_never_conjures_a_return_schema``).
    """
    fragment = _schema_fragment(spec, "output")
    derived = spec_output_schema(
        spec,
        paginate=_is_list_selector(spec),
        projection=projection,
        # The fallback for a handle declaring no wording of its own. The
        # kernel supplies none on purpose, and ``None`` here leaves such a
        # field undescribed, which is what dropping the line means.
        handle_description=_render(conventions.handle_field_description),
    )
    if derived is None or fragment is None:
        return derived
    return {**derived, **fragment}


def _spec_description(spec: Spec) -> str | None:
    """The tool description: the docstring of the compiled spec's selector / service."""
    compiled = kernel_spec(spec)
    callable_ = compiled.selector if isinstance(compiled, KernelSelectorSpec) else compiled.service
    return inspect.getdoc(callable_) if callable_ is not None else None


def _input_schema(
    spec: Spec,
    max_page_size: int | None = None,
    *,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
    conventions: AgentConventions = _DEFAULT_CONVENTIONS,
    server_names: frozenset[str] = frozenset(),
    shapes: frozenset[str] = frozenset(),
    unknown_arguments: UnknownArguments = UnknownArguments.REJECT,
) -> dict[str, Any]:
    """The tool's parameter schema: what the spec takes, with a list selector's pagination.

    The base is ``_caller_schema``, the kernel's schema of the compiled spec
    less the names the server fills. Every read the spec declares is one of
    its parameters there, a shape or a read a callable takes alike, with the
    node, the ``required`` and the default the compiled spec gives it, so a
    tool built off ``registry.specs()`` advertises its reads as one built off
    the registry does.

    **No sort argument is contributed here.** A tool that can sort says so in
    its own schema — a ``filter_set``'s ``OrderingFilter``, or an ``ordering``
    parameter on the selector callable — and that property passes straight
    through.

    **A list selector gains ``page`` and ``limit``**, which the toolset takes
    out of the call before dispatch, and each of the spec's ``shapes`` is told
    what it applies to on a page (``_with_scope``). A read a callable takes is
    that callable's argument, which the page's items never see, so it is told
    nothing.

    A ``many=True`` spec's schema is already the object a model can send: one
    required array property named by ``spec.many_argument``.

    **A ``metadata["json_schema"]["input"]`` fragment wins, key by key**, over
    the spec's schema and before the toolset's own arguments
    (``test_a_schema_fragment_wins_over_the_input_schema_key_by_key``).

    **Less ``server_names``, last**: every name ``tool_server_arguments``
    binds or withholds leaves ``properties`` and ``required``
    (``test_bound_and_withheld_names_leave_the_schema_and_its_required``,
    ``test_a_tool_with_no_channels_loses_the_bound_name_too``).
    """
    schema = _caller_schema(spec, pool_seeds=pool_seeds, unknown_arguments=unknown_arguments)
    fragment = _schema_fragment(spec, "input")
    if fragment is not None:
        schema = {**schema, **fragment}
    if _is_list_selector(spec):
        schema = {
            **schema,
            "properties": {
                **_with_scope(
                    schema["properties"], shapes, _render(conventions.query_param_on_pages)
                ),
                "page": _PAGE_PARAM_SCHEMA,
                "limit": _limit_param_schema(max_page_size),
            },
        }
    return _without(schema, server_names)


def _with_scope(
    properties: dict[str, Any], shapes: frozenset[str], scope: str | None
) -> dict[str, Any]:
    """``properties`` with ``scope`` appended to each of ``shapes``' descriptions.

    Every list selector here returns a page, and a read-shaping argument's
    declared description is written by someone thinking of a row -- "fields to
    return" -- while the model reads it beside a documented
    ``{"items": [...], ...}`` result. The scope sentence closes that gap at the
    parameter, where the model is choosing a value. Appended after the declared
    text, which stays first because it is the part that says what the argument
    *is*; with no declared text it is the whole description. ``None``, when the
    conventions dropped the sentence, leaves every declaration as written. A
    shape an input schema fragment left out of ``properties`` is told nothing
    (``test_a_shape_a_schema_fragment_drops_is_told_nothing``).
    """
    if scope is None:
        return properties
    scoped = dict(properties)
    for name in shapes & properties.keys():
        declared = scoped[name].get("description")
        scoped[name] = {**scoped[name], "description": f"{declared} {scope}" if declared else scope}
    return scoped


def _without(schema: dict[str, Any], names: frozenset[str]) -> dict[str, Any]:
    """``schema`` with ``names`` out of its ``properties`` and its ``required``.

    The schema itself when there is nothing to take out, so a tool no binding
    names is advertised unchanged. Otherwise a new mapping, never an edit of
    the one passed in. A ``required`` left empty is dropped, as the kernel
    writes none for a spec requiring nothing.
    """
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
) -> dict[str, Any]:
    """What ``spec`` itself asks of the model, before this toolset's own arguments.

    The kernel's ``spec_input_schema`` of the compiled spec, the argument set
    its ``dispatch`` checks: a selector's reads and shapes, or a service's
    target lookup's reads, its own, its shapes and its Validator's fields. Read
    under the toolset's own ``unknown_arguments``, because the schema closes the
    argument set, ``"additionalProperties": false``, exactly where dispatch
    does: under ``REJECT`` and not under ``IGNORE``
    (``test_the_schema_closes_the_argument_set_only_where_the_call_does``).

    **Less ``server_owned_keys`` for this toolset's seeds**: every name dispatch
    fills from the server whatever the caller sends -- the dispatcher's own
    seeds, every registered one, and each key a ``kwargs=`` or ``input_data=``
    provider is annotated to fill. A registered seed's name a service's
    Validator declares as a top-level field is not among them, since that field
    is the caller's to send. Advertised, such a name would be an argument the
    call drops or ``REJECT`` refuses
    (``test_a_registered_seed_outranks_a_model_argument_of_the_same_name``).

    **A read a provider may decline is advertised and not required**: the
    kernel lists it as declared, ``required`` meaning the call needs the value
    from someone, and dispatch does not demand it of the caller, since the
    provider may fill it
    (``test_a_key_the_provider_may_decline_stays_the_models_to_send``), and in
    its own seat only
    (``test_a_key_one_seat_may_decline_is_still_required_by_another``); relaxing
    the last one leaves no ``required`` at all
    (``test_a_schema_whose_every_read_may_be_declined_requires_nothing``). A read
    beside a provider whose keys cannot be read stays as the compiled spec
    declares it, required or not
    (``test_a_marked_parameter_stays_required_beside_an_untyped_provider``).
    """
    compiled = kernel_spec(spec)
    schema = _without(
        spec_input_schema(compiled, unknown_arguments=unknown_arguments),
        kernel_server_owned_keys(compiled, pool_seeds=pool_seeds),
    )
    declinable: set[str] = set()
    for names, keys in _read_seats(compiled):
        if keys is not None:
            declinable |= names & keys.declinable
    required = [name for name in schema.get("required", []) if name not in declinable]
    if required == schema.get("required", []):
        return schema
    relaxed = {key: value for key, value in schema.items() if key != "required"}
    if required:
        relaxed["required"] = required
    return relaxed


def _read_seats(
    compiled: KernelServiceSpec | KernelSelectorSpec,
) -> list[tuple[frozenset[str], ProviderKeys | None]]:
    """Each seat of reads in ``compiled`` with what its provider says it fills.

    A selector's ``reads`` beside its ``kwargs=``; a service's own beside its
    ``kwargs=``, and its target lookup's beside the lookup's. Each provider's
    values reach its own seat only, so a provider's keys say nothing about a
    read in another. ``None`` is a provider whose keys cannot be read, which
    may fill any read in its seat; no provider at all fills nothing. A
    Validator's fields are not a seat here: the Validator answers for them.
    The target's seat is held by ``test_a_service_call_missing_its_lookup_is_handed_back``.
    """
    seats = [(compiled.reads.names(), provider_keys(compiled.kwargs))]
    target = compiled.target_selector_spec() if isinstance(compiled, KernelServiceSpec) else None
    if target is not None:
        seats.append((target.reads.names(), provider_keys(target.kwargs)))
    return seats


def _required_arguments(
    spec: Spec,
    *,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
) -> tuple[str, ...]:
    """The reads a call to ``spec`` cannot go without, as its schema requires them.

    Read off ``_caller_schema``, the schema the tool advertises, so what a call
    is refused for and what the model was told cannot drift apart. Only the
    reads: a service's Validator fields, and the list a ``many`` service takes,
    are checked by dispatch, which answers a missing one itself
    (``test_a_validator_still_words_its_own_required_fields``).

    **Less every read a provider whose keys cannot be read may fill**: the
    schema keeps such a read required where a marker says so, and dispatch
    does not demand it of the caller, since only the assembled pool can say
    whether it arrived
    (``test_an_untyped_provider_filling_a_marked_parameter_is_not_refused``).
    """
    reads: set[str] = set()
    may_fill: set[str] = set()
    for names, keys in _read_seats(kernel_spec(spec)):
        reads |= names
        if keys is None:
            may_fill |= names
    required = _caller_schema(spec, pool_seeds=pool_seeds).get("required", [])
    return tuple(name for name in required if name in reads and name not in may_fill)


def _is_list_selector(spec: Spec) -> bool:
    compiled = kernel_spec(spec)
    return isinstance(compiled, KernelSelectorSpec) and compiled.kind == SelectorKind.LIST


def _spec_ordering_argument(
    spec: Spec,
    *,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
) -> str | None:
    """The argument name a list spec advertises its sort under, or ``None``.

    **The one signal, read by both the instructions and the dispatch path, so
    they cannot disagree** — the invariant being *whatever the schema advertises,
    the dispatch must deliver*.

    **Returns the name rather than a yes/no.** Asking only whether the schema
    contains the literal ``"ordering"`` would assume the one name the
    documentation happens to suggest. For a project whose ``OrderingFilter`` is
    called ``sorting`` the sort would still apply, but the usage instruction
    would be dropped, and a sort sent under ``ordering`` would not be refused
    as one the tool never offered.

    django-filter is a dependency of the project declaring the FilterSet, never
    of this package, which never imports it, and a spec's
    ``filter_set`` is duck-typed all the way down. ``get_ordering_value`` is
    defined by ``OrderingFilter`` and by nothing else in the filter hierarchy, so
    asking for it identifies the sort filter under whatever name it was
    declared, including a project's own subclass.

    Falls back to the literal ``"ordering"``, which covers the case a
    ``filter_set`` cannot answer for: a list selector with no ``filter_set``
    whose *callable* declares an ``ordering`` parameter, listed in the schema
    like any other selector argument and consumed by the callable itself.

    Restricted to list selectors because that is the only kind the toolset
    pages: elsewhere there is no ownership to contest, and a service whose
    Validator happens to have a field named ``ordering`` must not be
    read as a clash.

    **Read off ``_caller_schema``, the schema the tool advertises**, told the
    same seeds. A selector whose ``ordering`` parameter a typed provider or a
    registered seed fills is offered no sort, so no instruction may teach one
    and a sort the model sends anyway is refused. Read without them, this
    would answer ``"ordering"`` for a property the schema dropped: held by
    ``test_an_ordering_the_toolset_fills_is_refused_from_the_model`` and
    ``test_an_ordering_the_toolset_fills_gets_no_usage_line``.
    """
    if not _is_list_selector(spec):
        return None
    properties = _caller_schema(spec, pool_seeds=pool_seeds).get("properties", {})
    filter_set = getattr(spec, "filter_set", None)
    for name, declared in getattr(filter_set, "base_filters", {}).items():
        # Advertised as well as declared: a filter the schema does not carry is
        # not something the model can send, so claiming it would break the
        # advertise/deliver invariant in the other direction.
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
    label: str,
    progress: ProgressReporter | None = None,
    pool_seeds: PoolSeeds = DEFAULT_POOL_SEEDS,
    call_context: dict[str, Any] | None = None,
    translate_exception: _ExceptionTranslator,
    shape_page: _PageShaper,
    render_output: _OutputRenderer,
    enforce_result_bytes: _ResultBounder,
    conventions: AgentConventions = _DEFAULT_CONVENTIONS,
) -> Any:
    """Dispatch ``spec`` through the kernel and render the result for the model.

    Synchronous on purpose — ``SpecToolset.call_tool`` runs it in a thread so
    the ORM stays off the event loop.

    The four keyword seams are what
    [`SpecToolset`][django_spec_agents.spec_tools.SpecToolset] threads its
    overridable methods through, bound to the run: ``translate_exception``
    covers both halves of a call, and ``shape_page`` / ``render_output`` /
    ``enforce_result_bytes`` the back half.

    ``pool_seeds`` are the call's seeds, the mount's with the call's own beside
    them, and ``call_context`` the call's context, both built by
    [`SpecToolset`][django_spec_agents.spec_tools.SpecToolset] in the dispatch
    thread before this runs.

    ``conventions`` words the two retries this function writes itself: a missing
    argument, and a read-shaping value the render rejected on a paged tool.

    Every read the spec declares is an argument like any other, whatever its
    library calls it; its declared default is applied by ``_fill_reads``.
    ``label`` is the tool's name, which the call's errors and logs carry.
    """
    # **The server's arguments before anything reads the call.** Before the
    # required-arguments check below, so a required read the binding fills is
    # not refused as missing (``test_a_binding_fills_a_required_read_the_model_never_sends``);
    # and before anything reads a value, so a bound read is the value the call
    # carries rather than the model's
    # (``test_the_bound_value_outranks_the_one_the_model_sends``).
    server_names: frozenset[str] = frozenset()
    if server_arguments is not None:
        server_names = server_arguments.names()
        args = _apply_server_arguments(spec, args, server_arguments, label=label)
    # **Then against the arguments as the model sent them.** A selector -- the
    # tool's own, or the lookup a service resolves its target through -- that is
    # called without a parameter it has no default for raises ``TypeError``,
    # which would escape the run. The names are the ones the tool's schema
    # requires of the selector, from the same reflection, a required declared
    # read among them
    # (``test_a_required_read_with_no_default_is_handed_back_when_left_out``).
    # Not one a provider only may fill, which only the assembled pool can
    # settle. The Validator's fields are not checked here; dispatch reports
    # those. It runs before the permission check, which the MCP transport's
    # does not: its listing hides a tool its caller is denied, so answering there first would
    # confirm the tool exists. This catalog is not permission-filtered (see
    # ``get_tools``), so the names asked for here are ones every run was shown.
    missing = [
        name for name in _required_arguments(spec, pool_seeds=pool_seeds) if name not in args
    ]
    if missing:
        raise _missing_arguments(missing, conventions)
    # Only the pagination arguments leave the call. A read, a shape among
    # them, is an argument like any other, and the kernel hands what the
    # caller sent for the spec's declared ``shapes`` to the presenter alone.
    page_args = _pop_pagination(spec, args, pool_seeds=pool_seeds)
    try:
        # Inside the ``try``, as every refusal the call can meet is: compiling
        # the spec and reading its parameters can refuse it (a Validator field
        # sharing a lookup parameter's name), and the consumer's map is asked
        # about that as about anything ``dispatch`` raises.
        compiled = kernel_spec(spec)
        # **Read before dispatch, from the arguments as the caller sent them.**
        # The render arm below needs to know whether the caller shaped the
        # render: a value the model sent is something it can correct, while a
        # render failing on no value of the caller's is a configuration bug and
        # has to stay loud. In the spec's declaration order, so a message
        # naming several reads the same way on every call. A ``None`` is not a
        # value: ``{"fields": null}`` is how a model says it chose not to fill
        # one. A bound value is the server's, not the caller's, so it is not
        # counted either: a retry would name an argument the model was never
        # offered
        # (``test_a_bound_selection_the_render_rejects_is_the_server_s_error_not_a_retry``).
        shaped = [
            parameter.name
            for parameter in compiled.shapes
            if args.get(parameter.name) is not None and parameter.name not in server_names
        ]
        _fill_reads(compiled, args)
        arguments = _call_arguments(
            args,
            compiled.parameters().names(),
            owned=kernel_server_owned_keys(compiled, pool_seeds=pool_seeds),
        )
        result = dispatch(
            compiled,
            principal=user,
            arguments=arguments,
            # Authorization is the kernel's: the class-level check before any row
            # is resolved, then the object- or scope-level check on what was. A
            # spec declaring no check at all reached here only because the toolset
            # was built with ``require_permissions=False``, which construction
            # warned about; the kernel refuses such a spec at every call, so the
            # knob's meaning is kept by vouching for that one spec and this one
            # principal. The condition reads the spec alone, never the knob,
            # since construction refused every such spec with the knob on
            # (``test_with_the_knob_off_only_an_unguarded_spec_runs_unchecked``
            # fails when every call is granted).
            grant=Grant(compiled, user) if compiled.permissions is None else None,
            pool_seeds=pool_seeds,
            unknown_arguments=unknown_arguments,
            # Accepted and forwarded, never constructed -- see ``AgentDeps.progress``.
            # ``None`` becomes the kernel's no-op seed.
            progress=progress,
            call_context=call_context,
        )
    except BaseException as exc:
        # **One arm, not a chain, because the consumer's map has to be consulted
        # first *and* fall through when it declines.** Written as a leading
        # ``except`` clause instead, every arm below it becomes unreachable — the
        # ``raise`` for an unclaimed exception leaves the ``try`` entirely rather
        # than trying the next clause.
        handler = translate_exception(exc)
        if handler is not None:
            return handler(exc)
        if isinstance(exc, InvalidArguments | ServiceValidationError):
            # ``ServiceValidationError`` is a ``ServiceError`` subclass, so it
            # must be matched here, before the business-error case below. Both
            # mean "the arguments were wrong", so the model retries with the
            # detail -- flattened to text, one line per message. The kernel's
            # ``InvalidArguments`` is its own refusal of the arguments' shape and
            # a Validator's refusal of their values
            # (``test_a_validator_less_service_advertises_what_dispatch_admits``);
            # and ``ServiceValidationError`` a service's
            # (``test_service_validation_error_is_model_retry``). Each fails
            # without its member.
            raise ModelRetry(_format_validation_detail(exc.detail)) from exc
        if isinstance(exc, AdditionalInputRequired):
            # **Must precede the ``ServiceError`` case below** — this is a
            # subclass of it, and the generic handler would report a request for
            # input as a terminal failure. ``ModelRetry`` is already the "here is
            # what to fix, call me again" channel, so the answer comes back as an
            # ordinary argument on the next call.
            raise ModelRetry(_missing_input_prompt(exc)) from exc
        if isinstance(exc, ServiceError):
            # **Raised, not returned, with the rule's own message.** A business
            # rule that refused — a conflict, a state the operation cannot run
            # against — is ``ToolFailed``'s own description: the call is done, it
            # failed definitively, and the model should adapt rather than repeat
            # it. ``ToolFailed`` spends no retry budget and prepends no
            # correction instructions, and marks the return ``outcome="failed"``
            # where a returned value would read as a success one hop out (see
            # the module docstring).
            if isinstance(exc, ActionUnavailable):
                # **Inside this branch, not ahead of it, and still behind the
                # consumer's map.** A subclass of ``ServiceConflict`` and so of
                # ``ServiceError``: it fails the call exactly as its parent does,
                # and the only difference is what the sentence carries. The
                # message is the only channel -- ``ToolFailed`` takes nothing
                # else, and it is what a transport forwards as the result -- so
                # a refusal that dropped its ``code`` here would leave the model
                # reading a reason it could not tie to the ``affordances`` answer
                # naming the same rule, with the name surviving only on
                # ``__cause__``, where no model looks.
                raise ToolFailed(_refusal_message(exc)) from exc
            raise ToolFailed(str(exc)) from exc
        raise

    if result.kind == "not_found":
        # "a missing resource" is the first case ``ToolFailed`` names. Raised here rather
        # than in the ``except`` arm because the kernel reports an unresolved
        # target as a *result kind*, not an exception.
        raise ToolFailed("not found")

    # ``page_args`` is non-None exactly for list selectors — the only specs that
    # advertise pagination args and return a (lazy) queryset to slice.
    page: OutputPage | None = None
    if page_args is not None:
        # Deliberately unguarded against ``FieldError``. Sorting is applied by
        # whatever declared it, upstream of here, and Django validates a
        # plain-string ``order_by`` eagerly — so a ``filter_set`` whose
        # ``param_map`` names something that is not a column raises inside
        # ``dispatch``, not at this line. An arm here would never fire.
        page = shape_page(
            result.value,
            page=page_args.page,
            limit=page_args.limit,
            max_page_size=max_page_size,
        )
        # The page's rows presented, never the whole set: the recipe the
        # kernel's ``present_for_audience`` gives for serving one page.
        result = replace(result, value=page.items)
    try:
        rendered = render_output(spec, result, projection=projection)
    except (InvalidArguments, ServiceValidationError) as exc:
        # **A read-shaping argument is the one caller input used while
        # rendering**, not while dispatching -- a ``fields`` or ``query``
        # selection reaches the presenter on the kernel's ``shapes`` and is read
        # while it presents each row -- so a bad one fails here, after the
        # ``try`` above has closed, and would escape the run as a raw refusal.
        # Wrapped around the call rather than inside the default
        # ``render_output`` so a consumer's override is covered too.
        #
        # Two refusals, one arc, each held by a test raising it: the kernel's
        # ``InvalidArguments``, which is the seat a presenter refusing a shape
        # raises
        # (``test_a_presenter_refusing_a_shape_with_the_kernel_s_refusal_is_a_retry``);
        # and a ``ServiceValidationError``, which a presenter or an override may
        # raise (``test_an_unknown_row_field_is_a_retry_not_a_dead_run``,
        # ``test_a_render_output_override_is_covered_too``).
        #
        # The consumer's map first, and its handler's value returned as-is,
        # exactly as on the dispatch path: one ``translate_exception`` should not
        # have to know which half of the call an error came from.
        handler = translate_exception(exc)
        if handler is not None:
            return handler(exc)
        # **Only when the caller shaped the render.** With no read-shaping value
        # sent, nothing the model could change would change the outcome -- a
        # presenter that raises unprompted, or a declared default it rejects --
        # so a retry would spend the budget on a server bug and hide it.
        # Validation errors only, for the same reason: an ``AttributeError`` in
        # a presenter is a server bug whatever the caller sent.
        #
        # One arc to coverage, and its parts live in how the names were
        # gathered, so what holds each is named here. Deleting this condition
        # fails ``test_render_error_with_no_shape_supplied_still_raises``,
        # ``test_render_error_from_a_seeded_default_still_raises`` and
        # ``test_render_error_with_an_explicit_null_still_raises``; counting a
        # null as supplied fails the third alone, and counting a bound value as
        # supplied
        # ``test_a_bound_selection_the_render_rejects_is_the_server_s_error_not_a_retry``
        # alone.
        if not shaped:
            raise
        raise ModelRetry(
            _render_rejection_message(
                shaped,
                exc.detail,
                scope=_render(conventions.query_param_on_pages) if page is not None else None,
            )
        ) from exc
    if page is not None:
        # **After the render, never before.** The projection lands on the rows;
        # ``items`` / ``page`` / ``totalPages`` / ``hasNext`` are the envelope's
        # own keys and belong to no presenter, so a projection walking them
        # would look for markings that cannot exist.
        rendered = page.envelope(rendered)
    # Measured on the envelope, because the envelope is what is sent. It adds
    # three small scalars and the "there is more" signal the model needs in
    # order to act on a refusal that tells it to lower `limit`.
    return enforce_result_bytes(rendered, max_bytes=max_result_bytes, label=label)


def _apply_server_arguments(
    spec: Spec, args: dict[str, Any], server: ServerArguments, *, label: str
) -> dict[str, Any]:
    """``args`` with ``server``'s values in place and its withheld names out.

    The kernel's ``apply_server_arguments``, which drops the caller's value
    for every name the binding answers for and lays the bound values over
    what is left. A dropped key is logged at ``WARNING`` naming the tool and
    the keys, **never the values**: a binding is usually an identifier a run
    is scoped by, and what the model guessed for it is no business of a log.
    It is not a refusal either: the model was never offered the name, and a
    retry spent on it can end a run on a small budget.
    """
    dropped = sorted(server.names() & args.keys())
    if dropped:
        logger.warning(
            "Tool %r: dropped caller-supplied argument(s) %s, which tool_server_arguments "
            "keeps from the model",
            label,
            ", ".join(dropped),
        )
    return apply_server_arguments(spec, args, server)


def _enforce_result_bytes(
    payload: Any,
    *,
    max_bytes: int | None,
    label: str,
    extra: Mapping[str, Any],
) -> Any:
    """Return ``payload``, or raise a model-readable refusal when it is over budget.

    **Fails; never truncates.** A list cut at the byte ceiling is
    indistinguishable from a list that had that many rows, so a model would
    answer confidently from data it does not know is missing.

    Raised as ``ToolFailed`` rather than as ``ModelRetry``: the model should
    narrow the request and it *can*, but a retry budget is finite and a run
    should not die because a model spent it on progressively smaller queries.
    Also logged at ``WARNING``, since a bound that fires invisibly reads to an
    operator as "the tool is broken". The dispatch did succeed, but the result
    was refused and no data reached the model, so ``failed`` is the truthful
    marking.

    ``extra`` carries the caller's log correlation, which
    [`SpecToolset.enforce_result_bytes`][django_spec_agents.spec_tools.SpecToolset.enforce_result_bytes]
    reads off its ``RunContext``.
    """
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
    """The service's message, plus the names it wants the answer back under.

    ``schema`` is a JSON-Schema *properties* mapping keyed by input name, so the
    keys alone are what the model needs: it is about to call the same tool again,
    and those are the arguments to add. The full schema is deliberately not
    rendered — the tool's own parameter schema already describes them, and a
    second, differently shaped description in prose is how a model ends up
    inventing a nested object.
    """
    if not exc.schema:
        return str(exc)
    names: str = ", ".join(f"`{name}`" for name in exc.schema)
    return f"{exc} Call this tool again, additionally supplying: {names}."


def _refusal_message(exc: ActionUnavailable) -> str:
    """The sentence a refused call fails with: the reason, then the rule's code.

    ``The books are closed. (code: books_closed)``. The kernel raises
    ``ActionUnavailable`` when one of a spec's ``Affordance`` conditions is not
    met, carrying that affordance's ``reason`` as the message and its ``code`` as
    an attribute, and asks a transport serving an agent to pass on both. Here the
    ``ToolFailed`` message is the only thing a model receives and the only thing a
    transport forwards, so both go into it.

    **The code is what connects the refusal to what the model already read.** A
    selector tool's rows carry ``affordances: {<name>: {"available": false,
    "code": ..., "reason": ...}}``, and the reason is a sentence a project may
    reword between releases; the code is the part that stays put. Labelled rather
    than bare so a reader does not take ``books_closed`` for more of the sentence,
    and last so the reason still reads first.

    **Written for the model and for a person reading a tool card, not for a
    program.** The format is a convention across the family's agent transports
    rather than this toolset's own -- the MCP transport this package's
    ``[drf-mcp]`` bridge reaches is meant to render the same refusal as the
    same text, so changing it here means changing it there. A program that
    branches on the code should read ``.code`` off the exception instead, either in
    [`translate_exception`][django_spec_agents.spec_tools.SpecToolset.translate_exception]
    or on the ``ToolFailed``'s ``__cause__``. Parsing it back out of the sentence
    couples that program to wording this function is free to change.
    """
    return f"{exc.message} (code: {exc.code})"


def _format_validation_detail(detail: Any) -> str:
    """A validation detail as text a model can act on.

    Not ``str(exc.detail)``, which for a nested detail is the Python repr of
    its dicts and lists, and for a Validator's error objects carries a class
    name and a ``code`` the model has no use for, around the one sentence it
    needs.

    One line per message, each prefixed with the path to the argument it is about:
    ``name: This field is required.``, a nested object's field as
    ``address.city: ...``, a position in a list as ``tags[0]: ...``. A message not
    about any one argument -- a bare string, a list of strings, or one under
    ``non_field_errors`` -- is the line on its own, because printing that key
    would read as the name of an argument to fix. The ``code`` is dropped: it is
    for programs, and a program reads it off the exception in
    [`translate_exception`][django_spec_agents.spec_tools.SpecToolset.translate_exception].
    """
    return "\n".join(_validation_detail_lines(detail, path=""))


def _validation_detail_lines(detail: Any, *, path: str) -> Iterator[str]:
    """Walk a detail depth-first, yielding ``path: message`` for every leaf."""
    if isinstance(detail, Mapping):
        for key, value in detail.items():
            yield from _validation_detail_lines(value, path=_validation_detail_path(path, key))
    elif isinstance(detail, list | tuple):
        for index, value in enumerate(detail):
            # A message in a list belongs to the list's own path -- ``name: [a, b]``
            # is two messages about ``name`` -- while a nested structure is one
            # entry of several (the rows of a ``many`` call, each refused at its
            # position) and needs its position to be found again.
            nested = isinstance(value, Mapping | list | tuple)
            yield from _validation_detail_lines(
                value, path=_validation_detail_path(path, index) if nested else path
            )
    else:
        yield f"{path}: {detail}" if path else str(detail)


def _validation_detail_path(path: str, key: Any) -> str:
    """Extend ``path`` by one step: ``[i]`` for a position, ``.name`` for a field.

    An ``int`` key is a position whether it came from a list or from the dict a
    Validator keys a list field's child errors by. The non-field key
    adds nothing, so its messages print against the object that holds them.
    """
    if isinstance(key, int):
        return f"{path}[{key}]"
    if key in NON_FIELD_KEYS:
        return path
    return f"{path}.{key}" if path else str(key)


def _render_rejection_message(names: Sequence[str], detail: Any, *, scope: str | None) -> str:
    """The retry for a render the caller's read-shaping values broke.

    For example ``"`fields` was rejected while rendering the result: Unknown
    field `items`."`` Names the argument first, because the detail alone -- the
    presenter's own wording -- says nothing about *which* argument the model has to
    change, and "while rendering the result" tells it the rest of the call was
    accepted.

    **Several supplied names are all named, joined with "or".** The presenter
    does not say which one it refused, so singling one out would be a guess
    presented as a fact, and "and ... were" would claim all of them were. The
    MCP transport words it the same way.

    On a paged tool the scope sentence follows, because the likeliest way to
    write a bad selection there is against the page envelope, which is exactly
    the shape the tool's result documents -- saying so turns that into one retry.
    ``scope`` is that sentence as the conventions word it, ``None`` on a tool that
    does not page or where the conventions dropped it.
    """
    quoted = [f"`{name}`" for name in names]
    subject = quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} or {quoted[-1]}"
    message = (
        f"{subject} was rejected while rendering the result: {_format_validation_detail(detail)}"
    )
    # A refusal's message may or may not carry a full stop; the sentence should
    # end in exactly one either way.
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
    """Strip + validate the list-selector args this toolset owns.

    The tool schema advertises ``page`` / ``limit`` as integers, but the
    toolset's argument validator is a no-op (the schema is advisory), so a model
    that sends ``limit="2"`` reaches here untyped. Coerce and validate rather
    than letting a ``TypeError`` abort the run, mapping a bad value to
    ``ModelRetry``.

    **The coercion stays here rather than moving to a shared shaper taking
    both values already parsed.** That is the one place the two agent
    transports legitimately differ: an MCP server answering a public endpoint has
    to clamp a malformed value and serve *something*, while an in-process toolset
    can hand the model its own mistake back and get a corrected call. Clamping is
    about a page's bounds; this is about bad input, and only the second is a
    policy.

    Clamping ``limit`` to ``max_page_size`` is therefore **not** done here:
    ``paginate_output(max_page_size=…)`` applies it at the slice, where the
    ``totalPages`` the caller is told about is computed from the same number, so
    there is one place for that number to be.

    **``ordering`` is left entirely alone for a spec that advertises it**, so the
    value reaches the ``filter_set`` — or the selector callable — that declared
    it. Nothing sorts from here. For a spec that advertises none, the argument is
    popped and refused: it was never offered, so a model that sent one is told
    that rather than having the value fall through to the spec and come back as a
    generic unknown-argument rejection.
    """
    if not _is_list_selector(spec):
        return None
    # Popped in a fixed order (limit, ordering, page) so that a call carrying two
    # bad arguments is corrected on the same one every time.
    limit: int | None = _coerce_positive_int(args.pop("limit", None), "limit")
    if _spec_ordering_argument(spec, pool_seeds=pool_seeds) is None:
        _refuse_unadvertised_ordering(args.pop("ordering", None))
    return _PageArgs(page=_coerce_positive_int(args.pop("page", None), "page"), limit=limit)


_STANDARD_CONTEXT_KEYS = ("request", "view", "format")
"""The entries every call's context carries, ``None`` where the run put none."""

_CALL_SEEDS = frozenset({"request", "view"})
"""The seeds each call registers itself, from the call's context."""


def _call_context(ctx: RunContext[Any], tool_name: str) -> dict[str, Any]:
    """This call's context: a copy of the run's, with the tool's name under ``action``.

    The run's is ``ctx.deps.call_context``, read with ``getattr`` as
    ``progress`` is, so a project's own deps type need not declare it. A copy
    rather than the run's own dict, because two calls of one run may run at
    once and each carries its own ``action``: the run's entries reach the call
    as the same objects, and what a stage writes stays in its call
    (``test_each_call_of_a_run_is_handed_its_own_copy_of_the_run_s_context``).
    The tool's name is the call's ``action``, which a permission check
    declaring ``call_context`` reads there
    (``test_the_tool_name_is_the_action_a_permission_check_reads``), and
    ``request``, ``view`` and ``format`` are present on every call, ``None``
    where the run put none, so a callable reading one finds the key
    (``test_the_three_standard_entries_are_none_where_the_run_put_none``).
    """
    context: dict[str, Any] = {
        **(getattr(getattr(ctx, "deps", None), "call_context", None) or {}),
        "action": tool_name,
    }
    for key in _STANDARD_CONTEXT_KEYS:
        context.setdefault(key, None)
    return context


def _with_call_seeds(
    pool_seeds: PoolSeeds, call_context: Mapping[str, Any], seeds: Mapping[str, Callable[..., Any]]
) -> PoolSeeds:
    """The mount's seeds, with this call's ``request``, ``view`` and own ``seeds`` beside them.

    The kernel's base pool holds no ``request`` and no ``view``, while spec
    callables written for HTTP declare both, so each call registers the two its
    context carries, ``None`` where the run put none; a callable declaring one
    then binds, reading ``None`` as none. ``seeds`` is what
    [`call_seeds`][django_spec_agents.spec_tools.SpecToolset.call_seeds]
    returned for the call, laid over those two, so a hook returning a ``view``
    is the view the call carries.

    Raises:
        ImproperlyConfigured: the mount registers ``request`` or ``view``
            itself. Its resolver could never be the one a call reads, and the
            kernel's own refusal would say the name was registered twice
            (``test_a_mount_registering_a_seed_the_call_supplies_is_refused``).
        ValueError: ``seeds`` names a seed the mount registers, which
            ``PoolSeeds.extend`` refuses rather than picking a winner
            (``test_a_hook_seed_the_mount_already_registers_is_refused``).
    """
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
    """Read the call's arguments under the spec's declared reads as the tool's schema advertises them.

    In place. Over ``_declared_reads``: every read the spec declares, in each
    seat a caller's argument reaches, and its shapes. A read reflected off a
    callable's signature is the callable's own, and its default is the
    callable's to apply.

    A ``null`` under a declared read that cannot be null is the model declining
    to fill it, so it is dropped rather than handed to the kernel, whose check
    would refuse it: for a shape
    (``test_an_explicit_null_shape_is_treated_as_omitted``) and for a read
    a callable takes alike
    (``test_an_explicit_null_for_a_read_a_callable_takes_is_treated_as_omitted``).
    A read declared ``nullable`` admits it, so a ``null`` sent there is a value
    and stays (``test_a_native_read_s_null_is_a_value_where_it_is_nullable``).
    One arc to coverage: ``value is None`` is held by every case sending a
    value, ``not nullable`` by the ``null-is-a-value`` case.

    A read left out is then given its declared default, as the schema, which
    is the compiled spec's, advertises it. The kernel reports a shape's
    default without applying it, and hands a read a callable takes nothing it
    was not sent, so with nobody filling it the presenter or the callable would
    be handed none (the ``default-seeded-when-omitted`` case of
    ``test_a_shape_reaches_the_presenter_under_its_default_when_left_out``,
    ``test_a_read_a_kwargs_selector_takes_is_its_argument_with_its_default``,
    ``test_a_read_a_selector_parameter_takes_is_handed_the_default_it_advertises``
    and ``test_a_read_s_default_answers_for_the_parameter_it_fills``). A value the
    call carries is never replaced
    (``test_a_read_s_default_never_replaces_a_value_the_model_sent``).
    ``UNSET`` declares none, and a default of ``None`` is no value, as a
    ``null`` sent is none: one arc to coverage, and each conjunct is held by
    its own case of ``test_a_read_or_shape_declaring_no_default_contributes_nothing``.
    """
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
    """The call's arguments: the model's, less the ones the server answers.

    The model's own argument is dropped when the spec declares it and
    ``owned``, the kernel's ``server_owned_keys`` for the call's seeds, lists
    it: a seed's name, so the seed answers instead of ``REJECT`` refusing it
    (``test_a_registered_seed_outranks_a_model_argument_of_the_same_name``), or
    a key a typed provider fills, whose value from the model was never the one
    the call read
    (``test_a_provider_filling_the_name_it_reads_is_not_handed_the_model_s_value``).
    ``owned`` leaves out a top-level Validator field named like a seed, which
    the model sends and dispatch validates into ``data`` while the seed keeps
    its own value
    (``test_a_validator_field_named_like_a_seed_is_the_model_s_to_send``). An
    owned name the spec never declares is left for the kernel to answer
    (``test_a_seed_named_argument_a_spec_never_declares_is_refused``).
    """
    arguments = {
        name: value for name, value in args.items() if not (name in declared and name in owned)
    }
    return arguments


def _missing_arguments(names: Sequence[str], conventions: AgentConventions) -> ModelRetry:
    """The retry for a call that left out arguments it cannot run without.

    One sentence for every check that asks -- a parameter the tool's schema
    requires, wherever the spec declares it -- so the model reads the same
    wording whichever it omitted, worded by ``conventions.missing_arguments``. Sorted, so
    a message naming several reads the same way on every call (the ``several``
    case of ``test_a_selector_call_missing_a_required_argument_is_handed_back``).
    The field cannot be ``None`` or blank: a retry has to say something.
    """
    return ModelRetry(conventions.missing_arguments.format(names=_listed(sorted(names))))


def _coerce_positive_int(value: Any, name: str) -> int | None:
    """Coerce a pagination arg to a positive int; ``ModelRetry`` on anything else.

    Accepts an ``int`` or an all-digit ``str`` (``"2"``); rejects booleans,
    floats, negatives, zero, and non-numeric strings.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass — never a valid count
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
    """``ModelRetry`` when a model sends a sort to a tool that advertises none.

    Reached only for a list selector whose reflected schema carries no sort
    argument — a spec that advertises one keeps its value, which travels on to
    whatever declared it. So the argument was genuinely never offered and the
    model invented it, and saying exactly that is more use than the generic
    unknown-argument rejection the value would otherwise collect at dispatch.

    **A deliberate divergence from the MCP transport**, which silently ignores an
    ordering value it does not recognise. For a model that is the worst outcome
    available: it asked for newest-first, received insertion order, and has no
    way to find out.
    """
    if value is not None:
        raise ModelRetry("This tool does not accept an `ordering` argument; omit it.")


def _shape_list(
    value: Any, *, page: int | None, limit: int | None, max_page_size: int | None
) -> OutputPage:
    """Paginate a list selector's queryset with ``paginate_output``, then force it to evaluate.

    Forced (``list(...)``) so that *nothing downstream* holds a lazy queryset
    once this returns: a presenter re-evaluating one would run the query a
    second time, and the envelope's counts and its rows could then come from two
    different reads. ``replace`` rather than mutation because ``OutputPage`` is
    frozen, and here rather than at the call site because the guarantee belongs
    to whatever produces the page.
    """
    shaped: OutputPage = paginate_output(
        value,
        page=page,
        limit=limit,
        max_page_size=max_page_size,
    )
    return replace(shaped, items=list(shaped.items))


__all__ = ["SpecToolset"]
