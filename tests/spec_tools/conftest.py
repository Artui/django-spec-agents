from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connections

# Stashed on ``config`` rather than kept in module globals, so the state belongs
# to the run that produced it and no test can reach it.
_DB_PATH = pytest.StashKey[str]()
# What a clean database holds, captured once. Not empty: migrations leave content
# types and permissions behind.
_PRISTINE = pytest.StashKey[dict[str, int]]()
# What the *next* test is entitled to find. Tracks ``_PRISTINE`` until something
# leaks; see ``_assert_nothing_outlived`` for why it then moves.
_EXPECTED = pytest.StashKey[dict[str, int]]()
_CURRENT_TEST = pytest.StashKey[str]()
_LAST_TEST_CHECKED = pytest.StashKey[bool]()


def _committed_row_counts(path: str) -> dict[str, int]:
    """Row counts for every table, read over a connection of this check's own."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }
    finally:
        connection.close()


def _assert_nothing_outlived(config: pytest.Config, nodeid: str) -> None:
    """Fail ``nodeid`` if the committed contents of the database moved under it."""
    counts = _committed_row_counts(config.stash[_DB_PATH])
    expected = config.stash[_EXPECTED]
    pristine = config.stash[_PRISTINE]
    config.stash[_EXPECTED] = counts
    if counts in (expected, pristine):
        return
    changed = {
        table: (expected.get(table, 0), count)
        for table, count in counts.items()
        if count != expected.get(table, 0)
    }
    raise AssertionError(
        f"{nodeid} left rows committed in the test database -- table: (before, after) "
        f"{changed}. pytest-django rolls back the connection belonging to the test's own "
        "thread and nothing else, so a write that ran anywhere else -- anything reached "
        "through sync_to_async, which is every dispatch this package makes -- committed, "
        "and a later test will read it as though that test were broken. Mark this test "
        "@pytest.mark.django_db(transaction=True), whose teardown truncates."
    )


@pytest.fixture(scope="session", autouse=True)
def _committed_row_baseline(request: pytest.FixtureRequest, django_db_setup: Any) -> Iterator[None]:
    """Capture the pristine contents, and check the run's final test.

    Depending on ``django_db_setup`` is what makes the baseline exist before the
    first teardown asks for it, instead of being captured off whichever test ran
    first -- which would fold that test's own leak into the baseline and exempt
    it forever.

    The same dependency is what lets this fixture check the *last* test of the
    run. Session fixtures are finalized during that test's teardown, in reverse
    order of setup, so this one runs while ``django_db_setup`` has yet to destroy
    the database -- and by the time the teardown hook below is called there is no
    database left to read. Nothing else in the run is positioned to ask.
    """
    path = str(connections["default"].settings_dict["NAME"])
    request.config.stash[_DB_PATH] = path
    request.config.stash[_PRISTINE] = _committed_row_counts(path)
    request.config.stash[_EXPECTED] = dict(request.config.stash[_PRISTINE])
    yield
    request.config.stash[_LAST_TEST_CHECKED] = True
    _assert_nothing_outlived(request.config, request.config.stash[_CURRENT_TEST])


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Remember which test is running, so the fixture above can name it."""
    item.config.stash[_CURRENT_TEST] = item.nodeid


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    """Check after every test, and blame the test that actually wrote the row."""
    if item.config.stash.get(_EXPECTED, None) is None:
        return
    if item.config.stash.get(_LAST_TEST_CHECKED, False):
        return
    _assert_nothing_outlived(item.config, item.nodeid)
