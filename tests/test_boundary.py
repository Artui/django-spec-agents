"""The boundary between the agent host and ``spec_tools``, held in both directions.

The package is two halves. The host, the root and everything outside
``spec_tools``, needs pydantic-ai and Django only, and django-admin-agent ships it
with no extra. ``spec_tools`` serves specs as agent tools and needs the ``[specs]``
extra, which installs the spec kernel. So:

- **The root never imports ``spec_tools``**, nor anything only ``[specs]`` or
  ``[drf-mcp]`` installs. A root that did would fail at import on a host-only
  install, or, where an extra happens to be installed, load the kernel or DRF
  into every project that never asked for it.
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
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC_TOOLS = ROOT / "django_spec_agents" / "spec_tools"

# Settings modules, each run from a directory holding it alone. See
# ``_modules_after``.
HOST_ONLY = "host_only_settings"
HOST_ONLY_WITH_STORE = "host_only_store_settings"

# What a bare root import must leave unloaded: the tool half, what only the
# ``[specs]`` and ``[drf-mcp]`` extras install, and the run-loop test doubles,
# whose module says the root never imports them.
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
    """Run ``script`` in a fresh interpreter under ``settings``; read its ``sys.modules``.

    The interpreter runs in a directory holding a copy of the settings module and
    nothing else, with that directory alone on ``PYTHONPATH``, so the package it
    imports is the one installed in the venv: the editable install of the source
    tree in development, and the built package on the host-only install. The
    source tree is never put on the path, which would shadow the latter.
    """
    with tempfile.TemporaryDirectory() as directory:
        shutil.copy(ROOT / "tests" / f"{settings}.py", directory)
        env = {
            # Unmeasured: pytest-cov below 7 starts coverage in every subprocess
            # from its COV_CORE_* variables, and here it would find none of the
            # suite's configuration in the temporary directory, so it would write
            # statement data the parent's branch data cannot be combined with.
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("COV_CORE_", "COVERAGE_"))
            },
            # Overwritten, never inherited: pytest-django has set the suite's own.
            "DJANGO_SETTINGS_MODULE": settings,
            "PYTHONPATH": directory,
        }
        result = subprocess.run(
            [sys.executable, "-c", f"{script}\n{_REPORT}"],
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    if os.environ.get("HOST_ONLY_INSTALL"):
        # On the host-only install the boundary is about what was installed, so
        # the package the interpreter read is the venv's, never the source tree.
        package, site_packages = report["package"], report["site_packages"]
        assert pathlib.Path(package).is_relative_to(site_packages), (package, site_packages)
    return set(report["modules"])


_REPORT = """
import json, sys, sysconfig
print(json.dumps({
    "modules": sorted(sys.modules),
    "package": sys.modules["django_spec_agents"].__file__,
    "site_packages": sysconfig.get_paths()["purelib"],
}))
"""

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


def _package_imports(source: str) -> list[str]:
    """Each name of this package ``source`` imports, anywhere in it, as a dotted path.

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
    return [name for name in names if _is_or_under(name, "django_spec_agents")]


def _host_imports(source: str) -> list[str]:
    """Each host name ``source`` imports: this package's, outside ``spec_tools``."""
    return [
        name
        for name in _package_imports(source)
        if not _is_or_under(name, "django_spec_agents.spec_tools")
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


def _imports_under(source: str, *packages: str) -> set[str]:
    """Each name ``source`` imports from any of ``packages``, as a dotted path."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {name for name in names if any(_is_or_under(name, p) for p in packages)}


# ``spec_tools`` reads every spec through the kernel, so it imports nothing from
# Django REST Framework or drf-services.
_DRF = ("rest_framework", "rest_framework_services")


def _drf_imports() -> set[str]:
    return {
        f"{path.relative_to(SPEC_TOOLS).as_posix()}: {name}"
        for path in _spec_tools_files()
        for name in _imports_under(path.read_text(), *_DRF)
    }


def test_spec_tools_imports_neither_drf_nor_drf_services() -> None:
    """Pinned as an empty set, so a new import of either package fails here,
    naming the module and the name it imports."""
    assert _drf_imports() == set()


def test_the_drf_scan_sees_a_guarded_import() -> None:
    source = "def f():\n    from rest_framework_services.types import QueryParam\n"

    assert _imports_under(source, *_DRF) == {"rest_framework_services.types.QueryParam"}


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


PACKAGE = ROOT / "django_spec_agents"

# The one host module that reaches the tool half, through a function-local import
# under the optional-dependency exception, so loading it never loads the half.
MAY_IMPORT_SPEC_TOOLS = {"django_spec_agents/integrations/build_spec_capability.py"}


def _host_files() -> list[pathlib.Path]:
    return sorted(path for path in PACKAGE.rglob("*.py") if SPEC_TOOLS not in path.parents)


def test_the_host_reaches_spec_tools_in_one_module_and_testing_in_none() -> None:
    # The fresh-interpreter test above sees only what importing the root loads, so
    # an import inside a host function, run later, would pass it. This scan reads
    # every host module for the two the root must never reach: ``spec_tools``
    # outside the one module that bridges to it, and ``testing`` anywhere: its
    # doubles are for a project's tests, and its module says the root never
    # imports it.
    files = {path.relative_to(ROOT).as_posix(): path for path in _host_files()}
    assert {"django_spec_agents/__init__.py", *MAY_IMPORT_SPEC_TOOLS} <= files.keys()

    imported = [
        (relative, name)
        for relative, path in files.items()
        for name in _package_imports(path.read_text())
    ]

    spec_tools = sorted(
        f"{relative}: {name}"
        for relative, name in imported
        if _is_or_under(name, "django_spec_agents.spec_tools")
        and relative not in MAY_IMPORT_SPEC_TOOLS
    )
    assert spec_tools == [], "a host module imports spec_tools"
    testing = sorted(
        f"{relative}: {name}"
        for relative, name in imported
        if _is_or_under(name, "django_spec_agents.testing")
    )
    assert testing == [], "a host module imports testing"
    # The allowance is used, so the scan reads the import it permits.
    assert (
        "django_spec_agents/integrations/build_spec_capability.py",
        "django_spec_agents.spec_tools.SpecCapability",
    ) in imported
