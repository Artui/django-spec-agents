"""The store app's label, and every database name Django derives from it.

Django keys installed state on the label: table names are ``<label>_<model>``,
content types and migration rows are keyed on it, and the seven indexes declared
without a name take theirs from the table. So these assertions are what the
upgrade migration renames *to*; a change here is a schema change for every
installed database.
"""

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
    # Django truncates the table name to its first eleven characters when it
    # names an index, so ``django_spec_agents_store_...`` yields ``django_spec``.
    names = [
        index.name
        for model in apps.get_app_config(LABEL).get_models()
        for index in model._meta.indexes
    ]
    assert len(names) == 7
    assert all(name.startswith("django_spec_") for name in names), names
