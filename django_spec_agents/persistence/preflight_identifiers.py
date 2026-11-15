from __future__ import annotations


def preflight_identifiers(store: object, **ids: str | None) -> None:
    """Refuse ids too long for ``store`` before a run writes them, writing nothing.

    Calls ``store.check_identifiers(**ids)`` when the store has that method, which
    raises [`IdentifierTooLongError`][django_spec_agents.IdentifierTooLongError] for an
    id longer than its column. A store without it, a custom one included, is
    accepted and nothing happens: the method is on no protocol, so an id it cannot
    hold is still refused when it writes. Each default store of
    ``django_spec_agents.contrib.store`` documents the keywords it takes.
    """
    check = getattr(store, "check_identifiers", None)
    if callable(check):
        check(**ids)


__all__ = ["preflight_identifiers"]
