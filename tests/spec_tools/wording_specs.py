"""The specs the two wording suites run against, defined once.

``test_default_wording.py`` pins what each default says and
``test_spec_toolset_conventions.py`` changes it one field at a time. Both need
the same selectors, serializers and run context, so a line of wording lands on
the same tool in each.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import django_filters
from django_service_specs import FieldMarking, SelectorKind
from pydantic_ai.usage import RunUsage
from rest_framework import serializers
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import AllowAny
from rest_framework_services import MARKING, SelectorSpec

from django_spec_agents import AgentDeps
from tests.spec_tools.testapp.models import Widget


def run_context() -> Any:
    """The fields of a live ``RunContext`` the toolset reads."""
    return SimpleNamespace(
        deps=AgentDeps(user=None),
        run_id="run-1",
        conversation_id="conv-1",
        run_step=1,
        tool_call_id="call-1",
        usage=RunUsage(),
    )


class SortedWidgets(django_filters.FilterSet):
    ordering = django_filters.OrderingFilter(fields=(("name", "name"),))

    class Meta:
        model = Widget
        fields = []


class HandledWidget(serializers.ModelSerializer):
    """An ``id`` marked as a handle with no wording of its own, so the fallback answers."""

    class Meta:
        model = Widget
        fields = ["id", "name"]
        extra_kwargs = {"id": {"style": {MARKING: FieldMarking.handle()}}}


class Picky(serializers.Serializer):
    """A row that refuses any ``fields`` selection, in its own words."""

    name = serializers.CharField()

    def to_representation(self, instance: Any) -> Any:
        if self.context["request"].query_params.get("fields"):
            raise DRFValidationError("Unknown field `items`.")
        return super().to_representation(instance)


def rows() -> list[dict[str, str]]:
    """Two plain rows, so the render runs without a database.

    It takes no argument, so a ``fields`` the tool reads is a shape: drf-services
    compiles a ``QueryParam`` no callable names into the spec's ``shapes``, which
    reach the presenter alone. A bare ``**kwargs`` here would take it as the
    selector's argument instead, and ``Picky`` would never see it.
    """
    return [{"name": "a"}, {"name": "b"}]


def widgets(**_: Any) -> Any:
    """A queryset, which a ``filter_set`` requires; never evaluated here."""
    return Widget.objects.none()


def one(*, pk: int) -> Widget:
    """A lookup with no default for ``pk``."""
    return Widget(pk=pk, name="w")


def approve(**_: Any) -> dict[str, bool]:
    """Approve the pending invoices."""
    return {"approved": True}


def selector_specs() -> dict[str, Any]:
    """The three selectors that turn on the pagination, ordering and handle lines."""
    return {
        "list_widgets": SelectorSpec(
            kind=SelectorKind.LIST,
            selector=rows,
            output_serializer=Picky,
            permission_classes=[AllowAny],
        ),
        "sorted_widgets": SelectorSpec(
            kind=SelectorKind.LIST,
            selector=widgets,
            output_serializer=Picky,
            filter_set=SortedWidgets,
            permission_classes=[AllowAny],
        ),
        "get_widget": SelectorSpec(
            kind=SelectorKind.RETRIEVE,
            selector=one,
            output_serializer=HandledWidget,
            permission_classes=[AllowAny],
        ),
    }
