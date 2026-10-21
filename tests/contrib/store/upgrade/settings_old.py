"""The old side of the store upgrade: django-pydantic-agent's store app alone.

It is there by its label and its five migrations.

Both sides read one database, named by the test through the environment, so the
second process migrates the database the first one left. It is a SQLite file
unless ``STORE_UPGRADE_ENGINE`` names another backend, as the upgrade test's
PostgreSQL run does.
"""

from __future__ import annotations

import os

SECRET_KEY = "test"
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "tests.contrib.store.upgrade.legacy_store",
]
DATABASES = {
    "default": {
        "ENGINE": os.environ.get("STORE_UPGRADE_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.environ["STORE_UPGRADE_DATABASE"],
        "HOST": os.environ.get("STORE_UPGRADE_HOST", ""),
        "USER": os.environ.get("STORE_UPGRADE_USER", ""),
    }
}
