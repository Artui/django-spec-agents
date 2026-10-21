"""An installed django-pydantic-agent store, upgraded by one ``migrate``.

The first process migrates a SQLite file through django-pydantic-agent's own five
migrations, under its label, and writes rows into it. The second, with this
package's store app installed instead, runs ``migrate`` and reads the rows back.
They are two processes because the upgrade is a change of installed apps, which a
process sets once. The migration's rename path is measured for coverage by
``tests/contrib/store/migrations/test_0001_initial.py``; this is the test that it
renames the schema django-pydantic-agent actually shipped.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[3]
OLD_LABEL = "django_pydantic_agent_store"
LABEL = "django_spec_agents_store"


def _step(database: pathlib.Path, side: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": f"tests.contrib.store.upgrade.settings_{side}",
        "STORE_UPGRADE_DATABASE": str(database),
        "PYTHONPATH": str(ROOT),
    }
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
    assert "django_spec_agents_store_owner_thread_unique" in names
    assert "django_spec_owner_i_c74857_idx" in names
    # On SQLite a constraint changes only by remaking its table, which derives
    # every name afresh, so not even the auto-named objects keep the old prefix.
    assert [name for name in names if name.startswith(("django_pyda", OLD_LABEL))] == []


def test_an_install_short_of_its_last_migration_is_refused(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "store.sqlite3"
    _ran(_step(database, "old", "0004_alter_storedattachment_file"))
    refused = _step(database, "new")

    assert refused.returncode != 0
    assert "OldInstallBehindError" in refused.stderr
    assert (
        f"The {OLD_LABEL} tables are here, but its migration "
        "0005_storedmemory_storedmemoryoperation is not applied"
    ) in refused.stderr
