from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from django import forms
from django_service_specs import (
    FormValidator,
    Output,
    OutputField,
    Parameter,
    Parameters,
    PermissionCheck,
    PresentationContext,
    Presenter,
    SpecRegistry,
    Unrestricted,
)


def allow() -> list[Unrestricted]:
    """The permission list of a spec any caller may call."""
    return [Unrestricted()]


class RowPresenter(Presenter):
    """Renders a row, a mapping or an object, as the fields it declares."""

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


def widget_presenter() -> RowPresenter:
    """A widget row as its ``id``, ``name`` and ``price``."""
    return RowPresenter(
        OutputField("id", "integer"),
        OutputField("name", "string"),
        OutputField("price", "integer"),
    )


class WidgetInputForm(forms.Form):
    """A widget's writable fields, with the bounds a write is refused past."""

    name = forms.CharField(max_length=100)
    price = forms.IntegerField(min_value=0)


def widget_input() -> FormValidator:
    """The Validator of a widget write: ``name`` and a non-negative ``price``."""
    return FormValidator(WidgetInputForm)


class DenyAll(PermissionCheck):
    """Refuses every principal."""

    def has_permission(self, principal: Any, spec: Any) -> bool:
        return False


def shaping(specs: Mapping[str, Any], shapes: Mapping[str, Sequence[Parameter]]) -> SpecRegistry:
    """Each tool ``shapes`` names declares those shapes on its spec, after any it declares."""
    registry = SpecRegistry()
    for name, spec in specs.items():
        extra = tuple(shapes.get(name, ()))
        registry.register(
            name, replace(spec, shapes=Parameters.of(*spec.shapes, *extra)) if extra else spec
        )
    return registry
