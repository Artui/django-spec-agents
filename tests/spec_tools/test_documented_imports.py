from __future__ import annotations

import importlib
import pathlib
import re

import pytest

import django_spec_agents.spec_tools as spec_tools

DOCS = pathlib.Path(__file__).resolve().parents[2] / "docs" / "spec-tools"
# The page for a project moving from the old packages teaches the same imports,
# for both halves, from outside the spec-tools section.
MOVING = DOCS.parent / "moving.md"

# An import statement inside a fenced Python block, from this package or from
# the kernel the pages declare their specs on: a parenthesised list over as many
# lines as it takes, or a bare one ending its line. One lazy group for both
# stops at the first line end, so it reads one name of a parenthesised list.
_IMPORT = re.compile(
    r"^from\s+((?:django_spec_agents|django_service_specs)[\w.]*)"
    r"\s+import\s+(?:\(([^)]*)\)|([\w, ]+)$)",
    re.MULTILINE,
)


def _imports_in(block: str) -> list[tuple[str, str]]:
    """Each ``(module, name)`` one Python block imports from either package."""
    return [
        (module, name.strip())
        for module, listed, bare in _IMPORT.findall(block)
        for name in (listed or bare).split(",")
        if name.strip()
    ]


def _documented_imports() -> list[tuple[str, str, str]]:
    found = []
    for page in [*sorted(DOCS.glob("*.md")), MOVING]:
        blocks = re.findall(r"^```python\n(.*?)^```", page.read_text(), re.MULTILINE | re.DOTALL)
        for block in blocks:
            found.extend((page.name, module, name) for module, name in _imports_in(block))
    return found


def test_the_package_exports_what_it_owns():
    assert sorted(spec_tools.__all__) == [
        "AGENT_TRANSPORT",
        "AgentConventions",
        "SpecCapability",
        "SpecToolset",
    ]


@pytest.mark.parametrize(
    "name", ["AgentDeps", "Parameter", "ServerArguments", "SpecRegistry", "__version__"]
)
def test_a_name_another_package_owns_is_not_re_exported(name):
    assert not hasattr(spec_tools, name)


def test_the_spec_source_alias_stays_at_its_module_path():
    """A subclass imports the alias from ``spec_toolset``, where it is defined."""
    import django_spec_agents
    from django_spec_agents.spec_tools import spec_toolset

    assert spec_toolset.SpecSource is not None
    for module in (django_spec_agents, spec_tools, spec_toolset):
        assert "SpecSource" not in module.__all__


def test_a_parenthesised_import_yields_every_name_it_lists():
    """The pages import kernel names a block at a time, one per line."""
    block = (
        "from django_service_specs import (\n    Parameter,\n    Parameters,\n"
        "    SpecRegistry,\n)\n"
    )

    assert _imports_in(block) == [
        ("django_service_specs", "Parameter"),
        ("django_service_specs", "Parameters"),
        ("django_service_specs", "SpecRegistry"),
    ]


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
