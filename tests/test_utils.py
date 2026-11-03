from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from asgiref.sync import async_to_sync, markcoroutinefunction, sync_to_async
from django.http import HttpRequest
from django.test import RequestFactory

from django_spec_agents.utils import (
    aauthorize,
    acall_get_user,
    auth_error_response,
    authorize,
    call_get_user,
    materialize_request_user,
)

factory = RequestFactory()


def test_sync_authorize_open_by_default_without_user() -> None:
    request = factory.get("/agent/tools/")
    assert authorize(request, get_user=None, require_authenticated=False) is None


def test_sync_authorize_rejects_anonymous_when_gated() -> None:
    request = factory.get("/agent/tools/")
    assert authorize(request, get_user=None, require_authenticated=True) == 401


def test_sync_authorize_accepts_middleware_user() -> None:
    request = factory.get("/agent/tools/")
    request.user = SimpleNamespace(is_authenticated=True)
    assert authorize(request, get_user=None, require_authenticated=True) is None


@pytest.mark.parametrize(
    ("is_authenticated", "is_staff", "expected"),
    [
        pytest.param(True, False, 403, id="predicate-forbids-with-403"),
        pytest.param(True, True, None, id="predicate-allows-when-it-passes"),
        # Anonymous + a staff predicate: the auth gate wins, so 401 and not 403.
        pytest.param(False, False, 401, id="authenticated-gate-precedes-predicate"),
    ],
)
def test_sync_authorize_predicate(
    is_authenticated: bool, is_staff: bool, expected: int | None
) -> None:
    request = factory.get("/agent/tools/")
    request.user = SimpleNamespace(is_authenticated=is_authenticated, is_staff=is_staff)
    deny = authorize(
        request,
        get_user=None,
        require_authenticated=True,
        authorize=lambda r: r.user.is_staff,
    )
    assert deny == expected


def test_call_get_user_sync_hook() -> None:
    user = SimpleNamespace(is_authenticated=True)
    assert call_get_user(lambda _request: user, factory.get("/")) is user


def test_call_get_user_async_hook_is_bridged() -> None:
    user = SimpleNamespace(is_authenticated=True)

    async def hook(request):  # noqa: ANN001, ANN202
        return user

    assert call_get_user(hook, factory.get("/")) is user


def test_call_get_user_sync_hook_returning_a_coroutine_is_consumed() -> None:
    user = SimpleNamespace(is_authenticated=True)

    async def _lookup() -> SimpleNamespace:
        return user

    assert call_get_user(lambda _request: _lookup(), factory.get("/")) is user


def test_sync_authorize_assigns_hook_user_onto_request() -> None:
    user = SimpleNamespace(is_authenticated=True)
    request = factory.get("/agent/tools/")
    assert authorize(request, get_user=lambda _r: user, require_authenticated=True) is None
    assert request.user is user


def test_materialize_request_user_handles_missing_user() -> None:
    request = factory.get("/")
    assert materialize_request_user(request) is None


async def test_async_authorize_rejects_anonymous_when_gated() -> None:
    request = factory.get("/agent/")
    request.user = SimpleNamespace(is_authenticated=False)
    assert await aauthorize(request, get_user=None, require_authenticated=True) == 401


async def test_async_authorize_predicate_forbids_with_403() -> None:
    request = factory.get("/agent/")
    request.user = SimpleNamespace(is_authenticated=True, is_staff=False)
    deny = await aauthorize(
        request,
        get_user=None,
        require_authenticated=True,
        authorize=lambda r: r.user.is_staff,
    )
    assert deny == 403


async def test_async_authorize_predicate_allows_when_it_passes() -> None:
    user = SimpleNamespace(is_authenticated=True, is_staff=True)
    request = factory.get("/agent/")
    deny = await aauthorize(
        request,
        get_user=lambda _r: user,
        require_authenticated=True,
        authorize=lambda r: r.user.is_staff,
    )
    assert deny is None


def test_auth_error_response_maps_status_to_message() -> None:
    unauth = auth_error_response(401)
    assert unauth.status_code == 401
    assert json.loads(unauth.content)["error"] == "authentication required"
    forbidden = auth_error_response(403)
    assert forbidden.status_code == 403
    assert json.loads(forbidden.content)["error"] == "forbidden"


async def test_acall_get_user_awaits_a_native_async_hook() -> None:
    """An ``async def`` hook is awaited directly, not bridged off the loop."""
    sentinel = object()

    async def hook(request: HttpRequest) -> object:
        return sentinel

    assert await acall_get_user(hook, RequestFactory().get("/")) is sentinel


async def test_acall_get_user_awaits_a_plain_hook_that_returns_a_coroutine() -> None:
    """An unmarked ``def`` hook handing back a coroutine is awaited, not leaked.

    Nothing marks it, so it takes the ``sync_to_async`` branch and the coroutine
    comes back from the thread; the awaitable fallback is what resolves it to the
    user rather than returning it for ``request.user``.
    """
    user = object()

    async def lookup() -> object:
        return user

    def hook(request: HttpRequest) -> Any:
        return lookup()

    assert await acall_get_user(hook, RequestFactory().get("/")) is user


# Two hooks that asgiref calls coroutine functions and ``inspect`` does not on
# Python 3.11, where ``inspect.iscoroutinefunction`` ignores the marker:
# ``sync_to_async(hook)`` and a plain function that ``markcoroutinefunction``
# marks. Python 3.12+ agrees on both, so there the test passes either way and
# only a 3.11 run can tell the two predicates apart.
def _marked_hook(user: object) -> Callable[[HttpRequest], Any]:
    async def lookup() -> object:
        return user

    def hook(request: HttpRequest) -> Any:
        return lookup()

    return markcoroutinefunction(hook)


def _wrapped_hook(user: object) -> Callable[[HttpRequest], Any]:
    return sync_to_async(lambda request: user)


MARKED_HOOKS = pytest.mark.parametrize("make_hook", [_marked_hook, _wrapped_hook])


@MARKED_HOOKS
async def test_acall_get_user_awaits_a_marked_hook_directly(
    make_hook: Callable[[object], Callable[[HttpRequest], Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hook asgiref marks as a coroutine function is awaited, not wrapped again.

    On 3.11 ``inspect`` would call it sync and hand it to ``sync_to_async``, which
    refuses a marked hook or a ``sync_to_async`` wrapper with ``TypeError:
    sync_to_async can only be applied to sync functions.`` That is the observable
    there. On any Python, the spy delegates to the real ``sync_to_async`` and must
    never be called, so an extra thread hop in front of the hook fails too.
    """
    user = object()
    hook = make_hook(user)
    wrapped: list[object] = []

    def spy(func: Any, **kwargs: Any) -> Any:
        wrapped.append(func)
        return sync_to_async(func, **kwargs)

    monkeypatch.setattr("django_spec_agents.utils.sync_to_async", spy)

    assert await acall_get_user(hook, RequestFactory().get("/")) is user
    assert wrapped == []


@MARKED_HOOKS
def test_call_get_user_bridges_a_marked_hook_itself(
    make_hook: Callable[[object], Callable[[HttpRequest], Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sync twin hands the marked hook itself to ``async_to_sync``.

    Treated as sync it would be called directly and its awaitable bridged after
    the fact, so ``async_to_sync`` would receive the internal ``_consume``
    instead of the hook.
    """
    user = object()
    hook = make_hook(user)
    bridged: list[object] = []

    def spy(awaitable: Any) -> Any:
        bridged.append(awaitable)
        return async_to_sync(awaitable)

    monkeypatch.setattr("django_spec_agents.utils.async_to_sync", spy)

    assert call_get_user(hook, factory.get("/")) is user
    assert bridged == [hook]
