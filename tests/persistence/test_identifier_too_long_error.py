from __future__ import annotations

import django_spec_agents
from django_spec_agents.persistence.identifier_too_long_error import IdentifierTooLongError


def test_the_error_is_a_value_error_exported_from_the_root() -> None:
    assert issubclass(IdentifierTooLongError, ValueError)
    assert django_spec_agents.IdentifierTooLongError is IdentifierTooLongError
    assert "IdentifierTooLongError" in django_spec_agents.__all__


def test_the_error_carries_what_was_refused_when_the_raiser_says() -> None:
    error = IdentifierTooLongError("run_id is 3", field="run_id", length=3, limit=2)

    assert (str(error), error.field, error.length, error.limit) == ("run_id is 3", "run_id", 3, 2)
    assert IdentifierTooLongError("plain").field is None
