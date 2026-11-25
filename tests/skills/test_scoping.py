from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import get_args

from django.contrib.auth.models import AbstractBaseUser
from django.http import HttpRequest

from django_spec_agents.skills import (
    SkillDraft,
    SkillScope,
    SkillScoping,
    build_skill_tools,
    create_skill,
    list_visible_skills,
)
from tests.skills.conftest import request_as
from tests.skills.testapp.models import PersonalFirstPolicy, TenantSkill

_ROOT = Path(__file__).resolve().parents[2]

_POLICIES = """\
from __future__ import annotations

from collections.abc import Iterable

from django.http import HttpRequest

from django_spec_agents.skills import AbstractSkill, SkillPolicy, SkillScope, SkillScoping


class Unscoped:
    def visible(self, request: HttpRequest) -> Iterable[AbstractSkill]:
        return []

    def can_write(self, request: HttpRequest, skill: AbstractSkill) -> bool:
        return True

    def new_skill(self, request: HttpRequest) -> AbstractSkill:
        raise NotImplementedError


class Scoped(Unscoped):
    def scope(self, request: HttpRequest, skill: AbstractSkill) -> SkillScope:
        return "personal"


unscoped: SkillPolicy[AbstractSkill] = Unscoped()
scoped: SkillPolicy[AbstractSkill] = Scoped()
scoping: SkillScoping[AbstractSkill] = Scoped()
"""


class _ScopedPolicy(PersonalFirstPolicy):
    def scope(self, request: HttpRequest, skill: TenantSkill) -> SkillScope:
        return "personal" if skill.owner_id == request.user.pk else "shared"


def _ty(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "policies.py"
    path.write_text(source)
    command = [sys.executable, "-m", "ty", "check", "--no-progress", str(path)]
    return subprocess.run(command, cwd=_ROOT, capture_output=True, text=True, check=False)


def test_a_policy_without_scope_still_type_checks_as_a_skill_policy(tmp_path: Path) -> None:
    result = _ty(tmp_path, _POLICIES)

    assert result.returncode == 0, result.stdout + result.stderr


def test_ty_refuses_a_policy_without_scope_where_skill_scoping_is_asked(tmp_path: Path) -> None:
    result = _ty(tmp_path, _POLICIES + "missing: SkillScoping[AbstractSkill] = Unscoped()\n")

    assert result.returncode == 1
    assert "invalid-assignment" in result.stdout
    assert "missing: SkillScoping" in result.stdout


def test_a_policy_opts_in_by_defining_scope() -> None:
    assert isinstance(_ScopedPolicy(), SkillScoping)
    assert not isinstance(PersonalFirstPolicy(), SkillScoping)


def test_a_policy_without_scope_still_serves_its_skills(member: AbstractBaseUser) -> None:
    policy, request = PersonalFirstPolicy(), request_as(member)
    create_skill(policy, request, SkillDraft(name="triage", description="Sort.", body="Sort."))

    assert [s.name for s in list_visible_skills(policy, request)] == ["triage"]
    assert "list_skills" in build_skill_tools(policy, request).tools


def test_scope_labels_the_callers_own_skill_personal_and_a_company_one_shared(
    member: AbstractBaseUser, admin: AbstractBaseUser
) -> None:
    policy = _ScopedPolicy()
    draft = SkillDraft(name="triage", description="Sort.", body="Sort.")
    company = create_skill(policy, request_as(admin), draft)
    own = create_skill(policy, request_as(member), draft)

    assert policy.scope(request_as(member), own) == "personal"
    assert policy.scope(request_as(member), company) == "shared"


def test_scope_has_exactly_the_two_values() -> None:
    assert get_args(SkillScope) == ("personal", "shared")


def test_ty_refuses_a_scope_outside_the_two_values(tmp_path: Path) -> None:
    team = (
        "class Team(Unscoped):\n"
        "    def scope(self, request: HttpRequest, skill: AbstractSkill) -> SkillScope:\n"
        '        return "team"\n'
    )
    result = _ty(tmp_path, _POLICIES + team)

    assert result.returncode == 1
    assert "invalid-return-type" in result.stdout
