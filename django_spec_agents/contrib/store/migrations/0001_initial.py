"""The store's schema under the ``django_spec_agents_store`` label.

The store shipped as django-pydantic-agent's ``django_pydantic_agent_store`` app,
whose five migrations do not come across. Django keys everything a database holds
about an app on its label: the tables are ``<label>_<model>``, the migration rows
and content types are keyed on it, and permissions, group grants and admin log
entries hang off the content types. Django has no app-label rename, so this one
migration does it.

Its state is the old app's ``0005`` under the new names. Its database work looks at
the connection first:

- **With no old tables**, it creates the nine tables, which is what a new install
  and every test run see.
- **With the old tables present**, it renames what the old label named: the nine
  tables, the seven constraints, the seven named indexes, the content types (so
  permissions and grants follow by foreign key), PostgreSQL's ``_like`` index on
  ``StoredAttachment.file``, and it deletes the old label's rows from
  ``django_migrations``. The old install must be at its ``0005``, so a
  schema behind its state is refused rather than renamed.

What it does not rename are the other objects Django named from the old tables
without a name in the model: the index on ``StoredAttachment.file``, the check
constraint on ``StoredAttachment.size``, ``ConversationAttachment``'s two
foreign-key indexes and constraints, each table's primary key and PostgreSQL's
identity sequences. When a later migration drops or replaces one of these, Django's
schema editors, from 4.2 to 6.0 on SQLite, PostgreSQL and MySQL, find it by asking
the database for the column's constraints, so the old names are harmless to them.
The ``_like`` index is the exception: PostgreSQL's editor drops it by the name it
computes from the current table, which is why it is renamed. On SQLite a constraint
can only change by remaking its table, so there every name comes out new.

Unapplying it renames everything back and records the old app's five migrations
again, which is the schema django-pydantic-agent 0.20 to 0.28 expects, so a
rollback is one ``migrate django_spec_agents_store zero`` before the old pin goes
back. That holds whichever way the migration was applied.
"""

from __future__ import annotations

from typing import Any

import django.db.models.deletion
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.state import ModelState, ProjectState, StateApps

import django_spec_agents.contrib.store.attachment_storage

LABEL = "django_spec_agents_store"
OLD_LABEL = "django_pydantic_agent_store"

# The old app's migrations, in order. The last is the state this one carries, and
# an old install not at it is refused.
OLD_MIGRATIONS = (
    "0001_initial",
    "0002_snapshot_state",
    "0003_attachment_content_hash_and_conversation_links",
    "0004_alter_storedattachment_file",
    "0005_storedmemory_storedmemoryoperation",
)

# The nine models that own a table, each before any model with a foreign key to it.
# ``StoredConversation.attachments`` goes through ``ConversationAttachment``, so
# Django creates no table of its own for it.
MODELS = (
    "storedconversation",
    "storedattachment",
    "conversationattachment",
    "storedrun",
    "storedstepevent",
    "storedsnapshot",
    "storedtooleffect",
    "storedmemory",
    "storedmemoryoperation",
)

# The seven indexes declared without a name. Django froze each name into a
# migration, derived from the table it was on, so the old app's names come from its
# tables and the new names from these.
INDEXES = {
    "django_pyda_owner_i_8b93b5_idx": "django_spec_owner_i_173ba7_idx",
    "django_pyda_owner_i_2ab804_idx": "django_spec_owner_i_d45445_idx",
    "django_pyda_owner_i_46b43c_idx": "django_spec_owner_i_c74857_idx",
    "django_pyda_owner_i_a8ce09_idx": "django_spec_owner_i_2821a0_idx",
    "django_pyda_owner_i_eb059a_idx": "django_spec_owner_i_6c7e72_idx",
    "django_pyda_owner_i_59abe8_idx": "django_spec_owner_i_ee594e_idx",
    "django_pyda_owner_i_b79813_idx": "django_spec_owner_i_0b5512_idx",
}

# The seven constraints carry the package's name as a prefix.
CONSTRAINT_PREFIX = "django_spec_agents_"
OLD_CONSTRAINT_PREFIX = "django_pydantic_agent_"


class OldInstallBehindError(RuntimeError):
    """The old app's tables exist, but its migrations stop short of its ``0005``."""


def _relabel(reference: Any, target: str) -> Any:
    # ``ModelState.from_model`` deconstructs each relation to ``<label>.<model>``,
    # and every relation among the nine stays inside the app. ``Any`` because
    # Django's stubs type the attribute as the model class a rendered field holds,
    # where a model state holds the string.
    return f"{target}.{reference.split('.', 1)[1]}"


def _old_apps(apps: StateApps) -> StateApps:
    """The nine models under the old label, with the old app's names.

    Rendered apart from ``apps``, so that every schema-editor call can be handed
    the model as it is named once the call is done, in either direction. SQLite
    remakes a table from the model it is handed to change a constraint, so that
    model has to carry the names the table is meant to end up with.
    """
    state = ProjectState()
    for name in MODELS:
        model_state = ModelState.from_model(apps.get_model(LABEL, name))
        model_state.app_label = OLD_LABEL
        for field in model_state.fields.values():
            remote = field.remote_field
            if remote is not None:
                remote.model = _relabel(remote.model, OLD_LABEL)
            if isinstance(remote, models.ManyToManyRel):
                remote.through = _relabel(remote.through, OLD_LABEL)
        renamed = {new: old for old, new in INDEXES.items()}
        for index in model_state.options.get("indexes", []):
            index.name = renamed[index.name]
        for constraint in model_state.options.get("constraints", []):
            constraint.name = OLD_CONSTRAINT_PREFIX + constraint.name.removeprefix(
                CONSTRAINT_PREFIX
            )
        state.add_model(model_state)
    return state.apps


def _rename_like_indexes(
    # ``Any``: Django's stubs leave out ``_create_index_name``, which this needs.
    schema_editor: Any,
    source_table: str,
    model: type[models.Model],
) -> None:
    """Rename the ``_like`` indexes PostgreSQL keeps beside a text column's index.

    The one auto-named object Django does not find by introspection: when a later
    migration changes such a column's type, or drops its uniqueness, PostgreSQL's
    schema editor drops the ``_like`` index by the name it computes from the table
    the model names now, and with ``IF EXISTS``. Left under the old table's name,
    the index would survive that drop and sit beside the one created in its place.
    The name is computed the way Django computes it, so it matches what Django will
    look for. Only PostgreSQL creates these, so elsewhere nothing matches.
    """
    connection = schema_editor.connection
    table = model._meta.db_table
    with connection.cursor() as cursor:
        existing = connection.introspection.get_constraints(cursor, table)
    for field in model._meta.local_concrete_fields:
        # ``_create_index_name`` is how Django names this index on creation and on
        # the drop described above; no public spelling of it exists.
        source = schema_editor._create_index_name(source_table, [field.column], suffix="_like")
        if source in existing:
            target = schema_editor._create_index_name(table, [field.column], suffix="_like")
            schema_editor.execute(
                schema_editor.sql_rename_index
                % {
                    "old_name": schema_editor.quote_name(source),
                    "new_name": schema_editor.quote_name(target),
                }
            )


def _move(
    apps: StateApps, schema_editor: BaseDatabaseSchemaEditor, source_label: str, target_label: str
) -> None:
    """Rename every object the source label named to the target label's name."""
    named = {LABEL: apps, OLD_LABEL: _old_apps(apps)}
    source, target = named[source_label], named[target_label]
    for name in MODELS:
        schema_editor.alter_db_table(
            target.get_model(target_label, name),
            source.get_model(source_label, name)._meta.db_table,
            target.get_model(target_label, name)._meta.db_table,
        )
    for name in MODELS:
        _rename_like_indexes(
            schema_editor,
            source.get_model(source_label, name)._meta.db_table,
            target.get_model(target_label, name),
        )
    # Indexes before constraints: on SQLite a constraint change remakes the table
    # with the target model's indexes, after which the source index is gone.
    for name in MODELS:
        model = target.get_model(target_label, name)
        pairs = zip(
            source.get_model(source_label, name)._meta.indexes, model._meta.indexes, strict=True
        )
        for source_index, target_index in pairs:
            schema_editor.rename_index(model, source_index, target_index)
    for name in MODELS:
        model = target.get_model(target_label, name)
        pairs = zip(
            source.get_model(source_label, name)._meta.constraints,
            model._meta.constraints,
            strict=True,
        )
        for source_constraint, target_constraint in pairs:
            schema_editor.remove_constraint(model, source_constraint)
            schema_editor.add_constraint(model, target_constraint)
    # Through this migration's state, which has the contenttypes app; the old
    # label's is rendered with the nine models alone.
    content_type = apps.get_model("contenttypes", "ContentType")
    content_type.objects.using(schema_editor.connection.alias).filter(
        app_label=source_label
    ).update(app_label=target_label)
    content_type.objects.clear_cache()


def forwards(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    connection = schema_editor.connection
    if f"{OLD_LABEL}_storedconversation" not in connection.introspection.table_names():
        for name in MODELS:
            schema_editor.create_model(apps.get_model(LABEL, name))
        return
    recorder = MigrationRecorder(connection)
    if (OLD_LABEL, OLD_MIGRATIONS[-1]) not in recorder.applied_migrations():
        raise OldInstallBehindError(
            f"The {OLD_LABEL} tables are here, but its migration {OLD_MIGRATIONS[-1]} is "
            "not applied, so the schema is behind the one this migration renames. Run "
            "`migrate` under django-pydantic-agent 0.20 or later first, then install "
            "django-spec-agents and run `migrate` again."
        )
    _move(apps, schema_editor, OLD_LABEL, LABEL)
    recorder.migration_qs.filter(app=OLD_LABEL).delete()


def backwards(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _move(apps, schema_editor, LABEL, OLD_LABEL)
    recorder = MigrationRecorder(schema_editor.connection)
    for name in OLD_MIGRATIONS:
        recorder.record_applied(OLD_LABEL, name)


class Migration(migrations.Migration):
    initial = True

    dependencies = [("contenttypes", "0002_remove_content_type_name")]

    operations = [
        # State only: the database half runs second, below, because a database
        # operation inside ``SeparateDatabaseAndState`` sees the state from before
        # these models exist.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="StoredAttachment",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("attachment_id", models.CharField(max_length=255)),
                        ("owner_id", models.CharField(blank=True, default="", max_length=255)),
                        ("thread_id", models.CharField(blank=True, default="", max_length=255)),
                        ("name", models.CharField(blank=True, default="", max_length=255)),
                        ("mime", models.CharField(blank=True, default="", max_length=255)),
                        ("size", models.PositiveBigIntegerField(default=0)),
                        ("sha256", models.CharField(blank=True, default="", max_length=64)),
                        (
                            "file",
                            models.FileField(
                                db_index=True,
                                storage=django_spec_agents.contrib.store.attachment_storage.attachment_storage,
                                upload_to="django_pydantic_agent/attachments/",
                            ),
                        ),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                    ],
                    options={
                        "indexes": [
                            models.Index(
                                fields=["owner_id", "-created_at"],
                                name="django_spec_owner_i_d45445_idx",
                            ),
                            models.Index(
                                fields=["owner_id", "sha256"],
                                name="django_spec_owner_i_c74857_idx",
                            ),
                        ],
                        "constraints": [
                            models.UniqueConstraint(
                                fields=("owner_id", "attachment_id"),
                                name="django_spec_agents_attachment_owner_id_unique",
                            )
                        ],
                    },
                ),
                migrations.CreateModel(
                    name="ConversationAttachment",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        (
                            "attachment",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="conversation_links",
                                to="django_spec_agents_store.storedattachment",
                            ),
                        ),
                    ],
                ),
                migrations.CreateModel(
                    name="StoredConversation",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("thread_id", models.CharField(max_length=255)),
                        ("owner_id", models.CharField(blank=True, default="", max_length=255)),
                        ("title", models.CharField(blank=True, default="", max_length=255)),
                        ("preview", models.TextField(blank=True, default="")),
                        ("messages", models.JSONField(default=list)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                        (
                            "attachments",
                            models.ManyToManyField(
                                blank=True,
                                related_name="conversations",
                                through="django_spec_agents_store.ConversationAttachment",
                                to="django_spec_agents_store.storedattachment",
                            ),
                        ),
                    ],
                ),
                migrations.AddField(
                    model_name="conversationattachment",
                    name="conversation",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attachment_links",
                        to="django_spec_agents_store.storedconversation",
                    ),
                ),
                migrations.CreateModel(
                    name="StoredMemory",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("owner_id", models.CharField(blank=True, default="", max_length=255)),
                        ("path", models.CharField(max_length=500)),
                        ("content", models.TextField(blank=True, default="")),
                        ("version", models.CharField(max_length=32)),
                        (
                            "operation_id",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                    ],
                    options={
                        "constraints": [
                            models.UniqueConstraint(
                                fields=("owner_id", "path"),
                                name="django_spec_agents_memory_owner_path_unique",
                            )
                        ],
                    },
                ),
                migrations.CreateModel(
                    name="StoredMemoryOperation",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("owner_id", models.CharField(blank=True, default="", max_length=255)),
                        ("operation_id", models.CharField(max_length=255)),
                        ("fingerprint", models.CharField(max_length=255)),
                        (
                            "version",
                            models.CharField(blank=True, default=None, max_length=32, null=True),
                        ),
                        ("existed", models.BooleanField(default=False)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                    ],
                    options={
                        "constraints": [
                            models.UniqueConstraint(
                                fields=("owner_id", "operation_id"),
                                name="django_spec_agents_memory_owner_operation_unique",
                            )
                        ],
                    },
                ),
                migrations.CreateModel(
                    name="StoredRun",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("run_id", models.CharField(max_length=255)),
                        ("owner_id", models.CharField(blank=True, default="", max_length=255)),
                        (
                            "conversation_id",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        (
                            "parent_run_id",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        (
                            "agent_name",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        ("metadata", models.JSONField(default=dict)),
                        ("started_at", models.DateTimeField()),
                    ],
                    options={
                        "indexes": [
                            models.Index(
                                fields=["owner_id", "started_at"],
                                name="django_spec_owner_i_2821a0_idx",
                            )
                        ],
                        "constraints": [
                            models.UniqueConstraint(
                                fields=("owner_id", "run_id"),
                                name="django_spec_agents_run_owner_run_unique",
                            )
                        ],
                    },
                ),
                migrations.CreateModel(
                    name="StoredSnapshot",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("run_id", models.CharField(max_length=255)),
                        ("owner_id", models.CharField(blank=True, default="", max_length=255)),
                        ("step_index", models.IntegerField()),
                        ("messages", models.JSONField(default=list)),
                        (
                            "conversation_id",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        (
                            "parent_run_id",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        (
                            "agent_name",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        ("timestamp", models.DateTimeField()),
                        (
                            "state",
                            models.CharField(
                                choices=[
                                    ("complete", "complete"),
                                    ("interrupted", "interrupted"),
                                ],
                                default="complete",
                                max_length=16,
                            ),
                        ),
                    ],
                    options={
                        "indexes": [
                            models.Index(
                                fields=["owner_id", "run_id"],
                                name="django_spec_owner_i_ee594e_idx",
                            )
                        ],
                    },
                ),
                migrations.CreateModel(
                    name="StoredStepEvent",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("run_id", models.CharField(max_length=255)),
                        ("owner_id", models.CharField(blank=True, default="", max_length=255)),
                        ("kind", models.CharField(max_length=32)),
                        ("step_index", models.IntegerField()),
                        ("timestamp", models.DateTimeField()),
                        (
                            "conversation_id",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        (
                            "parent_run_id",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        (
                            "agent_name",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        (
                            "tool_call_id",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        (
                            "tool_name",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        ("error", models.TextField(blank=True, default=None, null=True)),
                        ("metadata", models.JSONField(default=dict)),
                    ],
                    options={
                        "indexes": [
                            models.Index(
                                fields=["owner_id", "run_id"],
                                name="django_spec_owner_i_6c7e72_idx",
                            )
                        ],
                    },
                ),
                migrations.CreateModel(
                    name="StoredToolEffect",
                    fields=[
                        (
                            "id",
                            models.BigAutoField(
                                auto_created=True,
                                primary_key=True,
                                serialize=False,
                                verbose_name="ID",
                            ),
                        ),
                        ("run_id", models.CharField(max_length=255)),
                        ("owner_id", models.CharField(blank=True, default="", max_length=255)),
                        ("tool_call_id", models.CharField(max_length=255)),
                        ("tool_name", models.CharField(max_length=255)),
                        ("status", models.CharField(max_length=16)),
                        ("started_at", models.DateTimeField()),
                        ("ended_at", models.DateTimeField(blank=True, default=None, null=True)),
                        (
                            "idempotency_key",
                            models.CharField(blank=True, default=None, max_length=255, null=True),
                        ),
                        (
                            "effect_summary",
                            models.TextField(blank=True, default=None, null=True),
                        ),
                    ],
                    options={
                        "indexes": [
                            models.Index(
                                fields=["owner_id", "run_id"],
                                name="django_spec_owner_i_0b5512_idx",
                            )
                        ],
                        "constraints": [
                            models.UniqueConstraint(
                                fields=("owner_id", "run_id", "tool_call_id"),
                                name="django_spec_agents_tool_effect_owner_run_call_unique",
                            )
                        ],
                    },
                ),
                migrations.AddIndex(
                    model_name="storedconversation",
                    index=models.Index(
                        fields=["owner_id", "-updated_at"],
                        name="django_spec_owner_i_173ba7_idx",
                    ),
                ),
                migrations.AddConstraint(
                    model_name="storedconversation",
                    constraint=models.UniqueConstraint(
                        fields=("owner_id", "thread_id"),
                        name="django_spec_agents_store_owner_thread_unique",
                    ),
                ),
                migrations.AddConstraint(
                    model_name="conversationattachment",
                    constraint=models.UniqueConstraint(
                        fields=("conversation", "attachment"),
                        name="django_spec_agents_conversation_attachment_unique",
                    ),
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(forwards, backwards),
    ]
