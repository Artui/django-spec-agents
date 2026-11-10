from __future__ import annotations

from django.apps import apps

LABEL = "django_spec_agents_store"

# The nine models that own a table. ``StoredConversation.attachments`` goes
# through ``ConversationAttachment``, so Django creates no tenth table for it.
MODELS = (
    "storedconversation",
    "storedattachment",
    "conversationattachment",
    "storedrun",
    "storedstepevent",
    "storedsnapshot",
    "storedtooleffect",
    "storedmemory",
    "storedmemoryoperation",
)


def test_the_app_is_labelled_after_the_package() -> None:
    config = apps.get_app_config(LABEL)
    assert config.name == "django_spec_agents.contrib.store"
    assert config.verbose_name == "django-spec-agents stores"


def test_the_nine_tables_are_named_from_the_label() -> None:
    config = apps.get_app_config(LABEL)
    tables = sorted(model._meta.db_table for model in config.get_models())
    assert tables == sorted(f"{LABEL}_{model}" for model in MODELS)


def test_the_seven_constraints_carry_the_packages_prefix() -> None:
    names = [
        constraint.name
        for model in apps.get_app_config(LABEL).get_models()
        for constraint in model._meta.constraints
    ]
    assert len(names) == 7
    assert all(name.startswith("django_spec_agents_") for name in names), names


def test_the_seven_named_indexes_are_derived_from_the_new_tables() -> None:
    # Django truncates an index name to its first eleven characters, so the name is ``django_spec``.
    names = [
        index.name
        for model in apps.get_app_config(LABEL).get_models()
        for index in model._meta.indexes
    ]
    assert len(names) == 7
    assert all(name.startswith("django_spec_") for name in names), names
