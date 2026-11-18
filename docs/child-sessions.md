# Child sessions

A child session is a whole conversation that one agent starts on another agent. It
gets its own thread, runs headless, and can outlive the run that started it. The parent
can message it, answer it and cancel it. The user opens it like any other thread.
`ChildSessionsToolset` gives the parent's model the tools to do this. Child sessions need
the `[harness]` extra, and building the toolset without it raises `ImportError`.

This page covers starting a child, the limits on it, its report, and how a report,
question, answer or steer reaches the model of the thread it was sent to.

## Mounting the toolset

```python
from decimal import Decimal

from django_spec_agents.child_sessions.child_sessions_toolset import ChildSessionsToolset
from django_spec_agents.child_sessions.types.child_session_limits import ChildSessionLimits
from django_spec_agents.child_sessions.types.child_session_spend import ChildSessionSpend

children = ChildSessionsToolset(
    "support",  # this mount's agent id, its thread scope
    agents={"research": "Reads the docs and answers one question."},
    runner=headless_runner,  # a HeadlessRunner over your mounts
    index=child_index,  # a ChildSessionIndex
    queue=inbound_queue,  # an InboundQueue
    limits=ChildSessionLimits(timeout=300.0),
    spend=ChildSessionSpend(shared_spend_store, tree_usd=Decimal("2"), day_usd=Decimal("10")),
)
```

Pass `children` among the mount's toolsets. A tool finds the run's thread through
pydantic-ai's `conversation_id`, which an AG-UI run sets to its thread id. It finds the
run's request in `ctx.deps.call_context["request"]`, where the default deps put it. A
project with its own `deps_factory` must put the request there too, or every tool raises.

## The tools

| Tool | What it does |
| --- | --- |
| `start_session(agent, prompt)` | Starts a child on one of `agents` and returns a receipt |
| `list_tasks()` | Lists this thread's children and their status |
| `message_task(thread_id, message)` | Sends a child more instructions, through its inbound queue |
| `answer_task(thread_id, answer)` | Answers a child's question, through its inbound queue |
| `cancel_task(thread_id)` | Cancels a child and every session under it |
| `ask_parent(question)` | Offered to a child only: queues a question to its parent and parks |

`start_session` returns a `ChildSessionReceipt`, and that receipt becomes the tool's result.
It names the child's `agent_id` and `thread_id`. A client looks for the child's thread in
the tool result, because the result is the one part of the parent's transcript that
persists.

The other names are the ones upstream's `TaskControl` uses, and pydantic-ai refuses two
tools with the same name. So a mount that carries both `ChildSessionsToolset` and
`SubAgents(task_control=TaskControl())` fails on its first run. Inline delegation through
`SubAgents`' `delegate_task` still works beside child sessions.

A tool that cannot act returns a `ChildSessionRefusal` rather than raising. Its `refused`
field names the rule that refused, and the model reads the reason. The tools refuse:

- an agent id that is not in `agents`;
- a `thread_id` that is not one of this thread's own children.

## The child's principal and tools

A child runs as the parent's user, so a run with no signed-in user cannot start one: it is
refused as `principal`. The child sees only the tools its parent had when it started, plus
the child-session tools, and never any others. A tool that needed approval in the parent
needs approval in the child too, and the child's own mount guards each call as well. When
a call needs approval, the child parks as `waiting`, and the parent is not asked to
approve it. `ask_parent` parks the child the same way, on a deferred call of kind
`question`.

The parent passes these tool names to the child, along with the child's place in the
tree, as a `ChildLineageRecord`. The runner composes a `ChildLineage` capability from it
on every run of the child's thread, the first included, as
[Every run of a child's thread](#every-run-of-a-childs-thread) describes.

## Limits

`ChildSessionLimits` holds the owner's defaults:

| Limit | Default | Refused as |
| --- | --- | --- |
| Depth below the root thread | `max_depth=2`, so a child and a grandchild | `max_depth` |
| Live children of one thread | `max_running_per_thread=3` | `max_running_per_thread` |
| Live children of one user, under any thread | `max_running_per_user=10` | `max_running_per_user` |
| Each child run's deadline | `timeout`, required, in seconds | the run ends `timed_out` |

A child counts as live while it is running and while it waits. A parked child keeps its
slot until someone answers it or cancels it. When the model asks for several children in one response,
the starts run one at a time, so each start counts the children before it. Starts in two
processes at once are not serialised: each reads the index before the other's child is
listed, so the per-thread and per-user counts can overshoot by the number of processes
starting at that moment. The limits bound the common case, not a race between workers.

`ChildSessionSpend` adds two `pydantic-ai-harness` `SpendLimits` to every child:

- `tree_usd` caps the whole tree under the root thread, for all time;
- `day_usd` caps one user's children for each day.

A start that finds either budget exhausted is refused, as `child_sessions_tree_spend` or
`child_sessions_day_spend`. The parent's own spend counts toward the tree budget only if
the parent's mount carries the same budget. The `store` has to be one that every worker
shares. An in-memory store counts each process on its own. This needs the `[harness]`
extra.

## Finding children

No store can say which threads a thread started, or how many of one user's children are
live. A `ChildSessionIndex` answers those two questions, reading the `parent` link each
child's conversation stores and the run status kept beside it. A child must be listed from the moment
`HeadlessRunner.start` returns, or the per-thread limit cannot count it. The limits use it, and so
does `cancel_task`, which walks the tree from the child down.

`DefaultChildSessionIndex(run_store)`, in the store app, is the one to use with
`DefaultConversationStore`. It lists a parent's children, oldest first and in any scope,
reading a scoped store's `scope:thread_id` column back into a `ThreadRef`, and takes each
child's status from the `RunStore`, idle where it holds none. The run store is keyed by
the client's thread id, not its scope, so two children with one thread id in two scopes
share one status.

## Every run of a child's thread

`start_session` hands the runner a `ChildLineageRecord` (the root, the depth, the
parent's tools and the ones it gated) beside `parent`, and no capabilities, since
capabilities cannot cross a queue to a worker. The runner saves both on the child's
conversation. `DefaultConversationStore` keeps the record in a JSON column and, as with
`parent`, leaves it alone on a save that names none. A child started through the
`run_headless` spec carries the record too: `HeadlessFiring.lineage` crosses the queue as
JSON, with `tools` and `gated` as sorted lists, and the runner saves it the same way.

Every run of that thread (its first, the user typing into it, a resume after an
approval, a wake) has to carry the same bounds, or it runs with the child mount's full
tools. The runner composes them on each run of a thread that has a parent:

```python
capabilities = child_lineage_for(
    conversation, agent_id="research", owner=str(user.pk), queue=queue, spend=spend
)
```

It returns nothing for a thread with no stored lineage, so a runner can call it on
every run. Until 0.21.0 `start_session` passed these capabilities to the first run
itself; a runner that composes only on later runs leaves a child's first run unbounded.

## Reports

`ChildReport` is composed into every child run, by `child_lineage_for`. When the run settles it enqueues a `REPORT` on the parent's queue,
with the body `{"text": ..., "outcome": ...}`, sent from the child's thread:

| The run | Outcome |
| --- | --- |
| ends with output | `ok` |
| stops at a usage or spend limit | `budget` |
| passes its deadline | `timeout` |
| raises | `failed` |
| parks, on a gated call or `ask_parent` | no report |
| is cancelled before its deadline | no report, since whoever cancelled it knows |

The report needs the run's request in `call_context["request"]`, as the toolset does. A
report that cannot be sent is logged, and the run's own result or error stands.

`report_frame(child, agent_id=..., output=..., outcome=...)` builds the text: a settled
child's output as `<task_output>` data, with the tag and attributes of upstream's
`DelegationTask.report()`, rendered by `pydantic_ai.framing.wrap`. The escaping is in the
text itself, since the report crosses a queue before any model sees it: the output stays
in the user channel, never in system voice, and it cannot close its own frame or forge
a `<task_question>`.

## Delivery

Reports, questions, answers and steering all travel through the receiving thread's
inbound queue. `DrainInbound` hands them to that thread's model. Compose it into every
run of each mount whose threads receive them, the parent's and the child's alike:

```python
from django_spec_agents.child_sessions.drain_inbound import DrainInbound

drain = DrainInbound(inbound_queue, "support")  # the mount's agent id, its thread scope
```

At each model request it reads the run's thread's queue and adds what it finds to the
request, oldest first:

| Kind | Sent by | The model reads |
| --- | --- | --- |
| `REPORT` | `ChildReport`, when a child settles | the `<task_output>` text the child framed |
| `QUESTION` | `ask_parent` | `<task_question task_id="..." agent_name="...">` data naming the child |
| `ANSWER` | `answer_task` | the parent's answer, as plain words |
| `STEER` | `message_task` | the parent's instructions, as plain words |

A question is framed when it is read, with `pydantic_ai.framing.wrap`, so the child's
text cannot close its frame or speak in system voice. The parent answers it with
`answer_task`, naming the `task_id`.

A message that lands after the run's last model request still reaches the model: the run
makes one more request before it ends. A message that lands as the run parks on a
deferred call waits in the queue for the resume. The run acks what it delivered when it
ends, whether it completed, parked or failed, so a run whose process died delivers it
again. A run with no signed-in user in `call_context["request"]` reads nothing.

## Waking a thread

A message for a thread with no run would wait until someone opened it.
`wake_thread` starts that run:

```python
from django_spec_agents.child_sessions.wake_thread import wake_thread

started = await wake_thread(
    parent_thread,  # a ThreadRef: the thread id and its mount's agent id
    request=request,  # the owner's request; the run is started as request.user
    runner=headless_runner,
    runs=run_store,
    queue=inbound_queue,
    timeout=300.0,
)
```

Call it after enqueueing for a thread. What it does depends on where the thread's runs
stand in the `RunStore`:

| The thread | `wake_thread` | The message is read |
| --- | --- | --- |
| running | starts nothing | at the run's next model request |
| waiting, on an approval or a question | starts nothing | by the resumed run, at its first request |
| idle, failed or never run, with messages | starts one headless run | at that run's first request |
| idle, with nothing pending | starts nothing | |

A child parked on `ask_parent` stays waiting when its parent answers: the answer waits
in the child's queue, and no headless run resumes a parked call with it yet. A run that
resumes the child, such as the user answering in its thread, reads it.

So a thread has one run at a time. Two wakes in a row start one run, which reads every
message queued by its first request. Two wakes that race in two processes start one too:
the runner's claim lets one through, and the other's refusal is absorbed once the thread
reads as running. Any other failure to start is raised.

The woken run's new message is a line saying that messages arrived; pass `prompt=` to
change it. `wake_thread` passes the runner no `lineage` and no `capabilities`. A child's
thread keeps the lineage it was started with, and the runner composes it on every run,
as [Every run of a child's thread](#every-run-of-a-childs-thread) describes.
