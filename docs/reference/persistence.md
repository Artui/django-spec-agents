# Persistence

Store contracts, their value types, and the shipped implementations. See
[Storage](../storage.md) for owner scoping, thread keying, and the reference
models.

## Conversations

### `ConversationStore`

::: django_spec_agents.ConversationStore

### `Conversation`

::: django_spec_agents.Conversation

### `ConversationMeta`

::: django_spec_agents.ConversationMeta

### `NullConversationStore`

::: django_spec_agents.NullConversationStore

### `DjangoSessionConversationStore`

::: django_spec_agents.DjangoSessionConversationStore

### `ModelConversationStore`

::: django_spec_agents.ModelConversationStore

### `ScopedConversationStore`

::: django_spec_agents.ScopedConversationStore

### `UnlinkableConversationStore`

::: django_spec_agents.UnlinkableConversationStore

## Attachments

### `AttachmentStore`

::: django_spec_agents.AttachmentStore

### `AttachmentRef`

::: django_spec_agents.AttachmentRef

### `OpenedAttachment`

::: django_spec_agents.OpenedAttachment

### `NullAttachmentStore`

::: django_spec_agents.NullAttachmentStore

### `ModelAttachmentStore`

::: django_spec_agents.ModelAttachmentStore

## Memory

### `memory_namespace`

::: django_spec_agents.memory_namespace

### `memory_namespace_for_user`

::: django_spec_agents.memory_namespace_for_user

## Errors

### `AnonymousOperationError`

::: django_spec_agents.AnonymousOperationError

### `IdentifierTooLongError`

::: django_spec_agents.IdentifierTooLongError

### `RunIdReusedError`

::: django_spec_agents.RunIdReusedError

### `preflight_identifiers`

::: django_spec_agents.preflight_identifiers

## Runs

A run's status and event log, and the protocol a transport implements to start
runs on the server. See [Storage](../storage.md#run-status-and-event-logs).

### `RunStore`

::: django_spec_agents.RunStore

### `ResumableRunStore`

::: django_spec_agents.ResumableRunStore

### `WatchableRunStore`

::: django_spec_agents.WatchableRunStore

### `RunNotParkedError`

::: django_spec_agents.RunNotParkedError

### `RunClaim`

::: django_spec_agents.RunClaim

### `RunOutcome`

::: django_spec_agents.RunOutcome

### `RunWaiting`

::: django_spec_agents.RunWaiting

### `build_run_waiting`

::: django_spec_agents.build_run_waiting

### `ThreadRunState`

::: django_spec_agents.ThreadRunState

### `ThreadRunStatus`

::: django_spec_agents.ThreadRunStatus

### `RunEndReason`

::: django_spec_agents.RunEndReason

### `RunLogEntry`

::: django_spec_agents.RunLogEntry

### `HeadlessRunner`

::: django_spec_agents.HeadlessRunner

### `HeadlessStart`

::: django_spec_agents.HeadlessStart

### `RunStop`

::: django_spec_agents.RunStop

### `FiringQueue`

::: django_spec_agents.headless.types.firing_queue.FiringQueue

### `FiringState`

::: django_spec_agents.headless.types.firing_state.FiringState

### `run_headless`

Needs the `[specs]` extra, so it is imported from its module, never from the root.

::: django_spec_agents.headless.run_headless.run_headless

### `HeadlessFiring`

::: django_spec_agents.headless.types.headless_firing.HeadlessFiring

## Child sessions

A thread one agent starts on another keeps a link to the thread that started it,
and every thread can have a durable queue of messages waiting for it.

### `ThreadRef`

::: django_spec_agents.persistence.types.thread_ref.ThreadRef

### `InboundQueue`

::: django_spec_agents.child_sessions.types.inbound_queue.InboundQueue

### `InboundMessage`

::: django_spec_agents.child_sessions.types.inbound_message.InboundMessage

### `InboundKind`

::: django_spec_agents.child_sessions.types.inbound_kind.InboundKind

### `ModelInboundQueue`

::: django_spec_agents.child_sessions.model_inbound_queue.ModelInboundQueue

## Reference implementations

The opt-in `django_spec_agents.contrib.store` app. Add it to
`INSTALLED_APPS` and run `migrate`; the base package ships no model, so projects
that don't opt in get no migration.

::: django_spec_agents.contrib.store.default_conversation_store.DefaultConversationStore

::: django_spec_agents.contrib.store.default_attachment_store.DefaultAttachmentStore

::: django_spec_agents.contrib.store.default_step_store.DefaultStepStore

::: django_spec_agents.contrib.store.default_memory_store.DefaultMemoryStore

::: django_spec_agents.contrib.store.default_run_store.DefaultRunStore

::: django_spec_agents.contrib.store.default_inbound_queue.DefaultInboundQueue

::: django_spec_agents.contrib.store.default_child_session_index.DefaultChildSessionIndex

::: django_spec_agents.contrib.store.default_spend_store.DefaultSpendStore

### Attachment lifecycle

Contrib-level, not part of either protocol. See
[Storage](../storage.md#attachment-lifecycle) for the semantics and the
management commands.

::: django_spec_agents.contrib.store.reconcile_conversation_attachments.reconcile_conversation_attachments

::: django_spec_agents.contrib.store.strip_inline_binary_parts.strip_inline_binary_parts

::: django_spec_agents.contrib.store.types.attachment_deletion.AttachmentDeletion
