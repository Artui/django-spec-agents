from __future__ import annotations

from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import RunContext

from django_spec_agents.policy.spend.types.spend_budget import SpendBudget
from django_spec_agents.policy.spend.types.spend_limits_config import SpendLimitsConfig


def build_spend_limits(config: SpendLimitsConfig) -> AbstractCapability[Any]:
    """The ``SpendLimits`` ``config`` describes; needs ``[harness]``."""
    from pydantic_ai_harness.spend_limits import SpendLimits

    return SpendLimits(
        budgets=[_budget(budget) for budget in config.budgets],
        store=config.store,
        price=config.price,
    )


def _budget(budget: SpendBudget) -> Any:
    from pydantic_ai_harness.spend_limits import Budget

    return Budget(
        name=budget.name,
        usd=budget.usd,
        tokens=budget.tokens,
        window=budget.window,
        scope=_user_key if budget.per_user else None,
        warn_at=budget.warn_at,
    )


def _user_key(ctx: RunContext[Any]) -> str:
    user: Any = getattr(ctx.deps, "user", None)
    if not getattr(user, "is_authenticated", False):
        return "anonymous"
    return f"user:{user.pk}"


__all__ = ["build_spend_limits"]
