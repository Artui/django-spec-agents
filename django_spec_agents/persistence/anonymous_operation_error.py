from __future__ import annotations


class AnonymousOperationError(Exception):
    """Raised when a model-backed store is asked to act for an anonymous request."""


__all__ = ["AnonymousOperationError"]
