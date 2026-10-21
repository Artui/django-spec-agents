"""The two processes of the store upgrade, one per side.

``python -m tests.contrib.store.upgrade.steps old [<migration>]`` runs under the old
side's settings: it migrates django-pydantic-agent's store app, to its last
migration or to the one named, and writes what an installed database holds: a
conversation linked to an attachment, a memory row where the schema has one, the
nine content types, a permission and a group granted it. It prints the content
types' keys.

``python -m tests.contrib.store.upgrade.steps new`` runs under the new side's
settings: it runs ``migrate`` and prints what the database holds afterwards, read
through this package's own models.

Each runs in its own interpreter, because a process has one set of installed apps
and the upgrade is a change of exactly that. The Django imports sit inside the
functions because they need ``django.setup()`` to have run.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import django

OLD_LABEL = "django_pydantic_agent_store"
LABEL = "django_spec_agents_store"

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


def install_old(target: str | None) -> dict[str, Any]:
    from django.contrib.auth.models import Group, Permission
    from django.contrib.contenttypes.models import ContentType
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    if target is None:
        call_command("migrate", verbosity=0)
    else:
        call_command("migrate", "contenttypes", verbosity=0)
        call_command("migrate", "auth", verbosity=0)
        call_command("migrate", OLD_LABEL, target, verbosity=0)
    # The app has no models of its own, so its rows are written through the
    # models its migrations describe, exactly as they stood.
    old = MigrationLoader(connection).project_state().apps
    conversation = old.get_model(OLD_LABEL, "StoredConversation").objects.create(
        thread_id="t1", owner_id="7", title="Kept", messages=[{"kind": "request"}]
    )
    attachment = old.get_model(OLD_LABEL, "StoredAttachment").objects.create(
        attachment_id="a1",
        owner_id="7",
        name="notes.txt",
        file="django_pydantic_agent/attachments/notes.txt",
    )
    old.get_model(OLD_LABEL, "ConversationAttachment").objects.create(
        conversation=conversation, attachment=attachment
    )
    if target is None:
        old.get_model(OLD_LABEL, "StoredMemory").objects.create(
            owner_id="7", path="/notes.md", content="remember", version="v1"
        )
    # An installed app gets these from ``post_migrate``, which finds no models in
    # the fixture app, so they are written here as the old install has them.
    content_types = {
        model: ContentType.objects.create(app_label=OLD_LABEL, model=model).pk for model in MODELS
    }
    permission = Permission.objects.create(
        content_type_id=content_types["storedconversation"],
        codename="view_storedconversation",
        name="Can view stored conversation",
    )
    Group.objects.create(name="readers").permissions.add(permission)
    return {"content_types": content_types}


def upgrade() -> dict[str, Any]:
    from django.contrib.auth.models import Group, Permission
    from django.contrib.contenttypes.models import ContentType
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    from django_spec_agents.contrib.store.models import StoredConversation, StoredMemory

    call_command("migrate", verbosity=0)
    tables = connection.introspection.table_names()
    with connection.cursor() as cursor:
        names = {
            table: sorted(connection.introspection.get_constraints(cursor, table))
            for table in tables
            if table.startswith(LABEL)
        }
    conversation = StoredConversation.objects.get()
    return {
        "vendor": connection.vendor,
        "tables": sorted(tables),
        "recorded": sorted(
            [app, name]
            for (app, name) in MigrationRecorder(connection).applied_migrations()
            if app in (OLD_LABEL, LABEL)
        ),
        "content_types": {
            content_type.model: content_type.pk
            for content_type in ContentType.objects.filter(app_label=LABEL)
        },
        "old_content_types": ContentType.objects.filter(app_label=OLD_LABEL).count(),
        "view_permissions": Permission.objects.filter(codename="view_storedconversation").count(),
        "granted": [
            [permission.content_type.app_label, permission.codename]
            for permission in Group.objects.get(name="readers").permissions.all()
        ],
        "conversation": [conversation.thread_id, conversation.title, conversation.messages],
        "attachments": [
            [attachment.name, attachment.file.name] for attachment in conversation.attachments.all()
        ],
        "memory": list(StoredMemory.objects.values_list("path", "content")),
        "names": names,
    }


def main(argv: list[str]) -> None:
    django.setup()
    step, *rest = argv
    result = install_old(rest[0] if rest else None) if step == "old" else upgrade()
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
