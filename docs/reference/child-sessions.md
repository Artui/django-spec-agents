# Child sessions

The toolset, the lineage a child carries, the report frame, the delivery of a thread's
inbound messages and the types around them. See
[Child sessions](../child-sessions.md) for the narrative. Each imports from its own module
under `django_spec_agents.child_sessions`, except `ChildLineageRecord`, which a
`Conversation` holds and so lives under `django_spec_agents.persistence.types`.

## `ChildSessionsToolset`

::: django_spec_agents.child_sessions.child_sessions_toolset.ChildSessionsToolset

## `ChildLineage`

::: django_spec_agents.child_sessions.child_lineage.ChildLineage

## `ChildReport`

::: django_spec_agents.child_sessions.child_report.ChildReport

## `child_lineage_for`

::: django_spec_agents.child_sessions.child_lineage_for.child_lineage_for

## `ChildLineageRecord`

::: django_spec_agents.persistence.types.child_lineage_record.ChildLineageRecord

## `DrainInbound`

::: django_spec_agents.child_sessions.drain_inbound.DrainInbound

## `wake_thread`

::: django_spec_agents.child_sessions.wake_thread.wake_thread

## `WakingInboundQueue`

::: django_spec_agents.child_sessions.waking_inbound_queue.WakingInboundQueue

## `report_frame`

::: django_spec_agents.child_sessions.report_frame.report_frame

## `MESSAGE_ORIGIN_KEY`

::: django_spec_agents.child_sessions.types.message_origin_key.MESSAGE_ORIGIN_KEY

## `MESSAGE_TASK_KEY`

::: django_spec_agents.child_sessions.types.message_task_key.MESSAGE_TASK_KEY

## `MessageOrigin`

::: django_spec_agents.child_sessions.types.message_origin.MessageOrigin

## `ChildSessionReceipt`

::: django_spec_agents.child_sessions.types.child_session_receipt.ChildSessionReceipt

## `ChildSessionRefusal`

::: django_spec_agents.child_sessions.types.child_session_refusal.ChildSessionRefusal

## `ChildSession`

::: django_spec_agents.child_sessions.types.child_session.ChildSession

## `ChildSessionIndex`

::: django_spec_agents.child_sessions.types.child_session_index.ChildSessionIndex

## `ChildSessionLimits`

::: django_spec_agents.child_sessions.types.child_session_limits.ChildSessionLimits

## `ChildSessionSpend`

::: django_spec_agents.child_sessions.types.child_session_spend.ChildSessionSpend
