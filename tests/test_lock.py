"""The lock names neither distribution this package replaces.

``django-spec-agents`` carries ``django-pydantic-agent`` and
``djangorestframework-pydantic-ai`` as one package, under its own import path. With
either old distribution in the lock, its classes would sit in the same process as
this package's under the same names, and a test importing the old path by mistake
would test the old code and pass. Neither is a dependency of anything this package
installs, so the lock should name neither, as a package or as any package's
dependency.

The lock is read as text rather than as TOML: ``tomllib`` arrives in Python 3.11
and the floor is 3.10. Every place a lock names a distribution, a ``[[package]]``
entry, a dependency, an optional or dev dependency and the ``requires-dist``
metadata alike, spells it ``name = "<name>"``, so one pattern reads them all.
"""

from __future__ import annotations

import pathlib
import re

import pytest

LOCK = pathlib.Path(__file__).resolve().parent.parent / "uv.lock"

REPLACED = {"django-pydantic-agent", "djangorestframework-pydantic-ai"}

_NAME = re.compile(r'\bname = "([^"]+)"')


def _normalise(name: str) -> str:
    # PEP 503: the lock writes normalised names, and a hand edit or another tool
    # need not, so a spelling with underscores is the same distribution.
    return re.sub(r"[-_.]+", "-", name).lower()


def _named_in(text: str) -> set[str]:
    return {_normalise(name) for name in _NAME.findall(text)}


def test_the_lock_names_neither_replaced_distribution() -> None:
    names = _named_in(LOCK.read_text())

    # The scan read the lock it was meant to: this package and its one required
    # agent dependency are in it, so an empty set cannot pass for a clean one.
    assert {"django-spec-agents", "pydantic-ai-slim"} <= names
    assert names.isdisjoint(REPLACED), sorted(names & REPLACED)


_AS_A_PACKAGE = """
[[package]]
name = "django-pydantic-agent"
version = "0.28.2"
source = { registry = "https://pypi.org/simple" }
"""

_AS_A_DEPENDENCY = """
[[package]]
name = "some-consumer"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "djangorestframework_pydantic_ai" },
]
"""


@pytest.mark.parametrize(
    ("injected", "expected"),
    [
        (_AS_A_PACKAGE, "django-pydantic-agent"),
        (_AS_A_DEPENDENCY, "djangorestframework-pydantic-ai"),
    ],
    ids=["as-a-package", "as-a-dependency"],
)
def test_the_scan_finds_a_replaced_distribution_injected_into_the_lock(
    tmp_path: pathlib.Path, injected: str, expected: str
) -> None:
    # The check above holds only if the scan sees each form a lock can name a
    # distribution in, so each is injected into a copy of the real lock.
    copy = tmp_path / "uv.lock"
    copy.write_text(LOCK.read_text() + injected)

    assert expected in _named_in(copy.read_text()) & REPLACED
