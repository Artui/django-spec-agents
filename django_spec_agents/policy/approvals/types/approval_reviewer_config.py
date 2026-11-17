from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ApprovalReviewerConfig:
    """A second model that may approve a gated call in the person's place.

    [`build_agent`][django_spec_agents.build_agent] composes a
    [`ReviewedApprovals`][django_spec_agents.ReviewedApprovals] from it, which needs the
    ``[harness]`` extra. The reviewer answers ``question`` about each call waiting for
    approval: ``no`` runs the call, marked ``reviewer:<model>``; ``yes``, ``unsure`` or a
    failed review leaves it for the person, as without a reviewer. It never denies a call.

    ``tools`` names the tools it reviews, or ``'all'`` for every approval request,
    those a ``ToolGuardrail`` or ``ToolCallJudge`` raises and tools added later
    included. Under either, the tools in
    [`NEVER_AUTO_APPROVED`][django_spec_agents.NEVER_AUTO_APPROVED] are never reviewed.
    """

    model: Any
    """The reviewing model: a ``"provider:name"`` string or a ``Model`` instance."""

    question: str
    """A yes/no risk question, where ``no`` approves the call."""

    tools: Sequence[str] | Literal["all"]
    """The tool names to review, or ``'all'``. Required, so a project names what its
    question was written for."""


__all__ = ["ApprovalReviewerConfig"]
