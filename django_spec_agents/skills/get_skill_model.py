from __future__ import annotations

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from django_spec_agents.skills.abstract_skill import AbstractSkill
from django_spec_agents.skills.types.skill_model_setting import SKILL_MODEL_SETTING


def get_skill_model() -> type[AbstractSkill] | None:
    """The model `SPEC_AGENTS_SKILL_MODEL` names (`"app_label.ModelName"`), `None` when unset.

    Raises `ImproperlyConfigured` when the setting names no installed `AbstractSkill` subclass.
    """
    label = getattr(settings, SKILL_MODEL_SETTING, None)
    if label is None:
        return None
    try:
        model = apps.get_model(label)
    except (AttributeError, LookupError, ValueError) as error:
        raise ImproperlyConfigured(
            f"{SKILL_MODEL_SETTING} must be 'app_label.ModelName' of an installed model, "
            f"not {label!r}."
        ) from error
    if not issubclass(model, AbstractSkill):
        raise ImproperlyConfigured(
            f"{SKILL_MODEL_SETTING} names {label!r}, which does not subclass AbstractSkill."
        )
    return model
