from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django_spec_agents.policy.spend.types.spend_budget import SpendBudget


@dataclass(frozen=True)
class SpendLimitsConfig:
    """``pydantic-ai-harness``'s ``SpendLimits``, as [`build_agent`][django_spec_agents.build_agent]
    composes it (the ``[harness]`` extra).

    Each response's price is added to every budget's counter, and once a counter is spent
    the next model request is refused with ``SpendLimitExceeded``, a ``UsageLimitExceeded``:
    the run ends as a usage limit ends it, which a child session reports as ``budget``.
    ``store`` is the harness ``BatchSpendStore`` the counters live in, ``Any`` because the
    harness is optional; ``DefaultSpendStore`` keeps them in the database, so every worker
    shares them. ``price`` prices a response before the ``genai-prices`` registry does,
    ``None`` falling through to it, for a self-hosted model or a negotiated rate.
    """

    store: Any
    budgets: Sequence[SpendBudget]
    price: Callable[[Any], Decimal | None] | None = None


__all__ = ["SpendLimitsConfig"]
