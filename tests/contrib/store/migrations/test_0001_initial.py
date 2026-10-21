"""The store's one migration, driven both ways on the suite's own database.

Every test database is built by this migration's fresh path, since no old table is
there when pytest-django migrates. These tests drive the other two: unapplying it
hands the schema back under django-pydantic-agent's names, and applying it again
takes the rename path from there, with the old app's five migrations recorded as
an old install has them. The upgrade from django-pydantic-agent's own migrations is
``test_upgrade_from_django_pydantic_agent``'s subject, in a pair of subprocesses.

Each test leaves the database migrated forward, whatever it asserts, because the
rest of the suite shares it.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from django_spec_agents.contrib.store.models import (
    ConversationAttachment,
    StoredAttachment,
    StoredConversation,
)

migration = importlib.import_module("django_spec_agents.contrib.store.migrations.0001_initial")

LABEL = "django_spec_agents_store"
OLD_LABEL = "django_pydantic_agent_store"


def _migrate(target: str | None) -> None:
    # A fresh executor each time: it reads the recorder when it is built.
    MigrationExecutor(connection).migrate([(LABEL, target)])
    ContentType.objects.clear_cache()


def _tables() -> set[str]:
    return set(connection.introspection.table_names())


def _object_names(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.get_constraints(cursor, table))


def _recorded(app: str) -> list[str]:
    return sorted(
        name for (label, name) in MigrationRecorder(connection).applied_migrations() if label == app
    )


@pytest.fixture
def migrated_forward() -> Iterator[None]:
    try:
        yield
    finally:
        if f"{LABEL}_storedconversation" not in _tables():
            recorder = MigrationRecorder(connection)
            recorder.record_applied(OLD_LABEL, migration.OLD_MIGRATIONS[-1])
            _migrate("0001_initial")


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("migrated_forward")
def test_unapplying_restores_the_old_names_and_applying_renames_them_again() -> None:
    conversation = StoredConversation.objects.create(thread_id="t1", owner_id="7")
    attachment = StoredAttachment.objects.create(attachment_id="a1", owner_id="7")
    ConversationAttachment.objects.create(conversation=conversation, attachment=attachment)
    content_types = {
        content_type.model: content_type.pk
        for content_type in ContentType.objects.filter(app_label=LABEL)
    }
    assert len(content_types) == 9
    permission = Permission.objects.get(codename="view_storedconversation")
    group = Group.objects.create(name="readers")
    group.permissions.add(permission)

    _migrate(None)

    assert {f"{OLD_LABEL}_{model}" for model in migration.MODELS} <= _tables()
    assert not {table for table in _tables() if table.startswith(LABEL)}
    assert _recorded(OLD_LABEL) == sorted(migration.OLD_MIGRATIONS)
    assert _recorded(LABEL) == []
    assert {
        content_type.model: content_type.pk
        for content_type in ContentType.objects.filter(app_label=OLD_LABEL)
    } == content_types
    conversation_names = _object_names(f"{OLD_LABEL}_storedconversation")
    assert "django_pydantic_agent_store_owner_thread_unique" in conversation_names
    assert "django_pyda_owner_i_8b93b5_idx" in conversation_names

    _migrate("0001_initial")

    assert {f"{LABEL}_{model}" for model in migration.MODELS} <= _tables()
    assert not {table for table in _tables() if table.startswith(OLD_LABEL)}
    assert _recorded(OLD_LABEL) == []
    assert _recorded(LABEL) == ["0001_initial"]
    assert {
        content_type.model: content_type.pk
        for content_type in ContentType.objects.filter(app_label=LABEL)
    } == content_types
    assert not ContentType.objects.filter(app_label=OLD_LABEL).exists()
    # The grant hangs off the content type by key, so it followed the label.
    assert list(Group.objects.get(name="readers").permissions.all()) == [permission]
    assert StoredConversation.objects.get().attachments.get() == attachment
    names = {name for model in migration.MODELS for name in _object_names(f"{LABEL}_{model}")}
    assert set(migration.INDEXES.values()) <= names
    assert not set(migration.INDEXES) & names
    assert {
        "django_spec_agents_store_owner_thread_unique",
        "django_spec_agents_attachment_owner_id_unique",
        "django_spec_agents_conversation_attachment_unique",
        "django_spec_agents_run_owner_run_unique",
        "django_spec_agents_tool_effect_owner_run_call_unique",
        "django_spec_agents_memory_owner_path_unique",
        "django_spec_agents_memory_owner_operation_unique",
    } <= names
    assert not {name for name in names if name.startswith("django_pydantic_agent_")}


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("migrated_forward")
def test_an_old_install_short_of_its_last_migration_is_refused() -> None:
    _migrate(None)
    MigrationRecorder(connection).migration_qs.filter(
        app=OLD_LABEL, name=migration.OLD_MIGRATIONS[-1]
    ).delete()

    with pytest.raises(migration.OldInstallBehindError) as refused:
        _migrate("0001_initial")

    assert str(refused.value).startswith(
        f"The {OLD_LABEL} tables are here, but its migration "
        "0005_storedmemory_storedmemoryoperation is not applied"
    )
    assert "Run `migrate` under django-pydantic-agent 0.20 or later first" in str(refused.value)
    # Nothing was renamed on the way to the refusal.
    assert f"{OLD_LABEL}_storedconversation" in _tables()
    assert f"{LABEL}_storedconversation" not in _tables()


@pytest.mark.django_db
def test_the_models_need_no_migration_beyond_this_one() -> None:
    # The state this migration carries is the models', so the next change to a
    # model is a migration of its own rather than a silent drift from this one.
    from django.core.management import call_command

    call_command("makemigrations", LABEL, "--check", "--dry-run", verbosity=0)


@pytest.mark.django_db(transaction=True)
def test_a_like_index_is_renamed_to_the_name_django_will_look_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only PostgreSQL makes ``_like`` indexes, and the suite runs on SQLite, so the
    # database is told it has the one an upgraded PostgreSQL install has, and the
    # SQL is collected rather than run. Both names are the ones PostgreSQL showed
    # before and after the upgrade test's run there.
    old_like = "django_pydantic_agent_store_storedattachment_file_39b93faa_like"
    monkeypatch.setattr(
        connection.introspection, "get_constraints", lambda cursor, table: {old_like: {}}
    )
    with connection.schema_editor(collect_sql=True) as schema_editor:
        migration._rename_like_indexes(
            schema_editor, f"{OLD_LABEL}_storedattachment", StoredAttachment
        )

    assert schema_editor.collected_sql == [
        f'ALTER INDEX "{old_like}" RENAME TO '
        '"django_spec_agents_store_storedattachment_file_bb78a77a_like";'
    ]
