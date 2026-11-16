from __future__ import annotations

import json

from django_spec_agents.skills.invalid_skill_error import InvalidSkillError
from django_spec_agents.skills.types.skill_draft import SkillDraft
from django_spec_agents.skills.utils import clean_draft, strip_blank_edges

_FIELDS = frozenset({"name", "description", "user-invocable", "disable-model-invocation"})
_PLAIN_INDICATORS = tuple("|>[]{}&*!%@`'\"#,?:-")


def _scalar(key: str, raw: str) -> str:
    """One frontmatter value: a JSON-style double-quoted, a single-quoted or a plain scalar."""
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise InvalidSkillError(f"The {key!r} value is not a closed quoted string.") from error
        return value
    if raw.startswith("'") and raw.endswith("'") and len(raw) > 1:
        inner = raw[1:-1]
        if "'" in inner.replace("''", ""):
            raise InvalidSkillError(f"The {key!r} value has an unescaped single quote.")
        return inner.replace("''", "'")
    if not raw or raw.startswith(_PLAIN_INDICATORS) or ": " in raw or " #" in raw:
        raise InvalidSkillError(
            f"The {key!r} value needs quotes: SKILL.md import reads one-line scalars only."
        )
    return raw


def _flag(key: str, raw: str) -> bool:
    """A YAML 1.2 boolean, the only spellings the harness accepts for a flag."""
    value = _scalar(key, raw).lower()
    if value not in ("true", "false"):
        raise InvalidSkillError(f"The {key!r} value must be true or false.")
    return value == "true"


def _frontmatter(lines: list[str]) -> dict[str, str]:
    """The raw `key: value` pairs between the delimiters; one-line entries only."""
    fields: dict[str, str] = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, raw = line.partition(":")
        if line[0].isspace() or not separator:
            raise InvalidSkillError(
                "SKILL.md import reads one-line `key: value` frontmatter entries only."
            )
        if key not in _FIELDS:
            raise InvalidSkillError(f"SKILL.md frontmatter field {key!r} is not supported.")
        if key in fields:
            raise InvalidSkillError(f"SKILL.md frontmatter repeats {key!r}.")
        fields[key] = raw.strip()
    return fields


def parse_skill_md(text: str) -> SkillDraft:
    """Read a `SKILL.md` into a cleaned `SkillDraft`; raises `InvalidSkillError` if it cannot.

    The frontmatter is a strict subset of YAML: one-line entries for `name`, `description`,
    `user-invocable` and `disable-model-invocation`, and nothing else, so no field is dropped.
    """
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise InvalidSkillError("A SKILL.md starts with a `---` frontmatter line.")
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line == "---"), None)
    if closing is None:
        raise InvalidSkillError("The SKILL.md frontmatter has no closing `---` line.")
    fields = _frontmatter(lines[1:closing])
    for required in ("name", "description"):
        if required not in fields:
            raise InvalidSkillError(f"The SKILL.md frontmatter has no {required!r}.")
    return clean_draft(
        SkillDraft(
            name=_scalar("name", fields["name"]),
            description=_scalar("description", fields["description"]),
            body=strip_blank_edges("\n".join(lines[closing + 1 :])),
            user_invocable=_flag("user-invocable", fields.get("user-invocable", "true")),
            model_invocable=not _flag(
                "disable-model-invocation", fields.get("disable-model-invocation", "false")
            ),
        )
    )
