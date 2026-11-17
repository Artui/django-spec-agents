from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ChildSessionSpend:
    """The spend budgets every child run carries, as ``pydantic-ai-harness`` ``SpendLimits``.

    ``tree_usd`` caps the whole tree under one root thread, for all time; ``day_usd``
    caps one user's children per day. ``store`` is the harness ``SpendStore`` the
    counters live in, ``Any`` because the harness is optional: it must be one every
    worker shares, never an in-memory store. ``None`` for a budget counts and never stops.
    """

    store: Any
    tree_usd: Decimal | None = None
    day_usd: Decimal | None = None


__all__ = ["ChildSessionSpend"]
