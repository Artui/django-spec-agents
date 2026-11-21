from __future__ import annotations

from typing import Protocol

from django_spec_agents.skills.types.skill_scan_result import SkillScanResult


class SkillScanner(Protocol):
    """Scans a skill's text before it is stored or offered: upstream's `ArtifactScanner`."""

    def scan(self, content: str, *, source: str = "") -> SkillScanResult:
        """Scan `content`; `source` labels it, as `skill:<name>:<field>`."""
        ...


__all__ = ["SkillScanner"]
