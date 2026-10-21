"""A project that installs the agent host and nothing else.

The settings of a host-only install, as django-admin-agent's users run it: no
Django REST Framework, no drf-services and no app serving specs, so nothing here
loads the ``[specs]`` half on the package's behalf. The boundary tests run under it
in a fresh interpreter, where the suite's own settings would have imported
``rest_framework`` already.

It lists no store app either, as spec-twins does, so a ``spec_tools`` import of the
store's models fails here as it would there: Django refuses a model whose app is
not installed.
"""

from __future__ import annotations

SECRET_KEY = "test"
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
