from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SpecSource(Protocol):
    """Anything that can hand back a ``name -> spec`` mapping."""

    def specs(self) -> dict[str, Any]:
        """The ``name -> spec`` mapping this source declares."""
        ...


__all__ = ["SpecSource"]
