from __future__ import annotations

from django_spec_agents.child_sessions.types.child_session_index import ChildSessionIndex
from tests.child_sessions.test_child_sessions_toolset import _Index


def test_the_test_double_satisfies_the_protocol() -> None:
    assert isinstance(_Index(), ChildSessionIndex)
    assert not isinstance(object(), ChildSessionIndex)
