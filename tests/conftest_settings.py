"""Django settings used by the test suite."""

from __future__ import annotations

import os
import tempfile

SECRET_KEY = "test"
DEBUG = False
ALLOWED_HOSTS = ["*"]
USE_TZ = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    # Opt-in reference store app — exercises the shipped models + migration.
    "django_spec_agents.contrib.store",
    # DRF, for the dev-only ``[drf-mcp]`` bridge and its tests, which serve
    # drf-services specs over drf-mcp; ``spec_tools`` imports neither.
    "rest_framework",
    # The models the spec_tools half's kernel specs serve.
    "tests.spec_tools.testapp",
]

# Skip migrations for the spec_tools test app — pytest-django builds the schema
# from models.
MIGRATION_MODULES = {"testapp": None}

# ``TEST["NAME"]`` is what makes a connection genuinely closable, and it is load
# bearing rather than a preference. Django's SQLite backend *ignores* ``close()``
# on an in-memory database, to avoid destroying it -- and for SQLite the test
# database is in-memory whatever ``NAME`` says unless ``TEST["NAME"]`` names a
# file. Under the default, any assertion about a connection being released
# passes for a working fix and for a no-op alike, which is how an attempt at the
# off-HTTP connection cleanup in ``spec_toolset`` came out green without having
# been verified at all. Two tests assert ``not connection.is_in_memory_db()``
# before asserting anything else, so reverting this line fails loudly instead of
# quietly making them vacuous. ``tests/spec_tools/conftest.py`` also reads this
# file to check that no test leaves rows committed.
#
# Built rather than written down: no committed file in this family may carry an
# absolute local path. The pid keeps two concurrent runs off one file; Django
# deletes it at the end of the session.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {"NAME": os.path.join(tempfile.gettempdir(), f"pai_test_{os.getpid()}.sqlite3")},
    }
}

MIDDLEWARE: list[str] = []

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# In-memory file storage so the reference attachment store's FileField never
# touches disk during tests.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# Read by the DRF the ``[drf-mcp]`` bridge brings.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}
