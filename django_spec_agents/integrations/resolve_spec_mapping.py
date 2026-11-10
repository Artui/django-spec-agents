from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django_spec_agents.integrations.types.spec_source import SpecSource


def resolve_spec_mapping(specs: Mapping[str, Any] | SpecSource) -> Mapping[str, Any]:
    """Return the ``name -> spec`` mapping, whether given one or a registry."""
    return specs.specs() if isinstance(specs, SpecSource) else specs


__all__ = ["resolve_spec_mapping"]
