from __future__ import annotations

import os

SECRET_KEY = "test"
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_spec_agents",
    "tests.routines.testapp",
]
MIGRATION_MODULES = {"auth": None, "contenttypes": None, "routines_testapp": None}
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["STORE_UPGRADE_DATABASE"],
        "HOST": os.environ.get("STORE_UPGRADE_HOST", ""),
        "PORT": os.environ.get("STORE_UPGRADE_PORT", ""),
        "USER": os.environ.get("STORE_UPGRADE_USER", ""),
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
