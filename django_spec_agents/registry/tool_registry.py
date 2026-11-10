from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from django_spec_agents.registry.build_input_schema import build_input_schema
from django_spec_agents.registry.types.tool_binding import ToolBinding
from django_spec_agents.registry.types.tool_spec import ToolSpec
from django_spec_agents.registry.utils import run_context_parameter
from django_spec_agents.types.schema_strategy import SchemaStrategy


class ToolRegistry:
    """An ordered, named collection of server-side tools."""

    def __init__(self, *, schema_strategy: SchemaStrategy = SchemaStrategy.HAND_BUILT) -> None:
        self._bindings: dict[str, ToolBinding] = {}
        self._schema_strategy = SchemaStrategy(schema_strategy)

    def register(self, spec: ToolSpec) -> ToolBinding:
        """Register ``spec`` and return its binding."""
        if spec.name in self._bindings:
            raise ValueError(f"tool {spec.name!r} already registered")
        strategy = self._schema_strategy if spec.schema_strategy is None else spec.schema_strategy
        binding = ToolBinding(
            spec=spec,
            input_schema=build_input_schema(
                spec.fn,
                destructive=spec.destructive,
                category=spec.category,
                confirm=spec.confirm,
                summary=spec.summary,
                schema_strategy=strategy,
            ),
        )
        self._bindings[spec.name] = binding
        return binding

    def __iter__(self) -> Iterator[ToolBinding]:
        return iter(self._bindings.values())

    def __len__(self) -> int:
        return len(self._bindings)

    def __contains__(self, name: object) -> bool:
        return name in self._bindings

    def get(self, name: str) -> ToolBinding:
        """Return the binding for ``name`` or raise ``KeyError``."""
        try:
            return self._bindings[name]
        except KeyError as e:
            raise KeyError(f"tool {name!r} is not registered") from e

    def call(self, name: str, arguments: dict[str, Any], *, ctx: Any = None) -> Any:
        """Dispatch a sync call to the registered tool."""
        fn = self.get(name).spec.fn
        if inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"tool {name!r} is async; use ToolRegistry.acall instead",
            )
        return _invoke(fn, name, arguments, ctx)

    async def acall(self, name: str, arguments: dict[str, Any], *, ctx: Any = None) -> Any:
        """Dispatch an async call; transparently awaits sync callables."""
        fn = self.get(name).spec.fn
        if inspect.iscoroutinefunction(fn):
            return await _ainvoke(fn, name, arguments, ctx)
        return _invoke(fn, name, arguments, ctx)


def _bind(
    fn: Callable[..., Any], name: str, arguments: dict[str, Any], ctx: Any
) -> inspect.BoundArguments:
    """Bind the model's ``arguments``, adding the run context the model cannot send."""
    sig = inspect.signature(fn, eval_str=True)
    context_parameter = run_context_parameter(sig)
    if context_parameter is not None:
        if ctx is None:
            raise TypeError(
                f"tool {name!r} takes a RunContext as its {context_parameter!r} "
                "parameter; pass ctx= to dispatch it directly."
            )
        arguments = {**arguments, context_parameter: ctx}
    bound = sig.bind(**arguments)
    for param in sig.parameters.values():
        if param.name not in bound.arguments and isinstance(param.default, FieldInfo):
            value = param.default.get_default(call_default_factory=True)
            if value is PydanticUndefined:
                raise TypeError(f"tool {name!r} missing a required argument: {param.name!r}")
            bound.arguments[param.name] = value
    bound.apply_defaults()
    return bound


def _invoke(fn: Callable[..., Any], name: str, arguments: dict[str, Any], ctx: Any) -> Any:
    bound = _bind(fn, name, arguments, ctx)
    return fn(*bound.args, **bound.kwargs)


async def _ainvoke(fn: Callable[..., Any], name: str, arguments: dict[str, Any], ctx: Any) -> Any:
    bound = _bind(fn, name, arguments, ctx)
    return await fn(*bound.args, **bound.kwargs)


__all__ = ["ToolRegistry"]
