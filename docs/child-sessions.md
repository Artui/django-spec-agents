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
| `check_task(thread_id)` | Shows where one child stands now, and why its last run ended early, without waiting for its report |
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
shares, such as [`DefaultSpendStore`](storage.md#spend-counters). An in-memory store counts
each process on its own. This needs the `[harness]`
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
share one status, and `wake_thread`, which reads that status, waits on either's run. An
inbound queue is not keyed that way: its claim names the scope, so two threads with one
thread id in two scopes each have their own inbox, held by their own run.

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

The runner is also told which run started the child: `start_session` passes the parent
run's id as `parent_run_id`, and a runner reports it as the child's first
`RUN_STARTED.parentRunId`. A wake that resumes a parked child passes the parked run's
id the same way; any other wake passes `None`. Every caller passes both `parent_run_id`
and `routine_id` on every start, so a [`HeadlessRunner`](reference/persistence.md#headlessrunner)
must accept them.

## Deleting a parent

A transport that deletes a parent thread should cancel its running children, with their
descendants, and keep them as ordinary threads with the link cleared, as django-ag-ui does
from 0.86.0. This package does neither on its own; a store clears the link with
`unlink_parent`, on the optional [`UnlinkableConversationStore`](reference/persistence.md#unlinkableconversationstore)
protocol:

```python
from django_spec_agents import UnlinkableConversationStore

if isinstance(children, UnlinkableConversationStore):
    await children.unlink_parent("c1", request=request)
```

The child then loads with `parent=None` and `lineage=None`, so no run of its thread is
bounded as a child's, and `DefaultChildSessionIndex.children` no longer lists it. Call it
through the child's own scoped store; unlinking a missing thread, or another owner's,
does nothing. Unlink once the child's run has ended or been cancelled: a save that names
a `parent` writes the link again.

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

When the user joins a child's session, typing into its thread in a browser, the result
may follow the user's direction rather than the parent's instructions. The transport says
so by passing `user_joined=True` to `child_lineage_for` for that run, which hands it to
`ChildReport`: the report's framed body then ends with a line telling the parent that the
user joined the session, so its result may reflect their direction. A run nobody joined
reports without it.

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

`lease` (a keyword, five minutes by default) is how long the run's claim on the thread's
inbox lasts without a model request to renew it.

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
deferred call waits in the queue for the resume. A message is acked once the model
request that carries it is answered, so it is in the run's history.

One run holds a thread's inbox at a time. The run takes it with `InboundQueue.take` under
its pydantic-ai `run_id`, at each model request, which renews its claim for `lease`; a
second run on the thread meanwhile takes nothing, and a run sees each message once. The
claim is the queue's own, so a queued run that a worker picks up later holds it as any
other run does, and takes what arrived while it waited. When the run ends, parks or fails,
it releases the inbox, and what it took but never acked goes to the thread's next run. A
run whose process died cannot release: its messages wait until its lease lapses, and the
next run takes them over. A run that starts before then takes nothing from the inbox, so
keep `lease` no longer than the lease your transport gives its `RunStore` claim. A run with no signed-in user in `call_context["request"]` reads
nothing.

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
    conversations=conversation_store,  # optional: the store the thread's mount reads
)
```

Call it after enqueueing for a thread. What it does depends on where the thread's runs
stand in the `RunStore`:

| The thread | `wake_thread` | The message is read |
| --- | --- | --- |
| running | starts nothing | at the run's next model request; after its last one, by the wake its runner makes once it ends |
| waiting on questions only, with an `ANSWER` pending for each | resumes the parked calls with those answers | each answer as its call's result; the rest by the resumed run |
| waiting on any approval, or on more questions than there are answers pending | starts nothing | by the resumed run, at its first request |
| idle, failed or never run, with messages | starts one headless run | at that run's first request |
| idle, with nothing pending, or only messages a run took and has not released | starts nothing | the holder hands them on when it releases; a dead holder's, to the next run once its lease lapses |

A child parked on `ask_parent` resumes when its parent answers. `wake_thread` takes the
oldest `ANSWER`s in the child's queue, one for each waiting call, and starts the run with
`answers={tool_call_id: text, ...}`, each waiting call's id from the `RunStore`, in the
order the run deferred them, mapped to the next answer's text. An answer names no call,
so a child parked on two questions takes its parent's answers in the order they were
sent; with fewer answers than questions it waits for the rest. The runner resumes the child's stored history with
`DeferredToolResults(calls=answers)` and sends no new prompt, so the answer reaches the
model as `ask_parent`'s result. The wake pairs the answers while it holds a claim on the
child's inbox (`take`, then `release`), and holds it until the start returns, so when two
wakes race on one park, as a parent answering both questions in one response makes them,
only one pairs the answers and starts the run; a wake that finds the inbox claimed starts
nothing. The `ANSWER`s are acked once the start returns, still under the claim, so the
resumed run's drain, which takes nothing while the wake holds the inbox, does not read
them a second time as plain text. A start that fails, or is cancelled or dies with its
process before the runner claims the thread, acks nothing: the answers stay pending, in
order, for the next wake, once the wake's claim is released or its lease lapses. Once the
run has started, a wake cancelled or killed before its ack leaves the answers unacked, and
the resumed run's drain delivers them a second time, as plain text, as the queue's
at-least-once delivery allows. The claim covers every message the wake took, so the
resumed run reads whatever else is queued from its first read after the wake releases the
inbox, which can be its second model request rather than its first.
An answer can also arrive while the child's run is still going, before it parks on the
question; the runner calls `wake_thread` once after recording such a park, so that answer
resumes it too.

So a thread has one run at a time. Two wakes in a row start one run, which reads every
message queued by its first request. Two wakes that race in two processes start one too:
the runner's claim lets one through, and the other's refusal is absorbed once the thread
reads as running. Any other failure to start is raised.

The woken run's new message is a line saying that messages arrived; pass `prompt=` to
change it.

A child's thread wakes the same way, when its parent steers or answers it while it is
idle. Pass `conversations`, the store the child's mount reads (a
`ScopedConversationStore` where the mount scopes its threads), and `wake_thread` starts
the run with the `parent` and `lineage` the child's conversation stores. The runner
composes the child's bounds from them, as
[Every run of a child's thread](#every-run-of-a-childs-thread) describes.
`wake_thread` never passes `capabilities`. A thread with no stored conversation, or a call
with no `conversations`, is started with neither.

### Waking on every message

`WakingInboundQueue` wraps a queue and calls `wake_thread` after each `enqueue`. Pass it
wherever the toolset and `child_lineage_for` take a queue, and every message wakes its
thread: a child's report or question wakes an idle parent, `message_task` or
`answer_task` wakes a settled child, whose new run starts with the wake line as its
prompt and the message delivered beside it by `DrainInbound`, and `answer_task` resumes
a child parked on `ask_parent` with the answer. A message that lands on a running thread
after its run's last read is not woken by the enqueue, since the thread still reads as
running; the runner's wake after `RunStore.finish` starts the next run for it.

```python
from django_spec_agents import WakingInboundQueue

queue = WakingInboundQueue(
    DefaultInboundQueue(),
    headless_runner,
    run_store,
    300.0,  # each woken run's timeout
    conversations={"research": research_store},  # agent id to the store its mount reads
)
```

A thread whose agent id is not in `conversations` wakes with no `parent` or `lineage`, as
a root thread does. A wake that fails is logged, and the message stays queued for the
thread's next run.
