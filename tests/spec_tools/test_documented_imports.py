"""What ``django_spec_agents.spec_tools`` exports, and the import lines its pages teach.

The package exports the toolset, the capability and the conventions, and
nothing it does not own: the one ``AgentDeps`` is the host's, at the package
root, and the spec, its parameters, its registry and its server arguments are
the kernel's, imported from ``django_service_specs``. A second spelling of
either is how a reader comes to believe there are two classes.

The pages are checked by **reading them and running what they show**, this
package's imports and the kernel's alike, as
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

# An import statement inside a fenced Python block, over one line or several,
# from this package or from the kernel the pages declare their specs on.
_IMPORT = re.compile(
    r"^from\s+((?:django_spec_agents|django_service_specs)[\w.]*)"
    r"\s+import\s+\(?\s*([\w,\s]+?)\s*\)?$",
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


@pytest.mark.parametrize(
    "name", ["AgentDeps", "Parameter", "ServerArguments", "SpecRegistry", "__version__"]
)
def test_a_name_another_package_owns_is_not_re_exported(name):
    assert not hasattr(spec_tools, name)


def test_the_spec_source_alias_stays_at_its_module_path():
    """A subclass annotating its ``specs`` argument imports the alias from
    ``spec_toolset``, where it is defined, and no ``__all__`` exports it."""
    import django_spec_agents
    from django_spec_agents.spec_tools import spec_toolset

    assert spec_toolset.SpecSource is not None
    for module in (django_spec_agents, spec_tools, spec_toolset):
        assert "SpecSource" not in module.__all__


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
