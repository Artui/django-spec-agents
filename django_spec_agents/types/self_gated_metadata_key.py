"""The "this tool asks for approval itself" signal on ``ToolDefinition.metadata``.

A tool marked with it raises ``ApprovalRequired`` from its own body, carrying a card built
from the call, so ``ToolGuard`` and ``ChildLineage`` never flip it to ``unapproved``: that
would defer the call before the body runs, and the card would never be built. The guard's
``requires_approval`` still answers ``True`` for it, whatever ``exempt`` says. It lives in
process only and is never stored, so its value follows the package's name.
"""

from __future__ import annotations

SELF_GATED_METADATA_KEY = "django_spec_agents.self_gated"

__all__ = ["SELF_GATED_METADATA_KEY"]
