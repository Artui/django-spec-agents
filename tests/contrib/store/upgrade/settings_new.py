"""The new side of the store upgrade: this package's store app, where the old one stood.

Both sides read one SQLite file, named by the test through the environment, so
the second process migrates the database the first one left.
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
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ["STORE_UPGRADE_DATABASE"],
    }
}
