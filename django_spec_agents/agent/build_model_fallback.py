from __future__ import annotations

from typing import Any

from pydantic_ai.capabilities import ModelFallback
from pydantic_ai.models import Model

from django_spec_agents.agent.build_model import build_model


def build_model_fallback(
    *models: str | Model,
    fallback_on: Any = None,
    sticky: bool = False,
) -> ModelFallback[Any]:
    """A `ModelFallback` capability whose models are built now, not mid-outage.

    Pass it in [`AgentConfig.capabilities`][django_spec_agents.AgentConfig]. When the
    agent's model fails, refuses (`finish_reason='content_filter'`) or is cut off at
    `max_tokens` (`finish_reason='length'`), the next model here serves the same request
    step, and the answer's `ModelResponse.model_id` names the model that gave it.

    Each `"provider:name"` string goes through the same builder as the agent's own
    model, so the `gemini:` alias applies and a name no provider resolves, or a provider
    with no key in its environment variable, raises `ImproperlyConfigured` here, at
    configuration time. A bare `ModelFallback("provider:name")` would resolve the string
    only on its first attempt, the moment the primary is already down. The agent's
    `api_key` is never reused: a fallback usually sits at another provider, so a string
    reads that provider's own environment variable, and a model needing an explicit key
    or provider is passed as a built `Model`.

    `fallback_on` and `sticky` pass through to `ModelFallback`; left out, `fallback_on`
    keeps its default (`ModelAPIError`, `ContentFilterError`,
    `OutputTokenLimitExceeded`). With `sticky=True`, a conversation that fell back stays
    on the model that answered, across later runs continuing the stored history.

    An agent spec needs none of this: `ModelFallback.from_spec` builds it from a spec's
    `capabilities` list, resolving its names on attempt.
    """
    if not models:
        raise ValueError("build_model_fallback needs at least one model to fall back to.")
    built = [build_model(model) if isinstance(model, str) else model for model in models]
    if fallback_on is None:
        return ModelFallback(*built, sticky=sticky)
    return ModelFallback(*built, fallback_on=fallback_on, sticky=sticky)


__all__ = ["build_model_fallback"]
