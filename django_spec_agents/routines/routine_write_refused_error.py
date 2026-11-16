from __future__ import annotations

from django.core.exceptions import PermissionDenied


class RoutineWriteRefusedError(PermissionDenied):
    """The caller is anonymous, or the policy's `can_write` refused the write."""
