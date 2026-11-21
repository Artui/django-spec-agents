from __future__ import annotations

from typing import Protocol, runtime_checkable

from django_spec_agents.skills.types.skill_scanner import SkillScanner


@runtime_checkable
class SkillScanning(Protocol):
    """The optional hook a [`SkillPolicy`][django_spec_agents.skills.SkillPolicy] adds to scan
    user-authored skills, with `pydantic-ai-harness`'s `ArtifactScanner` or one of its shape.

    A policy with a ``scanner`` attribute has ``create_skill``, ``update_skill`` and
    ``import_skill_md`` refuse a draft whose description or body the scanner flags, with
    ``InvalidSkillError`` naming the field, and has a flagged stored row left out of what
    ``StoredSkills`` offers a run and what the ``list_skills`` tool lists. A policy without
    it, or whose ``scanner`` is ``None``, scans nothing.
    """

    scanner: SkillScanner | None


__all__ = ["SkillScanning"]
