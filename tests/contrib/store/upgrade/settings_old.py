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
