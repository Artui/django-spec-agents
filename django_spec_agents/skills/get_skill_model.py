from __future__ import annotations

from typing import cast

from django_spec_agents.apps import SpecAgentsConfig
from django_spec_agents.skills.abstract_skill import AbstractSkill


def get_skill_model() -> type[AbstractSkill] | None:
    """The model `register_skill_model` registered, or `None` when there is none."""
    config = SpecAgentsConfig.installed()
    if config is None:
        return None
    return cast("type[AbstractSkill] | None", config.registered_models.get(AbstractSkill))
