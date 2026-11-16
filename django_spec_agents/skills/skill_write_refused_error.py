from __future__ import annotations

from django.core.exceptions import PermissionDenied


class SkillWriteRefusedError(PermissionDenied):
    """The policy's `can_write` refused the write."""
