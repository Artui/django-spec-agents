from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness.spend_limits import InMemorySpendStore, SpendLimitExceeded

from django_spec_agents.agent.agent_factory import build_agent
from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps
from django_spec_agents.policy.spend.build_spend_limits import build_spend_limits
from django_spec_agents.policy.spend.types.spend_budget import SpendBudget
from django_spec_agents.policy.spend.types.spend_limits_config import SpendLimitsConfig
from django_spec_agents.registry.decorator import tool
from django_spec_agents.registry.tool_registry import ToolRegistry


def look() -> str:
    """Look around."""
    return "nothing yet"


def _looks_then_answers(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    if len(messages) > 1:
        return ModelResponse(parts=[TextPart("done")])
    return ModelResponse(parts=[ToolCallPart("look", {})])


def _dollar(_response: Any) -> Decimal:
    return Decimal("1")


def _agent(limits: SpendLimitsConfig | None) -> Any:
    reg = ToolRegistry()
    tool(reg)(look)
    return build_agent(
        reg, AgentConfig(model=FunctionModel(_looks_then_answers), spend_limits=limits)
    )


def _limits(store: Any, **budget: Any) -> SpendLimitsConfig:
    return SpendLimitsConfig(
        store=store, budgets=[SpendBudget("cap", usd=Decimal("1"), **budget)], price=_dollar
    )


def _user(pk: int) -> Any:
    return SimpleNamespace(pk=pk, is_authenticated=True)


async def test_a_spent_budget_refuses_the_next_request() -> None:
    agent = _agent(_limits(InMemorySpendStore(), window="run"))

    with pytest.raises(SpendLimitExceeded):
        await agent.run("Go.", deps=AgentDeps(user=None))


async def test_without_spend_limits_the_run_completes() -> None:
    result = await _agent(None).run("Go.", deps=AgentDeps(user=None))

    assert result.output == "done"


def _two_dollars(store: Any, *, per_user: bool) -> SpendLimitsConfig:
    budget = SpendBudget("cap", usd=Decimal("2"), per_user=per_user)
    return SpendLimitsConfig(store=store, budgets=[budget], price=_dollar)


async def test_a_per_user_budget_counts_each_user_apart() -> None:
    agent = _agent(_two_dollars(InMemorySpendStore(), per_user=True))

    first = await agent.run("Go.", deps=AgentDeps(user=_user(1)))
    second = await agent.run("Go.", deps=AgentDeps(user=_user(2)))

    assert (first.output, second.output) == ("done", "done")


async def test_a_shared_budget_counts_every_user_together() -> None:
    agent = _agent(_two_dollars(InMemorySpendStore(), per_user=False))

    await agent.run("Go.", deps=AgentDeps(user=_user(1)))
    with pytest.raises(SpendLimitExceeded):
        await agent.run("Go.", deps=AgentDeps(user=_user(2)))


@pytest.mark.parametrize("anonymous", [None, AnonymousUser()])
async def test_runs_with_no_authenticated_user_share_one_counter(anonymous: Any) -> None:
    config = _limits(InMemorySpendStore(), per_user=True)
    limits: Any = build_spend_limits(config)
    agent = _agent(config)

    with pytest.raises(SpendLimitExceeded):
        await agent.run("Go.", deps=AgentDeps(user=anonymous))

    [status] = await limits.status(scope="anonymous")
    assert status.spent.usd == Decimal("1")


def test_each_budget_field_reaches_the_harness_budget() -> None:
    limits: Any = build_spend_limits(
        SpendLimitsConfig(
            store=InMemorySpendStore(),
            budgets=[SpendBudget("tenant", tokens=500, window="month", per_user=True, warn_at=0.5)],
        )
    )

    [budget] = limits.budgets
    assert (budget.name, budget.usd, budget.tokens, budget.window, budget.warn_at) == (
        "tenant",
        None,
        500,
        "month",
        0.5,
    )
    assert budget.scope(SimpleNamespace(deps=AgentDeps(user=_user(7)))) == "user:7"
    assert limits.price is None
