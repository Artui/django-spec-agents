from __future__ import annotations


class IdentifierTooLongError(ValueError):
    """Raised before a store write when an id is longer than the column that holds it.

    Its message names the field and the limit; nothing was written."""


__all__ = ["IdentifierTooLongError"]
