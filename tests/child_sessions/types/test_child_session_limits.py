from __future__ import annotations

import pytest

from django_spec_agents.child_sessions.types.child_session_limits import ChildSessionLimits


def test_the_defaults_are_the_owners() -> None:
    limits = ChildSessionLimits(timeout=60.0)

    assert (limits.max_depth, limits.max_running_per_thread, limits.max_running_per_user) == (
        2,
        3,
        10,
    )


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan")])
def test_a_child_needs_a_positive_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="positive timeout"):
        ChildSessionLimits(timeout=timeout)


@pytest.mark.parametrize("name", ["max_depth", "max_running_per_thread", "max_running_per_user"])
def test_each_limit_is_at_least_one(name: str) -> None:
    with pytest.raises(ValueError, match=name):
        ChildSessionLimits(timeout=1.0, **{name: 0})
