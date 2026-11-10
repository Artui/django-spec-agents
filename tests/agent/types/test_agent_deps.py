from __future__ import annotations

import dataclasses
import re
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from pydantic_ai.ui import StateHandler

from django_spec_agents.agent.types.agent_config import AgentConfig
from django_spec_agents.agent.types.agent_deps import AgentDeps


class _Doc(BaseModel):
    body: str = ""


def test_carries_the_acting_user() -> None:
    user = SimpleNamespace(name="alice")
    assert AgentDeps(user=user).user is user


def test_the_acting_user_has_to_be_stated() -> None:
    """Pydantic-AI never validates ``deps=``, so a default here would be an unauthenticated run."""
    with pytest.raises(TypeError):
        AgentDeps()  # type: ignore[call-arg]


def test_an_unauthenticated_run_says_so_in_one_word() -> None:
    """Nothing is forbidden, only made explicit: the anonymous run is still one argument away."""
    deps = AgentDeps(user=None)
    assert (deps.user, deps.state, deps.progress) == (None, None, None)


def test_the_docstring_only_names_settings_that_exist() -> None:
    """``AgentConfig.deps_factory`` raises ``TypeError``; that parameter lives on the transport."""
    named = set(re.findall(r"AgentConfig\.(\w+)", AgentDeps.__doc__ or ""))
    fields = {f.name for f in dataclasses.fields(AgentConfig)}

    assert named <= fields, f"AgentDeps names non-existent AgentConfig fields: {named - fields}"


def test_carries_a_progress_sink_for_the_run() -> None:
    """A spec's ``progress(...)`` calls are read off ``ctx.deps.progress``."""
    reports: list[tuple[int, int | None]] = []

    def sink(progress: int, *, total: int | None = None, **_: object) -> None:
        reports.append((progress, total))

    deps = AgentDeps(user=None, progress=sink)
    deps.progress(1, total=2)  # type: ignore[misc]

    assert reports == [(1, 2)]


def test_carries_a_context_of_its_own_for_the_run() -> None:
    """``call_context`` is a plain dict, a new one per deps, never one shared default."""
    first, second = AgentDeps(user=None), AgentDeps(user=None)

    assert getattr(first, "call_context", None) == {}
    assert getattr(first, "call_context", None) is not getattr(second, "call_context", None)


class TestStateHandlerConformance:
    """Pydantic-AI decides whether a run receives ``RunAgentInput.state`` by
    ``isinstance(deps, StateHandler)``. These pin the two things that check
    needs, so shared-state support doesn't need the deps redesigned."""

    def test_satisfies_the_protocol(self) -> None:
        assert isinstance(AgentDeps(user=None), StateHandler)

    def test_state_is_assignable(self) -> None:
        """The adapter assigns ``deps.state`` directly, so this record cannot be frozen."""
        deps = AgentDeps(user=None)
        deps.state = {"document": "hello"}

        assert deps.state == {"document": "hello"}

    def test_is_a_dataclass(self) -> None:
        """The protocol requires ``__dataclass_fields__``."""
        assert dataclasses.is_dataclass(AgentDeps)

    def test_a_pydantic_model_seeds_validation(self) -> None:
        """The adapter validates state against ``type(deps.state)``: seeding an instance opts in."""
        deps = AgentDeps(user=None, state=_Doc())
        assert isinstance(deps.state, BaseModel)
