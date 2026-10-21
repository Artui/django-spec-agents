"""The new side of the store upgrade: this package's store app, where the old one stood.

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
    "django_spec_agents.contrib.store",
]
DATABASES = {
    "default": {
        "ENGINE": os.environ.get("STORE_UPGRADE_ENGINE", "django.db.backends.sqlite3"),
        "NAME": os.environ["STORE_UPGRADE_DATABASE"],
        "HOST": os.environ.get("STORE_UPGRADE_HOST", ""),
        "USER": os.environ.get("STORE_UPGRADE_USER", ""),
    }
}
