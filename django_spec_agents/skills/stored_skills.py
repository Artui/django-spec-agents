from __future__ import annotations

from functools import cache
from importlib.resources import files
from typing import Any

from asgiref.sync import sync_to_async
from django.http import HttpRequest
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, Capability, CombinedCapability

from django_spec_agents.skills.build_skill_tools import build_skill_tools
from django_spec_agents.skills.list_visible_skills import list_visible_skills
from django_spec_agents.skills.parse_skill_md import parse_skill_md
from django_spec_agents.skills.skill_capability import SkillCapability
from django_spec_agents.skills.types.create_skill_name import CREATE_SKILL_NAME
from django_spec_agents.skills.types.skill_draft import SkillDraft
from django_spec_agents.skills.types.skill_policy import SkillPolicy


class StoredSkills(AbstractCapability[Any]):
    """The caller's stored skills, one deferred capability each, read per run: pass it in a
    run's `capabilities=`. With `authoring=True` the run also gets `build_skill_tools` and the
    bundled `create-skill` skill, which a visible stored skill of that name shadows."""

    def __init__(
        self, policy: SkillPolicy[Any], request: HttpRequest, *, authoring: bool = False
    ) -> None:
        self.policy = policy
        self.request = request
        self.authoring = authoring

    async def for_run(self, ctx: RunContext[Any]) -> AbstractCapability[Any]:
        """One deferred capability per enabled, model-invocable skill, plus authoring if asked."""
        skills = await sync_to_async(list_visible_skills, thread_sensitive=True)(
            self.policy, self.request
        )
        offered: list[AbstractCapability[Any]] = [
            SkillCapability(skill.name, skill.description, skill.body)
            for skill in skills
            if skill.model_invocable
        ]
        if self.authoring:
            if all(skill.name != CREATE_SKILL_NAME for skill in skills):
                draft = _bundled_create_skill()
                offered.append(SkillCapability(draft.name, draft.description, draft.body))
            offered.append(Capability(toolsets=[build_skill_tools(self.policy, self.request)]))
        return CombinedCapability(offered) if offered else self


@cache
def _bundled_create_skill() -> SkillDraft:
    text = (
        files("django_spec_agents.skills")
        .joinpath("bundled", CREATE_SKILL_NAME, "SKILL.md")
        .read_text()
    )
    return parse_skill_md(text)
