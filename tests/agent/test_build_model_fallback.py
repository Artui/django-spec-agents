from __future__ import annotations

from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from pydantic_ai import Agent
from pydantic_ai.capabilities import ModelFallback
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, TextPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.google import GoogleModel

from django_spec_agents import build_model_fallback


def _down(messages: list[Any], info: AgentInfo) -> ModelResponse:
    raise ModelHTTPError(503, "primary", "overloaded")


def _answers(text: str, calls: list[str]) -> FunctionModel:
    def answer(messages: list[Any], info: AgentInfo) -> ModelResponse:
        calls.append(text)
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(answer, model_name=text)


def _finishes(reason: Any) -> FunctionModel:
    def answer(messages: list[Any], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("partial")], finish_reason=reason)

    return FunctionModel(answer, model_name=f"primary-{reason}")


def test_strings_are_built_now_through_build_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-env")
    fallback = build_model_fallback("anthropic:claude-sonnet-4-5", "gemini:gemini-2.0-flash")
    assert isinstance(fallback, ModelFallback)
    assert [type(model) for model in fallback.models] == [AnthropicModel, GoogleModel]


def test_a_name_no_provider_resolves_fails_at_configuration() -> None:
    with pytest.raises(ImproperlyConfigured, match="bogus:model"):
        build_model_fallback("bogus:model")


def test_a_provider_with_no_key_fails_at_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured, match="anthropic:claude-sonnet-4-5"):
        build_model_fallback("anthropic:claude-sonnet-4-5")


def test_a_built_model_is_kept_as_is() -> None:
    model = _answers("backup", [])
    assert build_model_fallback(model).models == [model]


def test_no_models_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one model"):
        build_model_fallback()


def test_an_outage_is_answered_by_the_fallback_which_model_id_names() -> None:
    calls: list[str] = []
    agent = Agent(
        FunctionModel(_down, model_name="primary"),
        capabilities=[build_model_fallback(_answers("backup", calls))],
    )
    result = agent.run_sync("hi")
    assert result.output == "backup"
    response = result.all_messages()[-1]
    assert isinstance(response, ModelResponse)
    assert response.model_id == "function:backup"


@pytest.mark.parametrize("reason", ["content_filter", "length"])
def test_a_refusal_or_a_cut_off_answer_falls_back_by_default(reason: str) -> None:
    agent = Agent(_finishes(reason), capabilities=[build_model_fallback(_answers("backup", []))])
    assert agent.run_sync("hi").output == "backup"


def test_fallback_on_narrows_what_falls_back() -> None:
    agent = Agent(
        _finishes("length"),
        capabilities=[build_model_fallback(_answers("backup", []), fallback_on=(ModelAPIError,))],
    )
    assert agent.run_sync("hi").output == "partial"


@pytest.mark.parametrize(("sticky", "first_asked"), [(True, ["backup"]), (False, [])])
def test_sticky_survives_a_stored_history(sticky: bool, first_asked: list[str]) -> None:
    primary_calls: list[str] = []
    backup_calls: list[str] = []

    def flaky(messages: list[Any], info: AgentInfo) -> ModelResponse:
        primary_calls.append("primary")
        if len(primary_calls) == 1:
            raise ModelHTTPError(503, "primary", "overloaded")
        return ModelResponse(parts=[TextPart("primary")])

    agent = Agent(
        FunctionModel(flaky, model_name="primary"),
        capabilities=[build_model_fallback(_answers("backup", backup_calls), sticky=sticky)],
    )
    first = agent.run_sync("hi")
    # The step store's encoding; the payload has to carry model_id for sticky to find it.
    stored = ModelMessagesTypeAdapter.dump_python(first.all_messages(), mode="json")
    assert stored[-1]["model_id"] == "function:backup"
    history = ModelMessagesTypeAdapter.validate_python(stored)

    backup_calls.clear()
    agent.run_sync("again", message_history=history)
    assert backup_calls == first_asked
    assert len(primary_calls) == (1 if sticky else 2)
