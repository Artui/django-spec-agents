from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillDraft:
    """What a create, an update or a `SKILL.md` import writes to a skill.

    `model_invocable` is the negation of SKILL.md's `disable-model-invocation`.
    """

    name: str
    description: str
    body: str
    user_invocable: bool = True
    model_invocable: bool = True
