from __future__ import annotations

from django_spec_agents import DESTRUCTIVE_METADATA_KEY
from django_spec_agents.types.destructive_metadata_key import (
    DESTRUCTIVE_METADATA_KEY as LEAF_DESTRUCTIVE_METADATA_KEY,
)


def test_the_destructive_metadata_key_names_this_package() -> None:
    # In-process only: it rides ``ToolDefinition.metadata`` between a bridge and
    # ``ToolGuard`` and is never stored, so it follows the package's name.
    assert DESTRUCTIVE_METADATA_KEY == "django_spec_agents.destructive"
    assert LEAF_DESTRUCTIVE_METADATA_KEY is DESTRUCTIVE_METADATA_KEY
