"""A host-only project that installs the store app, as django-admin-agent tells its users to.

The same project as ``host_only_settings`` with
``django_spec_agents.contrib.store`` added, so the root's boundary is also held
where ``django.setup()`` loads the store's models.
"""

from __future__ import annotations

SECRET_KEY = "test"
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_spec_agents.contrib.store",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
