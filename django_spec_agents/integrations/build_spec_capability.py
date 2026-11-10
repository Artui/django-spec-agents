from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django_spec_agents.integrations.resolve_spec_mapping import resolve_spec_mapping
from django_spec_agents.integrations.types.spec_source import SpecSource


def build_spec_capability(
    specs: Mapping[str, Any] | SpecSource,
    *,
    exclude_names: frozenset[str] = frozenset(),
) -> Any:
    """A ``SpecCapability`` over ``specs``, acting as the run's ``deps.user``."""
    from django_spec_agents.spec_tools import SpecCapability

    resolved = resolve_spec_mapping(specs)
    selected = [name for name in resolved if name not in exclude_names]
    subset = getattr(specs, "subset", None)
    if subset is not None:
        return SpecCapability(subset(*selected))
    return SpecCapability({name: resolved[name] for name in selected})


__all__ = ["build_spec_capability"]
