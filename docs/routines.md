# Routines

A routine is a stored prompt for one agent, a schedule, and the user it runs as: "every
weekday at 8, run /what-is-on and tell me what is overdue". The package gives you the
model's fields, a policy protocol, the schedule vocabulary, the next-firing computation,
and the functions that write a routine. You bring the concrete model, its tenant
relation, its migration and the policy. No concrete routine model ships in the package,
and no default policy.

This page covers the storage and the rules. Firing a routine on schedule and showing its
thread are later pieces.

## The model

Subclass `AbstractRoutine` and add whatever scopes a routine in your project:

```python
from django.db import models

from django_spec_agents.routines import AbstractRoutine, RoutinePolicy


class Routine(AbstractRoutine):
    team = models.ForeignKey("teams.Team", on_delete=models.CASCADE)

    @classmethod
    def routine_policy(cls) -> RoutinePolicy["Routine"]:
        return OwnRoutinesPolicy()


class OwnRoutinesPolicy:
    def visible(self, request):
        return Routine.objects.filter(principal=request.user)

    def can_write(self, request, routine):
        return routine.principal_id == request.user.pk

    def can_run(self, principal, routine):
        return principal.is_active and routine.team.members.filter(pk=principal.pk).exists()

    def new_routine(self, request):
        return Routine(team=request.user.team)
```

| Field | Meaning |
| --- | --- |
| `name` | What the person calls it, at most 120 characters |
| `agent_id` | The agent the routine runs |
| `prompt` | The message each firing sends, at most 20,000 characters |
| `schedule` | The schedule, as the JSON `schedule_to_json` writes; read it with `get_schedule()` |
| `timezone` | The IANA zone the schedule's wall times are read in; `get_timezone()` loads it |
| `principal` | The user each firing runs as: always the user who created the routine |
| `enabled`, `paused_reason` | Off means it does not fire; a pause by the system says why |
| `preapproved_tools` | The tools a firing may call without parking, each with its pinned flag |
| `next_fire_at` | The next firing, in UTC, indexed; `None` while the routine is disabled |
| `last_run_id` | The run the last firing started |
| `created_by`, `created_at`, `updated_at` | Who wrote it, and when |

## The policy

`RoutinePolicy` is four methods:

- `visible(request)` returns every routine the caller may see, disabled ones too.
- `can_write(request, routine)` decides whether the caller may save `routine`, a new
  unsaved one or an existing row.
- `can_run(principal, routine)` decides whether `routine` may fire now as its principal.
  A firing has no request, so it is asked with the user alone.
- `new_routine(request)` returns the unsaved routine a create writes to, with its scope
  already set.

## The schedule

The vocabulary is closed, and there are no cron strings:

- `WeeklySchedule(weekdays, at)` fires on each weekday (0 is Monday, 6 is Sunday) at the
  wall time `at`, to the minute, in the routine's timezone.
- `IntervalSchedule(every_hours)` fires every N hours of elapsed time, whatever the
  timezone's clock changes do. An interval shorter than `MIN_ROUTINE_INTERVAL`, an hour,
  is refused.

`parse_schedule(data)` reads a schedule from its JSON, `{"kind": "weekly", "weekdays":
[0, 1, 2, 3, 4], "at": "08:00"}` or `{"kind": "interval", "every_hours": 6}`, and
raises `InvalidRoutineError` for anything else. `schedule_to_json` writes it back.

`next_fire_at(schedule, timezone, after)` returns the first firing strictly after
`after`, in UTC. A weekly wall time a DST change skips fires shifted by the gap, so 02:30
on a spring-forward night fires at 03:30. A wall time it repeats fires once, at its first
occurrence. An interval adds its hours to `after`, so pass the firing just claimed rather
than the clock, or every late sweep pushes the cadence later.

## Writing

```python
from django_spec_agents.routines import RoutineDraft, WeeklySchedule, create_routine

policy = Routine.routine_policy()
create_routine(
    policy,
    request,
    RoutineDraft(
        name="Overdue digest",
        agent_id="assistant",
        prompt="/what-is-on: tell me what is overdue",
        schedule=WeeklySchedule(weekdays=frozenset(range(5)), at=time(8, 0)),
        timezone="Europe/London",
        preapproved_tools=("archive_invoice",),
    ),
    guard=agent_guard,
    tool_defs=agent_tool_defs,
)
```

- `create_routine` makes the caller the principal and `created_by`, and refuses an
  anonymous caller with `RoutineWriteRefusedError`. It raises `InvalidRoutineError` once
  the caller holds `MAX_ROUTINES_PER_USER` (25) routines, counted on the model by
  principal, disabled ones included.
- `update_routine` replaces the fields but never the principal, whoever edits.
  `next_fire_at` moves only when the schedule, the timezone or `enabled` changes, so an
  edit to the prompt keeps an interval's cadence. Turning a routine back on clears its
  `paused_reason`.
- Both ask `can_write`, and raise `RoutineWriteRefusedError` when the policy refuses.
  An update asks twice, once about the row as stored and once about the result. A blank
  name, agent or prompt, an oversized one, and a timezone `zoneinfo` cannot load raise
  `InvalidRoutineError`.

## Pre-approved tools

A routine's firings run with no one watching, so a gated tool call parks the thread
unless the routine pre-approved the tool. The grant is made when the routine is created
or edited, and that edit is where a person approves it.

The grant pins each tool's flag. `pin_preapproved_tools(guard, tool_defs, names)` asks
the agent's `ToolGuard.requires_approval` about each named definition and records the
answer as `destructive`. A name none of `tool_defs` carries raises
`InvalidRoutineError`, so a typo is never recorded as a grant. `create_routine` and
`update_routine` pin through it. An update keeps the flag already pinned for a tool the
routine held, so an unrelated edit never re-approves a tool declared destructive since.
A tool dropped from the list is revoked, and pins afresh if named again.

`preapproved_exempt(routine)` returns the names a firing's guard exempts: only the
tools that were gated when granted. A tool granted while ungated and declared
destructive later is not among them, so its call parks.

## Refusing a model with no policy at startup

Name your model in the `SPEC_AGENTS_ROUTINE_MODEL` setting, and add the package's opt-in
app to `INSTALLED_APPS`, as for [user skills](skills.md#refusing-a-model-with-no-policy-at-startup):

| Id | When |
| --- | --- |
| `django_spec_agents.E003` | The setting names no installed model, or names one that does not subclass `AbstractRoutine` |
| `django_spec_agents.E004` | The model's `routine_policy()` returns `None`, which is `AbstractRoutine`'s default |

`get_routine_model()` returns the configured model, or `None` when the setting is unset.
Import `django_spec_agents.routines` once the app registry is ready, because it defines
an abstract model.
