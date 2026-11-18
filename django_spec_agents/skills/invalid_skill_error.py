from __future__ import annotations


class InvalidSkillError(ValueError):
    """A skill or a `SKILL.md` that breaks a rule; `field` names the draft field that broke it."""

    def __init__(self, *args: object, field: str | None = None) -> None:
        super().__init__(*args)
        self.field = field
