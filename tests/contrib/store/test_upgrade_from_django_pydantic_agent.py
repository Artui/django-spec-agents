"""An installed django-pydantic-agent store, upgraded by one ``migrate``.

The first process migrates a SQLite file through django-pydantic-agent's own five
migrations, under its label, and writes rows into it. The second, with this
package's store app installed instead, runs ``migrate`` and reads the rows back.
They are two processes because the upgrade is a change of installed apps, which a
process sets once. The migration's rename path is measured for coverage by
``tests/contrib/store/migrations/test_0001_initial.py``; this is the test that it
renames the schema django-pydantic-agent actually shipped.

It runs on SQLite. To run the first case on PostgreSQL, create an empty database
and set ``STORE_UPGRADE_ENGINE=django.db.backends.postgresql``,
``STORE_UPGRADE_DATABASE`` to its name and ``STORE_UPGRADE_HOST`` and
``STORE_UPGRADE_USER`` as the server needs, with ``psycopg`` importable.
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[3]
migration = importlib.import_module("django_spec_agents.contrib.store.migrations.0001_initial")
OLD_LABEL = "django_pydantic_agent_store"
LABEL = "django_spec_agents_store"


# What Django named from the old tables without a name in the model, as an upgraded
# PostgreSQL database shows it. The migration renames none of them: when a later
# migration drops or replaces one, Django asks the database for the column's
# constraints rather than computing the name. The ``_like`` index it does drop by
# a computed name is renamed, so it is not here, and the ``alter`` step holds it.
STALE_ON_POSTGRESQL = {
    "django_pydantic_agen_attachment_id_6a60527b_fk_django_py",
    "django_pydantic_agen_conversation_id_9f098291_fk_django_py",
    "django_pydantic_agent_stor_attachment_id_6a60527b",
    "django_pydantic_agent_stor_conversation_id_9f098291",
    "django_pydantic_agent_store_conversationattachment_pkey",
    "django_pydantic_agent_store_storedattachment_file_39b93faa",
    "django_pydantic_agent_store_storedattachment_pkey",
    "django_pydantic_agent_store_storedattachment_size_check",
    "django_pydantic_agent_store_storedconversation_pkey",
    "django_pydantic_agent_store_storedmemory_pkey",
    "django_pydantic_agent_store_storedmemoryoperation_pkey",
    "django_pydantic_agent_store_storedrun_pkey",
    "django_pydantic_agent_store_storedsnapshot_pkey",
    "django_pydantic_agent_store_storedstepevent_pkey",
    "django_pydantic_agent_store_storedtooleffect_pkey",
}


def _step(
    database: pathlib.Path, side: str, *args: str, sqlite: bool = False
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": f"tests.contrib.store.upgrade.settings_{'old' if side == 'old' else 'new'}",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
    }
    if sqlite or "STORE_UPGRADE_ENGINE" not in os.environ:
        env.pop("STORE_UPGRADE_ENGINE", None)
        env["STORE_UPGRADE_DATABASE"] = str(database)
    return subprocess.run(
        [sys.executable, "-m", "tests.contrib.store.upgrade.steps", side, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _ran(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_an_installed_store_comes_across_with_its_rows(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "store.sqlite3"
    before = _ran(_step(database, "old"))
    after = _ran(_step(database, "new"))

    assert [table for table in after["tables"] if table.startswith(OLD_LABEL)] == []
    assert len([table for table in after["tables"] if table.startswith(LABEL)]) == 9
    assert after["recorded"] == [[LABEL, "0001_initial"]]
    # The same rows, moved rather than recreated, so whatever points at them by key
    # still does.
    assert after["content_types"] == before["content_types"]
    assert after["old_content_types"] == 0
    assert after["view_permissions"] == 1
    assert after["granted"] == [[LABEL, "view_storedconversation"]]
    assert after["conversation"] == ["t1", "Kept", [{"kind": "request"}]]
    assert after["attachments"] == [["notes.txt", "django_pydantic_agent/attachments/notes.txt"]]
    assert after["memory"] == [["/notes.md", "remember"]]

    names = {name for table in after["names"].values() for name in table}
    assert set(migration.INDEXES.values()) <= names
    assert {
        "django_spec_agents_store_owner_thread_unique",
        "django_spec_agents_attachment_owner_id_unique",
        "django_spec_agents_conversation_attachment_unique",
        "django_spec_agents_run_owner_run_unique",
        "django_spec_agents_tool_effect_owner_run_call_unique",
        "django_spec_agents_memory_owner_path_unique",
        "django_spec_agents_memory_owner_operation_unique",
    } <= names
    stale = {name for name in names if name.startswith(("django_pyda", OLD_LABEL))}
    # On SQLite a constraint changes only by remaking its table, which derives
    # every name afresh, so not even the auto-named objects keep the old prefix.
    assert stale == (STALE_ON_POSTGRESQL if after["vendor"] == "postgresql" else set())

    # A later change to the column's type drops its ``_like`` index by the name
    # Django computes from the new table and creates one for the new type, so a
    # ``_like`` index left under the old name would survive beside the new one.
    like = _ran(_step(database, "alter"))
    on_postgresql = after["vendor"] == "postgresql"
    assert like == (
        ["django_spec_agents_store_storedattachment_file_bb78a77a_like"] if on_postgresql else []
    )


def test_an_install_short_of_its_last_migration_is_refused(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "store.sqlite3"
    _ran(_step(database, "old", "0004_alter_storedattachment_file", sqlite=True))
    refused = _step(database, "new", sqlite=True)

    assert refused.returncode != 0
    assert "OldInstallBehindError" in refused.stderr
    assert (
        f"The {OLD_LABEL} tables are here, but its migration "
        "0005_storedmemory_storedmemoryoperation is not applied"
    ) in refused.stderr
