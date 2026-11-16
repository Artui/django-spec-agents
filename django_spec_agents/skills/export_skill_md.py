from __future__ import annotations

import json
import re

from django_spec_agents.skills.abstract_skill import AbstractSkill

# Characters YAML may not hold raw, or folds as a line break, in a double-quoted scalar.
_NOT_YAML_PRINTABLE = re.compile("[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]")


def _quoted(value: str) -> str:
    """`value` as a JSON string, which is a YAML double-quoted scalar once the rest are escaped."""
    text = json.dumps(value, ensure_ascii=False)
    return _NOT_YAML_PRINTABLE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def export_skill_md(skill: AbstractSkill) -> str:
    """`skill` as a `SKILL.md`, the four frontmatter fields the harness reads and the body.

    `parse_skill_md` reads it back to the same fields, and the harness's `Skills` loads it from
    a directory named after the skill. `enabled` and the scope are not part of the file.
    """
    return (
        "---\n"
        f"name: {_quoted(skill.name)}\n"
        f"description: {_quoted(skill.description)}\n"
        f"user-invocable: {'true' if skill.user_invocable else 'false'}\n"
        f"disable-model-invocation: {'false' if skill.model_invocable else 'true'}\n"
        "---\n"
        "\n"
        f"{skill.body}\n"
    )
