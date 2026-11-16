from __future__ import annotations


class RunIdReusedError(ValueError):
    """Raised by a run store's ``claim`` for a run id its owner has claimed before.

    Run ids are single-use per owner: a resumed or retried run claims under a
    fresh id. Nothing was written; ``run_id`` names the refused id. A transport
    maps it to a client error rather than a server one.
    """

    def __init__(self, message: str, *, run_id: str) -> None:
        super().__init__(message)
        self.run_id = run_id


__all__ = ["RunIdReusedError"]
