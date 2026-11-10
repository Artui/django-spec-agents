from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import warnings
from typing import Any

import pytest
from django_service_specs import Parameter, Parameters, ServiceSpec, Unrestricted
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from django_spec_agents import AgentDeps
from django_spec_agents.spec_tools import SpecToolset
from django_spec_agents.testing import (
    instruction_capturing_model,
    streaming_tool_calling_model,
    tool_calling_model,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Run in a fresh interpreter: by collection time this process has imported the
# kernel and django-spec-mcp for the suite, so a check made here proves nothing.
_IMPORTS_OF_THE_DOUBLES = """
import sys
import django_spec_agents
assert "django_spec_agents.testing" not in sys.modules, "the package root imported the doubles"
import django_spec_agents.testing
print(sorted(
    name for name in sys.modules
    if name.partition(".")[0]
    in ("django_service_specs", "django_spec_mcp", "rest_framework", "rest_framework_mcp")
    or name.startswith("django_spec_agents.spec_tools")
))
"""


def test_the_doubles_import_without_the_specs_extra():
    """The doubles import on pydantic-ai alone, with no kernel or DRF import."""
    env = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORTS_OF_THE_DOUBLES],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"


def _toolset(service, name="t", **spec_kwargs):
    # ``atomic=False`` because none of these services touch the ORM.
    return SpecToolset(
        {
            name: ServiceSpec(
                service=service, permissions=[Unrestricted()], atomic=False, **spec_kwargs
            )
        }
    )


def _ping(user: Any) -> Any:
    """Ping."""
    return {"ok": True}


async def test_tool_calling_model_drives_one_call_and_stops():
    seen = {}

    def ping(user):
        """Ping."""
        seen["user"] = user
        return {"ok": True}

    agent = Agent(tool_calling_model("t"), deps_type=AgentDeps, toolsets=[_toolset(ping)])

    result = await agent.run("go", deps=AgentDeps(user="alice"))

    assert result.output == "done"
    assert seen["user"] == "alice"


async def test_final_text_is_the_run_output():
    agent = Agent(
        tool_calling_model("t", final_text="all set"),
        deps_type=AgentDeps,
        toolsets=[_toolset(_ping)],
    )

    assert (await agent.run("go", deps=AgentDeps(user="u"))).output == "all set"


# The one argument the retrying services read.
_MODE = Parameters.of(Parameter("mode", "string", required=True))


async def test_retry_args_default_to_repeating_the_first_arguments():
    """Documented as fine for *did the retry arrive* and wrong for recovery."""
    calls: list[str] = []

    def always_retries(mode, user):
        """Always asks for another attempt."""
        calls.append(mode)
        raise ModelRetry("never satisfied")

    toolset = _toolset(always_retries, reads=_MODE)
    agent = Agent(tool_calling_model("t", {"mode": "bad"}), deps_type=AgentDeps, toolsets=[toolset])

    with pytest.raises(UnexpectedModelBehavior):
        await agent.run("go", deps=AgentDeps(user="u"))

    assert calls == ["bad", "bad"], "the default repeated the first arguments"


async def test_instruction_capturing_model_records_what_the_toolset_contributed():
    captured: dict[str, Any] = {}
    agent = Agent(
        instruction_capturing_model(captured), deps_type=AgentDeps, toolsets=[_toolset(_ping)]
    )

    result = await agent.run("go", deps=AgentDeps(user="u"))

    assert result.output == "done"
    # The toolset's conventions arrive on the request, not on the toolset.
    assert captured["instructions"] is not None


async def test_instruction_capturing_model_honours_final_text():
    captured: dict[str, Any] = {}
    agent = Agent(instruction_capturing_model(captured, final_text="ok"), deps_type=AgentDeps)

    assert (await agent.run("go", deps=AgentDeps(user="u"))).output == "ok"


async def test_the_streaming_double_serves_a_streamed_run() -> None:
    """The reason this double exists: the other one cannot serve one at all."""
    agent = Agent(
        streaming_tool_calling_model("t"),
        deps_type=AgentDeps,
        toolsets=[_toolset(_ping)],
    )

    async with agent.run_stream("go", deps=AgentDeps(user="alice")) as run:
        output = await run.get_output()

    assert output == "done"


async def test_the_streaming_double_still_serves_a_plain_run() -> None:
    """Both functions are supplied, so one double covers either transport."""
    agent = Agent(
        streaming_tool_calling_model("t"),
        deps_type=AgentDeps,
        toolsets=[_toolset(_ping)],
    )

    result = await agent.run("go", deps=AgentDeps(user="alice"))

    assert result.output == "done"


async def test_the_streaming_double_retries_in_the_same_order() -> None:
    """The branch order is shared with the non-streamed double, not copied."""
    attempts: list[str] = []

    def picky(mode, user):
        """Refuses the first attempt and accepts the second."""
        attempts.append(mode)
        if len(attempts) == 1:
            raise ModelRetry("try again")
        return {"mode": mode}

    toolset = _toolset(picky, reads=_MODE)
    agent = Agent(
        streaming_tool_calling_model("t", {"mode": "first"}, retry_args={"mode": "second"}),
        deps_type=AgentDeps,
        toolsets=[toolset],
    )

    async with agent.run_stream("go", deps=AgentDeps(user="u")) as run:
        await run.get_output()

    assert attempts == ["first", "second"], "the streamed double retried out of order"


def _legacy_retry_prompt() -> RetryPromptPart:
    # Deprecated from pydantic-ai 2.60, which no longer emits it.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return RetryPromptPart(content="try again", tool_name="t", tool_call_id="c1")


def _retried_tool_return() -> ToolReturnPart:
    # pydantic-ai 2.60 and later records a retry or failed validation as the call's return.
    return ToolReturnPart(tool_name="t", content="try again", tool_call_id="c1", outcome="retried")


@pytest.mark.parametrize(
    "retry",
    [_legacy_retry_prompt, _retried_tool_return],
    ids=["retry-prompt", "retried-tool-return"],
)
@pytest.mark.parametrize("double", [tool_calling_model, streaming_tool_calling_model])
def test_a_retry_in_either_shape_gets_the_retry_args(retry: Any, double: Any) -> None:
    """Whichever shape the installed pydantic-ai writes a retry in."""
    model = double("t", {"mode": "first"}, retry_args={"mode": "second"})
    history = [
        ModelRequest(parts=[UserPromptPart("go")]),
        ModelResponse(parts=[ToolCallPart(tool_name="t", args={"mode": "first"})]),
        ModelRequest(parts=[retry()]),
    ]

    (part,) = model.function(history, None).parts

    assert isinstance(part, ToolCallPart), f"the double ended the run on a retry: {part!r}"
    assert part.args == {"mode": "second"}


def test_a_successful_tool_return_still_ends_the_run() -> None:
    """The other side of the retry check: an ordinary return is not a retry."""
    model = tool_calling_model("t", {"mode": "first"}, final_text="finished")
    history = [
        ModelRequest(parts=[UserPromptPart("go")]),
        ModelResponse(parts=[ToolCallPart(tool_name="t", args={"mode": "first"})]),
        ModelRequest(parts=[ToolReturnPart(tool_name="t", content="ok", tool_call_id="c1")]),
    ]

    (part,) = model.function(history, None).parts

    assert isinstance(part, TextPart)
    assert part.content == "finished"
