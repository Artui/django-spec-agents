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
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["STORE_UPGRADE_DATABASE"],
        "HOST": os.environ.get("STORE_UPGRADE_HOST", ""),
        "USER": os.environ.get("STORE_UPGRADE_USER", ""),
    }
}
STORAGES = {"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"}}
