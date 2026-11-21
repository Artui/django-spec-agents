from __future__ import annotations

from typing import Protocol


class SkillScanResult(Protocol):
    """What one scan found: upstream's `ArtifactScanResult` has this shape."""

    @property
    def flagged(self) -> bool:
        """Whether the scanned text matched any pattern."""
        ...

    def summary(self) -> str:
        """The threats and pattern names found, without quoting the scanned text."""
        ...


__all__ = ["SkillScanResult"]
