from __future__ import annotations

from typing import Any

from pydantic_ai.capabilities import AbstractCapability


class SkillCapability(AbstractCapability[Any]):
    """One skill as a deferred capability with no tools: only its id and description show until
    the model loads it, and then its body is an instruction, never a grant."""

    def __init__(self, name: str, description: str, body: str) -> None:
        self.id = name
        self.description = description.replace("\n", "\n  ")
        self.defer_loading = True
        self.instructions = f"# Skill: {name}\n\n{body}"

    def get_instructions(self) -> str:
        return self.instructions
