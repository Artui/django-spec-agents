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

### Reserving the names of code-registered skills

A stored skill shares its `/token` with skills registered in code, such as a transport's
own. To keep a stored skill from taking one of those names, give the policy a fourth
method, `reserved_names(request)`, which returns the names no stored skill may take. A
policy that has it satisfies `ReservedSkillNames`:

```python
class TenantSkillPolicy:
    ...

    def reserved_names(self, request):
        return {"summarize", "translate"}
```

`create_skill`, `import_skill_md` and a rename through `update_skill` then raise
`InvalidSkillError` for a reserved name, so the skill tools and an editor endpoint refuse
it the same way. A skill that already holds a name reserved since it was stored can still
be edited. A policy without the method reserves nothing, and the bundled `create-skill`
is never reserved, so a stored skill may shadow it.

### Scanning what users write

A skill's description and body are text a user wrote that reaches the model. To check
them for prompt injection, credential exfiltration, hidden HTML comments and invisible
characters, give the policy a `scanner` attribute holding `pydantic-ai-harness`'s
`ArtifactScanner`. A policy that has it satisfies `SkillScanning`:

```python
from pydantic_ai_harness import ArtifactScanner


class TenantSkillPolicy:
    scanner = ArtifactScanner()
    ...
```

The scanner comes with the `[harness]` extra (`pip install "django-spec-agents[harness]"`).
The package never imports it itself: your policy does, so an install without the extra
loads nothing of the harness. Any object with the same `scan(content, *, source)` method,
`SkillScanner`, works in its place, such as an `ArtifactScanner` with `extra_patterns`.

With a scanner set:

- `create_skill`, `update_skill` and `import_skill_md` scan the cleaned description and
  body, and raise `InvalidSkillError` with `field` set to the first one flagged. The
  message names the threats and patterns found, never the flagged text, so the skill
  tools can send it back to the model to retry. A refused update leaves the row and the
  instance as they were. An update scans the new draft, not the row as stored, so it can
  repair a flagged row.
- A stored row the scanner flags is left out of what `StoredSkills` offers a run and of
  what the `list_skills` tool lists. That covers rows written before the scanner was set,
  or under older patterns. It still holds its name, so the company skill of the same name
  is not offered in its place, and the bundled `create-skill` stays hidden behind a
  flagged stored `create-skill`. `list_visible_skills` and `resolve_skill` still return
  it, so an editor shows it and `update_skill` or `disable_skill` can fix it.

A policy without the attribute, or with `scanner = None`, scans nothing.

### Labelling a skill personal or shared

A transport that lists skills for an editor can show whose each one is. To tell it, give
the policy a `scope(request, skill)` method returning `"personal"` for a skill in the
caller's own scope and `"shared"` for one another scope holds, such as a company skill. A
policy that has it satisfies `SkillScoping`, and the values are the `SkillScope` literal:

```python
class TenantSkillPolicy:
    ...

    def scope(self, request, skill):
        return "personal" if skill.owner_id == request.user.pk else "shared"
```

django-ag-ui's skill editor, from 0.91.0, carries it as each entry's `scope` key. The method is
optional, and a policy without it labels nothing: the editor then leaves the key out.
`scope` is a protocol of its own rather than a fourth `SkillPolicy` method because a
protocol has no optional members, so a policy written before it still satisfies
`SkillPolicy` under a type checker. Nothing in this package calls it, and the
`list_skills` tool lists the same fields either way.

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
- `create_skill`, `update_skill`, `disable_skill` and `enable_skill` write. Each one asks
  `can_write` and raises `SkillWriteRefusedError` when the policy refuses. That error is a
  Django `PermissionDenied`. A create asks about the new skill. An update, a disable and
  an enable ask twice, once about the row as stored and once about the result. A refused
  update, disable or enable leaves both the row and the instance you passed as they were.
- A name that is not kebab-case raises `InvalidSkillError`. So do a blank or oversized
  description or body. A create or an enable by a caller who already sees
  `MAX_VISIBLE_SKILLS` (100) enabled skills raises `SkillLimitError`, a subclass of it. An
  enable asks `can_write` before it counts the cap, so a refused caller sees the refusal.
- `InvalidSkillError.field` names the draft field that broke the rule: `name`,
  `description` or `body`, and from a `SKILL.md` also `user_invocable` or
  `model_invocable`. It is `None` when no one field is at fault, as for the visible-skill
  cap, an unsupported frontmatter key or a malformed frontmatter. Build the error without
  `field` and it is `None`, so existing callers are unaffected.
- `list_visible_skills` stops at `MAX_VISIBLE_SKILLS`, in the policy's order, because its
  list is the catalog that reaches the model. A create can be admitted while other
  callers already see the cap, for example when staff add a company skill that a member
  with many personal skills also sees. The list still holds, and since personal skills
  come first in the policy's order, the member's own skills and their shadows survive the
  cut.

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

## Offering stored skills to the agent

Pass `StoredSkills` in the per-run `capabilities=`, built for the request the run serves:

```python
from django_spec_agents.skills import StoredSkills

capabilities = [StoredSkills(Skill.skill_policy(), request, authoring=True)]
```

On every run it reads the caller's visible skills through the policy, and offers each
enabled skill with `model_invocable` set as a deferred capability. The capability's id is
the skill's name. Until the model calls `load_capability` with that id, it sees only the
name and the description. After that, the body joins its instructions. A skill created or
changed between two runs is offered on the next run, with no agent rebuild.

A skill grants nothing. It has no tools, and it cannot lift an approval. With the tool
guard enabled, a destructive tool waits for approval, and the policy's `can_write` refuses
a write, whatever a body says.

`authoring=True` adds `build_skill_tools`' five tools:

| Tool | Gated | Does |
| --- | --- | --- |
| `list_skills` | no | the visible skills: name, description, the two flags, and `writable`. No body |
| `create_skill` | yes | `create_skill` with the name, description, body and flags given |
| `update_skill` | yes | replaces the draft of the enabled skill the caller reaches by name |
| `disable_skill` | yes | `disable_skill` on that skill |
| `enable_skill` | yes | `enable_skill` on the first disabled skill of that name the policy yields |

The four writes carry the destructive marker, so when the tool guard is enabled `ToolGuard`
defers them for approval and the approval card shows the body. `ToolGuardConfig.enabled`
is off by default, and with it off the writes run at once, so pair `authoring=True` with
`ToolGuardConfig(enabled=True)`. A draft that breaks a rule is sent back to the model to
retry. A write the policy refuses is reported to the model as text, and nothing changes,
and so is a create or an enable by a caller at `MAX_VISIBLE_SKILLS`: a retry cannot clear it.

It also adds the bundled `create-skill` skill, which interviews the user, drafts a name,
description and body, and calls `create_skill`. A visible stored skill named `create-skill`
replaces the bundled one, by the same rule that lets a personal skill replace a company one.

## Refusing a model with no policy at startup

No setting names your model. Your code holds it and passes `Skill.skill_policy()` to
every function, and to a transport as a constructor argument. To have startup refuse a
model with no policy, add the package's opt-in app to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...,
    "django_spec_agents",
    "myproject.skills",
]
```

The app registers a Django system check that walks the app registry for every installed
model subclassing `AbstractSkill`, so `manage.py check`, `runserver` and `migrate` fail
when one of them is unguarded. An abstract layer has no table and is not checked.

| Id | When |
| --- | --- |
| `django_spec_agents.E001` | An installed `AbstractSkill` subclass's `skill_policy()` returns `None`, which is `AbstractSkill`'s default |

The app has no models and no migrations. Its label, `django_spec_agents`, does not clash
with the store app's label, `django_spec_agents_store`. A project that has no skills
leaves the app out, and nothing changes for it.

Import `django_spec_agents.skills` once the app registry is ready, for example from a
`models.py`, a view or a policy, because it defines an abstract model.
