from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class SpendBudget:
    """One ceiling a [`SpendLimitsConfig`][django_spec_agents.SpendLimitsConfig] enforces.

    ``usd`` and ``tokens`` are the ceilings, either, both or neither (a budget with
    neither only counts). ``window`` is the period the counter covers: one ``run``, one
    ``conversation``, a UTC ``day`` or ``month``, or the ``total`` for all time.
    ``per_user`` counts each acting user (``ctx.deps.user``) apart, every run with no
    authenticated user sharing one ``anonymous`` counter; otherwise the whole mount shares
    one. ``warn_at`` is the fraction past which the harness reports a warning, never a stop.
    ``name`` tells budgets on one window apart.
    """

    name: str
    usd: Decimal | None = None
    tokens: int | None = None
    window: Literal["run", "conversation", "day", "month", "total"] = "day"
    per_user: bool = False
    warn_at: float | None = None


__all__ = ["SpendBudget"]
