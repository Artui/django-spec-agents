# Routines

The routine model, its policy, the schedule vocabulary and the functions over them. See
[Routines](../routines.md) for the narrative. Everything here but `run_agent_spec` imports
from `django_spec_agents.routines`.

## `AbstractRoutine`

::: django_spec_agents.routines.AbstractRoutine

## `RoutinePolicy`

::: django_spec_agents.routines.RoutinePolicy

## `RoutineDraft`

::: django_spec_agents.routines.RoutineDraft

## `WeeklySchedule`

::: django_spec_agents.routines.WeeklySchedule

## `IntervalSchedule`

::: django_spec_agents.routines.IntervalSchedule

## `PreapprovedTool`

::: django_spec_agents.routines.PreapprovedTool

## `next_fire_at`

::: django_spec_agents.routines.next_fire_at

## `parse_schedule`

::: django_spec_agents.routines.parse_schedule

## `schedule_to_json`

::: django_spec_agents.routines.schedule_to_json

## `create_routine`

::: django_spec_agents.routines.create_routine

## `update_routine`

::: django_spec_agents.routines.update_routine

## `due_routines`

::: django_spec_agents.routines.due_routines

## `RoutineSweep`

::: django_spec_agents.routines.RoutineSweep

## `SweepResult`

::: django_spec_agents.routines.SweepResult

## `pin_preapproved_tools`

::: django_spec_agents.routines.pin_preapproved_tools

## `preapproved_exempt`

::: django_spec_agents.routines.preapproved_exempt

## `run_agent_spec`

Needs the `[specs]` extra, so it is imported from its module,
`django_spec_agents.routines.run_agent_spec`, never from the package.

::: django_spec_agents.routines.run_agent_spec.run_agent_spec

## `RoutineFiring`

::: django_spec_agents.routines.RoutineFiring

## `ROUTINE_GRANT_KEY`

::: django_spec_agents.routines.ROUTINE_GRANT_KEY

## `InvalidRoutineError`

::: django_spec_agents.routines.InvalidRoutineError

## `RoutineWriteRefusedError`

::: django_spec_agents.routines.RoutineWriteRefusedError

## Limits

`MIN_ROUTINE_INTERVAL` (an hour) and `MAX_ROUTINE_INTERVAL` (366 days) bound an interval.
`MAX_ROUTINES_PER_USER` (25) caps a principal's routines. `MAX_ROUTINE_NAME_LENGTH`,
`MAX_ROUTINE_AGENT_ID_LENGTH` and `MAX_ROUTINE_PROMPT_LENGTH` bound a draft's text.

::: django_spec_agents.routines.MIN_ROUTINE_INTERVAL

::: django_spec_agents.routines.MAX_ROUTINE_INTERVAL

::: django_spec_agents.routines.MAX_ROUTINES_PER_USER

::: django_spec_agents.routines.MAX_ROUTINE_NAME_LENGTH

::: django_spec_agents.routines.MAX_ROUTINE_AGENT_ID_LENGTH

::: django_spec_agents.routines.MAX_ROUTINE_PROMPT_LENGTH

## `Schedule`

::: django_spec_agents.routines.Schedule
