from __future__ import annotations

from django.db import models

from django_spec_agents.contrib.store.attachment_storage import attachment_storage


class StoredConversation(models.Model):
    """The reference durable conversation row, one per ``(owner_id, thread_id)``."""

    thread_id = models.CharField(max_length=255)
    owner_id = models.CharField(max_length=255, blank=True, default="")
    title = models.CharField(max_length=255, blank=True, default="")
    preview = models.TextField(blank=True, default="")
    messages = models.JSONField(default=list)
    attachments = models.ManyToManyField(
        "StoredAttachment",
        through="ConversationAttachment",
        related_name="conversations",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner_id", "thread_id"],
                name="django_spec_agents_store_owner_thread_unique",
            )
        ]
        indexes = [models.Index(fields=["owner_id", "-updated_at"])]

    def __str__(self) -> str:
        return self.title or self.thread_id


class StoredAttachment(models.Model):
    """The reference durable attachment row for a model-backed store."""

    attachment_id = models.CharField(max_length=255)
    owner_id = models.CharField(max_length=255, blank=True, default="")
    thread_id = models.CharField(max_length=255, blank=True, default="")
    name = models.TextField(blank=True, default="")
    mime = models.TextField(blank=True, default="")
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    # Indexed: deleting a row must ask whether another row still points at the same blob.
    file = models.FileField(
        upload_to="django_pydantic_agent/attachments/",
        storage=attachment_storage,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner_id", "attachment_id"],
                name="django_spec_agents_attachment_owner_id_unique",
            )
        ]
        indexes = [
            models.Index(fields=["owner_id", "-created_at"]),
            # Owner first, and only ever queried with an owner. An index on
            # ``sha256`` alone would make the query this store must never run —
            # "who else holds these bytes?", across tenants — the cheap one.
            models.Index(fields=["owner_id", "sha256"]),
        ]

    def __str__(self) -> str:
        return self.name or self.attachment_id


class ConversationAttachment(models.Model):
    """One conversation's reference to one attachment."""

    conversation = models.ForeignKey(
        StoredConversation, on_delete=models.CASCADE, related_name="attachment_links"
    )
    attachment = models.ForeignKey(
        StoredAttachment, on_delete=models.CASCADE, related_name="conversation_links"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "attachment"],
                name="django_spec_agents_conversation_attachment_unique",
            )
        ]

    def __str__(self) -> str:
        return f"{self.conversation.thread_id}:{self.attachment.attachment_id}"


# --- Durable run lineage -----------------------------------------------------
#
# The four models below back ``DefaultStepStore``. Each mirrors one
# ``pydantic-ai-harness`` dataclass (``RunRecord`` / ``StepEvent`` /
# ``ContinuableSnapshot`` / ``ToolEffectRecord``) plus an ``owner_id`` every query
# filters by — the security boundary, which the harness types do not carry.
# Optional lineage strings are ``null=True`` rather than blank so a ``None``
# sentinel round-trips exactly; ``list_runs`` filters on it.


class StoredRun(models.Model):
    """Lineage metadata for one agent run, one row per ``(owner_id, run_id)``."""

    run_id = models.CharField(max_length=255)
    owner_id = models.CharField(max_length=255, blank=True, default="")
    conversation_id = models.CharField(max_length=255, null=True, blank=True, default=None)
    parent_run_id = models.CharField(max_length=255, null=True, blank=True, default=None)
    agent_name = models.TextField(null=True, blank=True, default=None)
    metadata = models.JSONField(default=dict)
    started_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner_id", "run_id"], name="django_spec_agents_run_owner_run_unique"
            )
        ]
        indexes = [models.Index(fields=["owner_id", "started_at"])]

    def __str__(self) -> str:
        return self.run_id


class StoredStepEvent(models.Model):
    """One append-only step event at a run/model/tool boundary."""

    run_id = models.CharField(max_length=255)
    owner_id = models.CharField(max_length=255, blank=True, default="")
    kind = models.CharField(max_length=32)
    step_index = models.IntegerField()
    timestamp = models.DateTimeField()
    conversation_id = models.CharField(max_length=255, null=True, blank=True, default=None)
    parent_run_id = models.CharField(max_length=255, null=True, blank=True, default=None)
    agent_name = models.TextField(null=True, blank=True, default=None)
    tool_call_id = models.CharField(max_length=255, null=True, blank=True, default=None)
    tool_name = models.TextField(null=True, blank=True, default=None)
    error = models.TextField(null=True, blank=True, default=None)
    metadata = models.JSONField(default=dict)

    class Meta:
        indexes = [models.Index(fields=["owner_id", "run_id"])]

    def __str__(self) -> str:
        return f"{self.run_id}:{self.kind}"


class StoredSnapshot(models.Model):
    """A message history a run can be resumed or forked from."""

    run_id = models.CharField(max_length=255)
    owner_id = models.CharField(max_length=255, blank=True, default="")
    step_index = models.IntegerField()
    messages = models.JSONField(default=list)
    conversation_id = models.CharField(max_length=255, null=True, blank=True, default=None)
    parent_run_id = models.CharField(max_length=255, null=True, blank=True, default=None)
    agent_name = models.TextField(null=True, blank=True, default=None)
    timestamp = models.DateTimeField()
    state = models.CharField(
        max_length=16,
        default="complete",
        choices=[("complete", "complete"), ("interrupted", "interrupted")],
    )

    class Meta:
        indexes = [models.Index(fields=["owner_id", "run_id"])]

    def __str__(self) -> str:
        return f"{self.run_id}@{self.step_index}"


class StoredToolEffect(models.Model):
    """A tool call's side-effect status."""

    run_id = models.CharField(max_length=255)
    owner_id = models.CharField(max_length=255, blank=True, default="")
    tool_call_id = models.CharField(max_length=255)
    tool_name = models.TextField()
    status = models.CharField(max_length=16)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True, default=None)
    idempotency_key = models.TextField(null=True, blank=True, default=None)
    effect_summary = models.TextField(null=True, blank=True, default=None)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner_id", "run_id", "tool_call_id"],
                name="django_spec_agents_tool_effect_owner_run_call_unique",
            )
        ]
        indexes = [models.Index(fields=["owner_id", "run_id"])]

    def __str__(self) -> str:
        return f"{self.run_id}:{self.tool_call_id}:{self.status}"


class StoredMemory(models.Model):
    """One durable memory file, one row per ``(owner_id, path)``."""

    owner_id = models.CharField(max_length=255, blank=True, default="")
    path = models.CharField(max_length=500)
    content = models.TextField(blank=True, default="")
    version = models.CharField(max_length=32)
    operation_id = models.CharField(max_length=255, null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner_id", "path"],
                name="django_spec_agents_memory_owner_path_unique",
            )
        ]

    def __str__(self) -> str:
        return self.path


class StoredMemoryOperation(models.Model):
    """An idempotency receipt for one memory mutation."""

    owner_id = models.CharField(max_length=255, blank=True, default="")
    operation_id = models.CharField(max_length=255)
    fingerprint = models.TextField()
    version = models.CharField(max_length=32, null=True, blank=True, default=None)
    existed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner_id", "operation_id"],
                name="django_spec_agents_memory_owner_operation_unique",
            )
        ]

    def __str__(self) -> str:
        return self.operation_id
