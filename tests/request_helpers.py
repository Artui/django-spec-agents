"""Requests carrying a stand-in user, for stores that read only ``is_authenticated`` and ``pk``."""

from __future__ import annotations

from types import SimpleNamespace

from django.http import HttpRequest
from django.test import RequestFactory


def authed_request(pk: str = "7") -> HttpRequest:
    """A request whose user resolves to ``owner_id == pk`` (no DB / session)."""
    request = RequestFactory().post("/")
    request.user = SimpleNamespace(is_authenticated=True, pk=pk)  # type: ignore[attr-defined]
    return request


def anonymous_request() -> HttpRequest:
    request = RequestFactory().post("/")
    request.user = SimpleNamespace(is_authenticated=False, pk=None)  # type: ignore[attr-defined]
    return request
