from __future__ import annotations

from typing import Literal, TypeAlias

ReportOutcome: TypeAlias = Literal["ok", "timeout", "budget", "failed", "cancelled", "error"]

__all__ = ["ReportOutcome"]
