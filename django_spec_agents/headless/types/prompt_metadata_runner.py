from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PromptMetadataRunner(Protocol):
    """A [`HeadlessRunner`][django_spec_agents.HeadlessRunner]'s opt-in to ``prompt_metadata``.

    A runner sets ``accepts_prompt_metadata = True`` once its ``start`` and ``run_to_first_stop``
    take ``prompt_metadata``. Only then does
    [`wake_thread`][django_spec_agents.child_sessions.wake_thread.wake_thread] pass the wake
    line's mark; a runner without the attribute, or with it ``False``, is woken without the
    keyword. A runner that hands ``wake_thread`` a wrapper of itself sets it on the wrapper.
    """

    accepts_prompt_metadata: bool


__all__ = ["PromptMetadataRunner"]
