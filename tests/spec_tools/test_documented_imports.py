"""What ``django_spec_agents.spec_tools`` exports, and the import lines its pages teach.

The package exports the toolset, the capability and the conventions, and
nothing it does not own: the one ``AgentDeps`` is the host's, at the package
root, and ``UrlKwarg`` and ``QueryParam`` are drf-services' classes, imported
from ``rest_framework_services``. A second spelling of either is how a reader
comes to believe there are two classes.

The pages are checked by **reading them and running what they show**, as
``tests/contrib/store/test_documented_imports.py`` does for the store's page: a
test importing the right names passes just as happily while a page teaches a
spelling that raises.
"""

from __future__ import annotations

import importlib
import pathlib
import re

import pytest

import django_spec_agents.spec_tools as spec_tools

DOCS = pathlib.Path(__file__).resolve().parents[2] / "docs" / "spec-tools"

# An import statement inside a fenced Python block, over one line or several.
_IMPORT = re.compile(
    r"^from\s+(django_spec_agents[\w.]*)\s+import\s+\(?\s*([\w,\s]+?)\s*\)?$",
    re.MULTILINE,
)


def _documented_imports() -> list[tuple[str, str, str]]:
    found = []
    for page in sorted(DOCS.glob("*.md")):
        blocks = re.findall(r"^```python\n(.*?)^```", page.read_text(), re.MULTILINE | re.DOTALL)
        for block in blocks:
            for module, names in _IMPORT.findall(block):
                found.extend(
                    (page.name, module, name.strip()) for name in names.split(",") if name.strip()
                )
    return found


def test_the_package_exports_what_it_owns():
    assert sorted(spec_tools.__all__) == ["AgentConventions", "SpecCapability", "SpecToolset"]


@pytest.mark.parametrize("name", ["AgentDeps", "QueryParam", "UrlKwarg", "__version__"])
def test_a_name_another_package_owns_is_not_re_exported(name):
    assert not hasattr(spec_tools, name)


def test_the_pages_teach_imports():
    # Guards the test below against a regex that silently matches nothing.
    assert len(_documented_imports()) >= 10


@pytest.mark.parametrize(
    ("page", "module", "name"),
    _documented_imports(),
    ids=lambda value: value,
)
def test_every_documented_import_resolves(page, module, name):
    assert hasattr(importlib.import_module(module), name), f"{page}: from {module} import {name}"
