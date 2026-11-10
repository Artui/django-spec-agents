from __future__ import annotations

import pathlib
import re

import pytest

LOCK = pathlib.Path(__file__).resolve().parent.parent / "uv.lock"

REPLACED = {
    "django-pydantic-agent",
    "djangorestframework-pydantic-ai",
    "djangorestframework-mcp-server",
}

_NAME = re.compile(r'\bname = "([^"]+)"')


def _normalise(name: str) -> str:
    # PEP 503: a spelling with underscores is the same distribution as the normalised name.
    return re.sub(r"[-_.]+", "-", name).lower()


def _named_in(text: str) -> set[str]:
    return {_normalise(name) for name in _NAME.findall(text)}


def test_the_lock_names_no_replaced_distribution() -> None:
    names = _named_in(LOCK.read_text())

    # The scan read the lock, so an empty set cannot mean it was never read.
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
    # Each form a lock can name a distribution in is injected into a copy of the real lock.
    copy = tmp_path / "uv.lock"
    copy.write_text(LOCK.read_text() + injected)

    assert expected in _named_in(copy.read_text()) & REPLACED
