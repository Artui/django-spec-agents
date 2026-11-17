from __future__ import annotations

from importlib.resources import files

from django_spec_agents.skills import parse_skill_md


def test_the_bundled_create_skill_parses_and_calls_the_create_tool() -> None:
    text = files("django_spec_agents.skills").joinpath("bundled/create-skill/SKILL.md").read_text()

    draft = parse_skill_md(text)

    assert draft.name == "create-skill"
    assert draft.model_invocable
    assert "`create_skill`" in draft.body
    assert "`list_skills`" in draft.body
