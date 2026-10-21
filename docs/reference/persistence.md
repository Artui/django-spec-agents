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

## Reference implementations

The opt-in `django_spec_agents.contrib.store` app. Add it to
`INSTALLED_APPS` and run `migrate`; the base package ships no model, so projects
that don't opt in get no migration.

::: django_spec_agents.contrib.store.default_conversation_store.DefaultConversationStore

::: django_spec_agents.contrib.store.default_attachment_store.DefaultAttachmentStore

::: django_spec_agents.contrib.store.default_step_store.DefaultStepStore

::: django_spec_agents.contrib.store.default_memory_store.DefaultMemoryStore

### Attachment lifecycle

Contrib-level, not part of either protocol. See
[Storage](../storage.md#attachment-lifecycle) for the semantics and the
management commands.

::: django_spec_agents.contrib.store.reconcile_conversation_attachments.reconcile_conversation_attachments

::: django_spec_agents.contrib.store.strip_inline_binary_parts.strip_inline_binary_parts

::: django_spec_agents.contrib.store.types.attachment_deletion.AttachmentDeletion
