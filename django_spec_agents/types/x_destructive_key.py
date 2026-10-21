"""``x-destructive``: this tool mutates.

One of four JSON-Schema extensions stamped at the schema root, which is what
carries them to the *client*: AG-UI has no native concept for any of them and
passes unknown keys through verbatim. Read client-side to gate execution behind
a confirmation, and server-side by ``ToolGuard`` for a tool whose schema is the
only place its author said so.
"""

from __future__ import annotations

X_DESTRUCTIVE_KEY = "x-destructive"

__all__ = ["X_DESTRUCTIVE_KEY"]
