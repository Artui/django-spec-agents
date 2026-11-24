from __future__ import annotations

from typing import Any

from pydantic_ai.capabilities import AbstractCapability

from django_spec_agents.policy.loops.types.loop_detection_config import LoopDetectionConfig


def build_detect_loops(config: LoopDetectionConfig) -> AbstractCapability[Any]:
    """The ``DetectLoops`` ``config`` describes; needs ``[harness]``."""
    from pydantic_ai_harness.detect_loops import DetectLoops

    stream: dict[str, Any] = {} if config.stream_checks else {"stream_detector": None}
    return DetectLoops(
        action=config.action,
        window=config.window,
        repeated_call_threshold=config.repeated_call_threshold,
        repeated_result_threshold=config.repeated_result_threshold,
        alternating_calls_threshold=config.alternating_calls_threshold,
        repeated_text_threshold=config.repeated_text_threshold,
        **stream,
    )


__all__ = ["build_detect_loops"]
