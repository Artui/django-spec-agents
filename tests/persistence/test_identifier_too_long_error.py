from __future__ import annotations

import django_spec_agents
from django_spec_agents.persistence.identifier_too_long_error import IdentifierTooLongError


def test_the_error_is_a_value_error_exported_from_the_root() -> None:
    assert issubclass(IdentifierTooLongError, ValueError)
    assert django_spec_agents.IdentifierTooLongError is IdentifierTooLongError
    assert "IdentifierTooLongError" in django_spec_agents.__all__
