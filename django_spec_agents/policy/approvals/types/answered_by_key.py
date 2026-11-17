from __future__ import annotations

ANSWERED_BY_KEY = "answered_by"
"""The key an in-run answerer sets in ``DeferredToolResults.metadata[id]``, read back
from ``ctx.tool_call_metadata`` onto the tool result and the audit record."""


__all__ = ["ANSWERED_BY_KEY"]
