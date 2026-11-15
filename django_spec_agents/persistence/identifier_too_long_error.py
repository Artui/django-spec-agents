from __future__ import annotations


class IdentifierTooLongError(ValueError):
    """Raised before a store write when an id is longer than the column that holds it.

    Its message names the field and the limit; nothing was written. The default
    stores also set ``field``, ``length`` and ``limit``; each is ``None`` when the
    raiser did not say.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        length: int | None = None,
        limit: int | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.length = length
        self.limit = limit


__all__ = ["IdentifierTooLongError"]
