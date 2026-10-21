from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from django_spec_agents.agent.build_model import build_model


@pytest.mark.parametrize(
    ("name", "model_class"),
    [
        ("anthropic:claude-sonnet-4-5", AnthropicModel),
        # The bare ``openai:`` prefix is Pydantic-AI's Responses API model;
        # ``openai-chat:`` selects Chat Completions.
        ("openai:gpt-4o", OpenAIResponsesModel),
        ("openai-chat:gpt-4o", OpenAIChatModel),
        # Any prefix Pydantic-AI knows works on the API_KEY path, with no
        # hand-maintained provider table.
        ("openai-responses:gpt-5", OpenAIResponsesModel),
        ("google:gemini-2.0-flash", GoogleModel),
        # ``gemini:`` is this package's alias for Pydantic-AI's ``google:``.
        ("gemini:gemini-2.0-flash", GoogleModel),
    ],
)
def test_builds_the_model_the_prefix_names_from_an_api_key(
    name: str, model_class: type[Model]
) -> None:
    assert isinstance(build_model(name, api_key="sk-test"), model_class)


def test_a_provider_instance_is_used_as_is() -> None:
    provider = AnthropicProvider(api_key="from-instance")
    model = build_model("anthropic:claude-sonnet-4-5", provider=provider)
    assert isinstance(model, AnthropicModel)


@pytest.mark.parametrize(
    "name",
    [
        # Pydantic-AI 2.x does not infer a provider from a bare name, however
        # recognisable it is.
        "claude-sonnet-4-5",
        "no-such-model",
        "bogus:model",
    ],
)
def test_a_name_no_provider_resolves_points_at_the_provider_setting(name: str) -> None:
    with pytest.raises(ImproperlyConfigured, match="provider"):
        build_model(name, api_key="k")


def test_the_error_opens_with_the_packages_name() -> None:
    # The prefix is how a reader of a deploy log tells which package refused the
    # configuration, so it names the distribution a project installs.
    with pytest.raises(ImproperlyConfigured) as raised:
        build_model("bogus:model", api_key="k")
    assert str(raised.value).startswith("django-spec-agents: could not build model 'bogus:model'")
