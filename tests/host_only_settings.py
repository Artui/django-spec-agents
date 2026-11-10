from __future__ import annotations

SECRET_KEY = "test"
USE_TZ = True
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
