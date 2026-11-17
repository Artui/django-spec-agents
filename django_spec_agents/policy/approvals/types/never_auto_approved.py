from __future__ import annotations

NEVER_AUTO_APPROVED: frozenset[str] = frozenset(
    {
        "create_routine",
        "update_routine",
        "create_skill",
        "update_skill",
        "disable_skill",
        "enable_skill",
    }
)
"""Tools no in-run answerer approves, under a name list, ``'all'`` or a grant: their
approval card is the grant itself, so only a person answers it."""


__all__ = ["NEVER_AUTO_APPROVED"]
