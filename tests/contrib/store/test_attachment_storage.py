from __future__ import annotations

import pytest
from django.core.files.storage import (
    InMemoryStorage,
    InvalidStorageError,
    default_storage,
    storages,
)
from django.test import override_settings

from django_spec_agents.contrib.store.attachment_storage import attachment_storage
from django_spec_agents.contrib.store.models import StoredAttachment
from django_spec_agents.types.attachment_storage_alias import ATTACHMENT_STORAGE_ALIAS


def test_falls_back_to_the_projects_lazy_default() -> None:
    # The same lazy object Django hands out, not a copy resolved when this app was imported.
    assert attachment_storage() is default_storage


def test_the_model_field_follows_the_default_through_a_settings_change() -> None:
    # An ``override_settings`` on a storage key would split the field from ``default_storage``.
    field = StoredAttachment._meta.get_field("file")
    with override_settings(STATIC_URL="/other-static/"):
        assert field.storage is default_storage
    assert field.storage is default_storage


def test_the_field_is_wired_to_this_hook() -> None:
    field = StoredAttachment._meta.get_field("file")
    assert field._storage_callable is attachment_storage


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        ATTACHMENT_STORAGE_ALIAS: {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    }
)
def test_a_configured_alias_wins() -> None:
    resolved = attachment_storage()
    assert isinstance(resolved, InMemoryStorage)
    # The point of the alias: it is *not* the global default any more.
    assert resolved is not storages["default"]


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        ATTACHMENT_STORAGE_ALIAS: {"BACKEND": "myproject.storages.NoSuchStorag"},
    }
)
def test_a_misspelled_backend_raises_rather_than_falling_back() -> None:
    # Both fail alike; catching this would send uploads to the public default.
    with pytest.raises(InvalidStorageError):
        attachment_storage()
