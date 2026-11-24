from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class LoopDetectionConfig:
    """``pydantic-ai-harness``'s ``DetectLoops``, as [`build_agent`][django_spec_agents.build_agent]
    composes it (the ``[harness]`` extra).

    ``action="nudge"`` tells the model which loop it is in and lets the run carry on.
    ``action="error"`` ends the run with ``LoopDetectedError``, which a child session
    reports as ``failed``, its text the loop's description. Each threshold is how many
    repeats report a loop, ``None`` turning that check off; ``window`` is how many recent
    tool calls the call checks read. ``stream_checks`` keeps the checks for a response that
    repeats itself as it streams.
    """

    action: Literal["nudge", "error"] = "nudge"
    window: int = 10
    repeated_call_threshold: int | None = 5
    repeated_result_threshold: int | None = 3
    alternating_calls_threshold: int | None = 3
    repeated_text_threshold: int | None = 3
    stream_checks: bool = True


__all__ = ["LoopDetectionConfig"]
