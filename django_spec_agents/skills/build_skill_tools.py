from __future__ import annotations

from collections.abc import Callable
from typing import Any

from asgiref.sync import sync_to_async
from django.http import HttpRequest
from pydantic_ai import ModelRetry
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets.function import FunctionToolset

from django_spec_agents.skills.create_skill import create_skill
from django_spec_agents.skills.disable_skill import disable_skill
from django_spec_agents.skills.enable_skill import enable_skill
from django_spec_agents.skills.invalid_skill_error import InvalidSkillError
from django_spec_agents.skills.list_visible_skills import list_visible_skills
from django_spec_agents.skills.resolve_skill import resolve_skill
from django_spec_agents.skills.skill_limit_error import SkillLimitError
from django_spec_agents.skills.skill_write_refused_error import SkillWriteRefusedError
from django_spec_agents.skills.types.skill_draft import SkillDraft
from django_spec_agents.skills.types.skill_policy import SkillPolicy
from django_spec_agents.skills.update_skill import update_skill
from django_spec_agents.types.destructive_metadata_key import DESTRUCTIVE_METADATA_KEY

_GATED = {DESTRUCTIVE_METADATA_KEY: True}

_LIST = (
    "List the skills this user can see, one per name, with whether they may change each. "
    "A skill's body is not listed: load the skill to read it."
)
_CREATE = (
    "Create a skill: a kebab-case name, a one-line description of when to use it, and the "
    "instructions in body. The write may need the user's approval after they read the body. "
    "user_invocable offers it in the user's palette; model_invocable offers it to you."
)
_UPDATE = (
    "Replace the description, body and flags of the enabled skill this user reaches by name. "
    "Send the whole skill, not only what changes; a skill cannot be renamed. "
    "It may need the user's approval."
)
_DISABLE = "Turn off the enabled skill this user reaches by name. It may need the user's approval."
_ENABLE = (
    "Turn back on a disabled skill this user can see, by name; where several disabled skills "
    "share the name, the first the policy yields. It may need the user's approval."
)


def build_skill_tools(policy: SkillPolicy[Any], request: HttpRequest) -> FunctionToolset[Any]:
    """Build a per-request toolset of `list_skills` and four writes the tool guard gates."""

    async def list_skills() -> list[dict[str, Any]]:
        return await sync_to_async(_listing, thread_sensitive=True)(policy, request)

    async def create(
        name: str,
        description: str,
        body: str,
        user_invocable: bool = True,
        model_invocable: bool = True,
    ) -> str:
        draft = SkillDraft(name, description, body, user_invocable, model_invocable)
        return await _write(lambda: _created(policy, request, draft))

    async def update(
        name: str,
        description: str,
        body: str,
        user_invocable: bool = True,
        model_invocable: bool = True,
    ) -> str:
        draft = SkillDraft(name, description, body, user_invocable, model_invocable)
        return await _write(lambda: _on_reached(policy, request, name, "Updated", draft))

    async def disable(name: str) -> str:
        return await _write(lambda: _on_reached(policy, request, name, "Disabled", None))

    async def enable(name: str) -> str:
        return await _write(lambda: _enabled(policy, request, name))

    return FunctionToolset(
        [
            Tool(list_skills, name="list_skills", description=_LIST),
            Tool(create, name="create_skill", description=_CREATE, metadata=_GATED),
            Tool(update, name="update_skill", description=_UPDATE, metadata=_GATED),
            Tool(disable, name="disable_skill", description=_DISABLE, metadata=_GATED),
            Tool(enable, name="enable_skill", description=_ENABLE, metadata=_GATED),
        ],
        id="django-spec-agents-skill-tools",
    )


async def _write(write: Callable[[], str]) -> str:
    """Run `write` off the event loop; a broken draft is retried, a refusal or the cap reported."""
    try:
        return await sync_to_async(write, thread_sensitive=True)()
    except (SkillLimitError, SkillWriteRefusedError) as error:
        return str(error)
    except InvalidSkillError as error:
        raise ModelRetry(str(error)) from error


def _listing(policy: SkillPolicy[Any], request: HttpRequest) -> list[dict[str, Any]]:
    return [
        {
            "name": skill.name,
            "description": skill.description,
            "user_invocable": skill.user_invocable,
            "model_invocable": skill.model_invocable,
            "writable": policy.can_write(request, skill),
        }
        for skill in list_visible_skills(policy, request)
    ]


def _created(policy: SkillPolicy[Any], request: HttpRequest, draft: SkillDraft) -> str:
    create_skill(policy, request, draft)
    return f"Created the skill {draft.name!r}."


def _on_reached(
    policy: SkillPolicy[Any],
    request: HttpRequest,
    name: str,
    verb: str,
    draft: SkillDraft | None,
) -> str:
    """Update `name` with `draft`, or disable it when there is none."""
    skill = resolve_skill(policy, request, name)
    if skill is None:
        return f"No skill named {name!r} is visible to this user."
    if draft is None:
        disable_skill(policy, request, skill)
    else:
        update_skill(policy, request, skill, draft)
    return f"{verb} the skill {name!r}."


def _enabled(policy: SkillPolicy[Any], request: HttpRequest, name: str) -> str:
    skill = next((s for s in policy.visible(request) if s.name == name and not s.enabled), None)
    if skill is None:
        return f"No disabled skill named {name!r} is visible to this user."
    enable_skill(policy, request, skill)
    return f"Enabled the skill {name!r}."
