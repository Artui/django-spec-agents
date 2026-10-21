.PHONY: help init test test-host-only lint lint-fix format format-check type-check deps-bump docs-serve docs-build release-bump release-publish release-publish-prepare release-publish-finalize

help:
	@echo "Available targets:"
	@echo "  init             Sync deps (all groups) and install pre-commit hooks"
	@echo "  test             Run pytest with coverage (100% required)"
	@echo "  test-host-only   Run the boundary tests on an install with no extra"
	@echo "  lint             Run ruff check + ty check"
	@echo "  lint-fix         Auto-fix lint issues with ruff"
	@echo "  format           Format with ruff"
	@echo "  format-check     Verify formatting"
	@echo "  type-check       Run ty over the package"
	@echo "  deps-bump        Upgrade pinned dependencies"
	@echo "  docs-serve       Live-reload docs at http://localhost:8000 (needs mkdocs.yml)"
	@echo "  docs-build       Build docs into ./site (strict — fails on broken links)"
	@echo "  release-bump     Bump version files + CHANGELOG. Usage: make release-bump VERSION=X.Y.Z"
	@echo "  release-publish  prepare → uv publish → finalize (workstation release)"
	@echo "  release-publish-prepare   Run by release.yml on push to main (no-op unless bumped)"
	@echo "  release-publish-finalize  Tag vX.Y.Z + create GitHub Release after PyPI publish"

init:
	uv sync --all-groups
	uv run pre-commit install

test:
	uv run pytest

# The package with no extra, the install django-admin-agent ships, in a venv of
# its own: ``uv run`` would sync the dev group, which brings every extra's
# packages. Only pytest joins it, not pytest-django, whose plugin would load the
# suite's settings and stop at ``rest_framework``. So the run takes no coverage
# gate, ``addopts`` being where ``--cov`` lives, and the ini's Django and asyncio
# keys are unknown to it, which is expected rather than a warning to read.
HOST_ONLY_VENV := build/host-only-venv

test-host-only:
	uv venv --clear --quiet --python 3.13 $(HOST_ONLY_VENV)
	uv pip install --quiet --python $(HOST_ONLY_VENV)/bin/python . pytest
	$(HOST_ONLY_VENV)/bin/python -c "import importlib.util, sys; found = [m for m in ('rest_framework', 'rest_framework_services', 'django_service_specs') if importlib.util.find_spec(m)]; sys.exit(f'not a host-only install: {found}' if found else 0)"
	$(HOST_ONLY_VENV)/bin/python -m pytest -q -p no:cacheprovider -o addopts="" \
		-W ignore::pytest.PytestConfigWarning tests/test_boundary.py

lint:
	uv run ruff check .
	uv run ty check django_spec_agents

lint-fix:
	uv run ruff check --fix .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check --diff .

type-check:
	uv run ty check django_spec_agents

deps-bump:
	uvx uv-upx upgrade run --profile with_pinned

docs-serve:
	uv run --group docs mkdocs serve

docs-build:
	uv run --group docs mkdocs build --strict

release-bump:
	@if [ -z "$(VERSION)" ]; then \
		echo "Usage: make release-bump VERSION=X.Y.Z"; exit 1; \
	fi
	uvx bump-my-version bump --new-version "$(VERSION)" patch
	@echo ""
	@echo "Bumped to $(VERSION). Edit CHANGELOG.md to fill the new section,"
	@echo "review with 'git diff', then run 'make release-publish'."

# Release pipeline. Version lives in django_spec_agents/version.py
# (pyproject pulls it in via [tool.hatch.version] dynamic).
RELEASE_PACKAGE_NAME := django-spec-agents
RELEASE_VERSION_FILES := django_spec_agents/version.py|^__version__[^=]*= *

release-publish:
	@PACKAGE_NAME='$(RELEASE_PACKAGE_NAME)' \
	VERSION_FILES="$$(printf '$(RELEASE_VERSION_FILES)')" \
		bash scripts/release-publish.sh all

release-publish-prepare:
	@PACKAGE_NAME='$(RELEASE_PACKAGE_NAME)' \
	VERSION_FILES="$$(printf '$(RELEASE_VERSION_FILES)')" \
		bash scripts/release-publish.sh prepare

release-publish-finalize:
	@PACKAGE_NAME='$(RELEASE_PACKAGE_NAME)' \
	VERSION_FILES="$$(printf '$(RELEASE_VERSION_FILES)')" \
		bash scripts/release-publish.sh finalize
