"""``AGENT_TRANSPORT`` -- what every agent call's context carries under ``transport``."""

from __future__ import annotations

from typing import Final

AGENT_TRANSPORT: Final = "agent"
"""The value of ``call_context["transport"]`` on every call a ``SpecToolset`` serves.

This package's own spelling: the kernel lists no transports, so each transport
package owns and exports its value. It is the plain string, so a spec callable
written for several transports compares ``call_context["transport"] == "agent"``
with nothing imported, and one written for agents alone may import this name
instead. The toolset stamps it last, through the kernel's ``build_call_context``,
over any ``transport`` entry the run's own context carries.
"""
