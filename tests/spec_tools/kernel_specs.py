"""Kernel-native building blocks the ``spec_tools`` suites declare their specs with.

The toolset reads every spec through the kernel, so its suites declare the
kernel's own ``ServiceSpec`` and ``SelectorSpec``: a ``Presenter`` that renders
a row as the fields it declares, the permission that admits every caller, and
the registry a test of a read builds its toolset from.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from django_service_specs import (
    Output,
    OutputField,
    Parameter,
    Parameters,
    PresentationContext,
    Presenter,
    SpecRegistry,
    Unrestricted,
)


def allow() -> list[Unrestricted]:
    """The permission list of a spec any caller may call."""
    return [Unrestricted()]


class RowPresenter(Presenter):
    """Renders a row, a mapping or an object, as the fields it declares.

    ``refuse``, when given, is asked with the call's shapes before each row is
    rendered and raises whatever it raises: the seat a presenter refusing a
    read-shaping argument it cannot honour takes.
    """

    def __init__(
        self,
        *fields: OutputField,
        refuse: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._output = Output(fields)
        self._refuse = refuse

    def output(self) -> Output:
        return self._output

    def present(self, value: Any) -> Any:
        return {
            field.name: value[field.name]
            if isinstance(value, Mapping)
            else getattr(value, field.name)
            for field in self._output
        }

    def presented(self, value: Any, context: PresentationContext | None) -> Any:
        if self._refuse is not None and context is not None:
            self._refuse(context.shapes)
        return self.present(value)


def shaping(specs: Mapping[str, Any], shapes: Mapping[str, Sequence[Parameter]]) -> SpecRegistry:
    """``specs`` as a ``SpecRegistry``, each tool ``shapes`` names declaring
    those shapes on its spec, after any it declares already.

    A toolset reads a tool's offline contract off its registry entry, so a test
    of a read builds its toolset from this, as a project passing its registry
    does.
    """
    registry = SpecRegistry()
    for name, spec in specs.items():
        extra = tuple(shapes.get(name, ()))
        registry.register(
            name, replace(spec, shapes=Parameters.of(*spec.shapes, *extra)) if extra else spec
        )
    return registry
