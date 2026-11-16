# User skills

A user skill is a named set of model instructions that a person writes and keeps in the
database, rather than a `SKILL.md` file shipped with the code. The package gives you
the model's fields, a policy protocol, and functions that read and write skills through
that policy. You bring the concrete model, its tenant relation, its migration and the
policy. No concrete skill model ships in the package, and no default policy.

This page covers the storage and the rules. Offering stored skills to the agent and to
the browser's palette is a transport's job.

## The model

Subclass `AbstractSkill` and add whatever scopes a skill in your project. That can be an
organisation, a workspace, or the user alone:

```python
from django.conf import settings
from django.db import models
from django.db.models import F, Q

from django_spec_agents.skills import AbstractSkill, SkillPolicy


class Skill(AbstractSkill):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE
    )

    @classmethod
    def skill_policy(cls) -> SkillPolicy["Skill"]:
        return PersonalFirstPolicy()


class PersonalFirstPolicy:
    def visible(self, request):
        return Skill.objects.filter(Q(owner=request.user) | Q(owner=None)).order_by(
            F("owner").asc(nulls_last=True), "pk"
        )

    def can_write(self, request, skill):
        if skill.owner_id is None:
            return request.user.is_staff
        return skill.owner_id == request.user.pk

    def new_skill(self, request):
        return Skill(owner=None if request.user.is_staff else request.user)
```

`AbstractSkill` holds the fields every consumer needs:

| Field | Meaning |
| --- | --- |
| `name` | The kebab-case `/token` the palette and the agent share, at most 64 characters |
| `description` | What the skill is for, at most 1024 characters |
| `body` | The instructions, at most 20,000 characters |
| `user_invocable` | Whether a person may pick it; SKILL.md's `user-invocable` |
| `model_invocable` | Whether the model may load it; the negation of SKILL.md's `disable-model-invocation` |
| `enabled` | Off means it neither shows nor shadows |
| `created_by`, `created_at`, `updated_at` | Who wrote it, and when |

The package puts no uniqueness constraint on `name`, because two scopes may each hold
a skill of the same name. Add the constraint that fits your scopes, such as one name per
owner.

## The policy

`SkillPolicy` is three methods, and every function below goes through them:

- `visible(request)` returns every skill the caller may see, disabled ones too, in
  precedence order. The first skill of a name wins, so ordering personal skills before
  company ones lets a personal skill shadow a company one.
- `can_write(request, skill)` decides whether the caller may save `skill`, which is
  either a new unsaved one or an existing row.
- `new_skill(request)` returns the unsaved skill a create writes to, with its scope
  already set.

## Reading and writing

Every function takes the policy and the request:

```python
from django_spec_agents.skills import SkillDraft, create_skill, list_visible_skills

policy = Skill.skill_policy()
create_skill(policy, request, SkillDraft("triage", "Sort the inbox.", "Read, then sort."))
list_visible_skills(policy, request)
```

- `list_visible_skills` returns the caller's enabled skills, one per name.
  `resolve_skill` returns the one a name reaches, or `None`. A disabled skill shadows
  nothing, so disabling a personal skill brings back the company one of the same name.
- `create_skill`, `update_skill` and `disable_skill` write. Each one asks `can_write`
  and raises `SkillWriteRefusedError` when the policy refuses. That error is a Django
  `PermissionDenied`. A create asks about the new skill. An update and a disable ask
  twice, once about the row as stored and once about the result.
- A name that is not kebab-case raises `InvalidSkillError`. So do a blank or oversized
  description or body, and a create by a caller who already sees `MAX_VISIBLE_SKILLS`
  (100) enabled skills. The count cap is measured on what the caller sees, because that
  is the catalog that reaches the model.

Descriptions are stripped, and bodies lose their leading and trailing blank lines and
take LF line endings. The harness's `Skills` loads a `SKILL.md` the same way.

## SKILL.md

`export_skill_md(skill)` writes a skill as a `SKILL.md`. `import_skill_md(policy,
request, text)` creates one from a `SKILL.md`, through `create_skill` and so through
`can_write`. `parse_skill_md(text)` only reads it, into a `SkillDraft`. The trip is
lossless: an exported skill imports to the same name, description, body and flags. The
harness's `Skills` loads the exported file to the same fields too, from a directory
named after the skill. `enabled` and the scope are not part of the file.

The import reads a strict subset of YAML: one-line entries for `name`, `description`,
`user-invocable` and `disable-model-invocation`. A value may be plain, single-quoted or
double-quoted. Any other field, such as `allowed-tools`, raises `InvalidSkillError`, and
so does a multi-line value. Dropping either one silently would lose part of the file.

## Refusing a model with no policy at startup

Name your model in the `SPEC_AGENTS_SKILL_MODEL` setting, and add the package's
opt-in app to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...,
    "django_spec_agents",
    "myproject.skills",
]

SPEC_AGENTS_SKILL_MODEL = "skills.Skill"
```

The app registers Django system checks, so `manage.py check`, `runserver` and `migrate`
fail when the configuration is broken:

| Id | When |
| --- | --- |
| `django_spec_agents.E001` | The setting names no installed model, or names one that does not subclass `AbstractSkill` |
| `django_spec_agents.E002` | The model's `skill_policy()` returns `None`, which is `AbstractSkill`'s default |

The app has no models and no migrations. Its label, `django_spec_agents`, does not clash
with the store app's label, `django_spec_agents_store`. A project that configures no
skills leaves the app out, and nothing changes for it. `get_skill_model()` returns the
configured model, or `None` when the setting is unset, so a transport can find the
policy as `get_skill_model().skill_policy()`.

Import `django_spec_agents.skills` once the app registry is ready, for example from a
`models.py`, a view or a policy, because it defines an abstract model.
