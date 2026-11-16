from __future__ import annotations

from typing import TypeVar

from django_spec_agents.skills.abstract_skill import AbstractSkill

SkillT = TypeVar("SkillT", bound=AbstractSkill)
