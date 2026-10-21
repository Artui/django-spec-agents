"""The boundary between the agent host and ``spec_tools``, held in both directions.

The package is two halves. The host, the root and everything outside
``spec_tools``, needs pydantic-ai and Django only, and django-admin-agent ships it
with no extra. ``spec_tools`` serves specs as agent tools and needs the ``[specs]``
extra: Django REST Framework, drf-services and the spec kernel. So:

- **The root never imports ``spec_tools``**, nor anything only ``[specs]`` installs.
  A root that did would fail at import on a host-only install, or, where the extra
  happens to be installed, load DRF into every project that never asked for it.
- **``spec_tools`` imports nothing from the host but ``AgentDeps``, and never
  ``contrib.store``.** The tool half reads the acting user off the deps it is
  handed, and a project's own deps type works as long as it has a ``user``; a
  dependency on the store's models would make the tool half need the host's
  state, which a project serving specs to its own agent does not install.

Both are checked in a fresh interpreter, under settings that list neither
``rest_framework`` nor the spec test app. Inside the suite's own process the check
would prove nothing: pytest-django has imported ``rest_framework`` by collection
time. The CI job that installs the package with no extra runs this file alone.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC_TOOLS = ROOT / "django_spec_agents" / "spec_tools"

HOST_ONLY = "tests.host_only_settings"
HOST_ONLY_WITH_STORE = "tests.host_only_store_settings"

# What a bare root import must leave unloaded: the tool half, what only the
# ``[specs]`` extra installs, and the run-loop test doubles, whose module says the
# root never imports them.
NOT_FROM_THE_ROOT = (
    "django_spec_agents.spec_tools",
    "django_spec_agents.testing",
    "rest_framework",
    "rest_framework_services",
    "django_service_specs",
)

# The one host name ``spec_tools`` may import, at its leaf path or the root's.
SPEC_TOOLS_MAY_IMPORT = {
    "django_spec_agents.agent.types.agent_deps.AgentDeps",
    "django_spec_agents.AgentDeps",
}

STORE = "django_spec_agents.contrib.store"


def _is_or_under(name: str, package: str) -> bool:
    # Exact or dotted-prefix: a bare ``startswith`` would count
    # ``rest_framework_services`` as ``rest_framework``.
    return name == package or name.startswith(f"{package}.")


def _modules_after(settings: str, script: str) -> set[str]:
    """Run ``script`` in a fresh interpreter under ``settings``; read its ``sys.modules``."""
    env = {
        **os.environ,
        # Overwritten, never inherited: pytest-django has set the suite's own.
        "DJANGO_SETTINGS_MODULE": settings,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"{script}\nimport json, sys\nprint(json.dumps(sorted(sys.modules)))",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return set(json.loads(result.stdout.splitlines()[-1]))


_BARE_ROOT_IMPORT = "import django_spec_agents\nimport django\ndjango.setup()"


@pytest.mark.parametrize("settings", [HOST_ONLY, HOST_ONLY_WITH_STORE])
def test_a_bare_root_import_loads_neither_spec_tools_nor_the_specs_extra(settings: str) -> None:
    modules = _modules_after(settings, _BARE_ROOT_IMPORT)

    # The interpreter imported the host: an empty list of offenders cannot come
    # from a process that never got as far as the package.
    assert {"django_spec_agents", "django_spec_agents.agent"} <= modules
    loaded = sorted(p for p in NOT_FROM_THE_ROOT if any(_is_or_under(m, p) for m in modules))
    assert loaded == []


@pytest.mark.skipif(
    importlib.util.find_spec("rest_framework") is None,
    reason="spec_tools needs the [specs] extra; the import scan below holds the rule without it",
)
def test_importing_spec_tools_loads_nothing_of_the_store() -> None:
    # The settings list no store app, so a ``spec_tools`` import of the store's
    # models fails the import itself, and one of the package alone is read off
    # ``sys.modules``. What a function-local import would reach only at call
    # time, the scan below sees.
    modules = _modules_after(
        HOST_ONLY, "import django\ndjango.setup()\nimport django_spec_agents.spec_tools"
    )

    assert "django_spec_agents.spec_tools.spec_toolset" in modules
    assert sorted(m for m in modules if _is_or_under(m, STORE)) == []


def _host_imports(source: str) -> list[str]:
    """Each host name ``source`` imports, anywhere in it, as a dotted path.

    Every node is walked, not only the module's top level, so an import inside a
    function or under a guard counts. ``from a.b import c`` reads as ``a.b.c``, so
    ``from django_spec_agents.contrib import store`` names the store.
    """
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # ruff bans every relative form in the package, so a relative import
            # here is one the scan could only place by guessing.
            assert node.level == 0, f"relative import at line {node.lineno}"
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return [
        name
        for name in names
        if _is_or_under(name, "django_spec_agents")
        and not _is_or_under(name, "django_spec_agents.spec_tools")
    ]


def _spec_tools_files() -> list[pathlib.Path]:
    # Found on disk, never by importing ``spec_tools``, which cannot import on a
    # host-only install.
    return sorted(SPEC_TOOLS.rglob("*.py"))


def test_the_scan_reads_every_spec_tools_module() -> None:
    files = {path.relative_to(SPEC_TOOLS).as_posix() for path in _spec_tools_files()}

    assert {"__init__.py", "spec_toolset.py", "spec_capability.py"} <= files


def test_spec_tools_imports_nothing_from_the_host_but_agent_deps() -> None:
    imported = {
        f"{path.relative_to(ROOT).as_posix()}: {name}"
        for path in _spec_tools_files()
        for name in _host_imports(path.read_text())
    }

    store = sorted(entry for entry in imported if _is_or_under(entry.split(": ")[1], STORE))
    assert store == [], "spec_tools imports the store"
    others = sorted(
        entry for entry in imported if entry.split(": ")[1] not in SPEC_TOOLS_MAY_IMPORT
    )
    assert others == []


@pytest.mark.parametrize(
    "source",
    [
        "from django_spec_agents.contrib.store.models import StoredConversation",
        "from django_spec_agents.contrib import store",
        "import django_spec_agents.contrib.store.models",
        "def f():\n    from django_spec_agents.contrib.store.models import StoredRun\n",
        "if True:\n    import django_spec_agents.contrib.store\n",
    ],
    ids=["from-leaf", "from-package", "import", "function-local", "guarded"],
)
def test_the_scan_sees_each_way_of_importing_the_store(source: str) -> None:
    # The check above holds only if the scan sees each form an import can take,
    # inside a function or a block as well as at the top.
    assert any(_is_or_under(name, STORE) for name in _host_imports(source))


def test_the_scan_allows_agent_deps_and_names_any_other_host_import() -> None:
    source = (
        "from django_spec_agents.agent.types.agent_deps import AgentDeps\n"
        "from django_spec_agents import AgentDeps\n"
        "from django_spec_agents.spec_tools.spec_toolset import SpecToolset\n"
        "from django_spec_agents.registry.tool_registry import ToolRegistry\n"
    )

    assert [n for n in _host_imports(source) if n not in SPEC_TOOLS_MAY_IMPORT] == [
        "django_spec_agents.registry.tool_registry.ToolRegistry"
    ]
