from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any

# asgiref's ``iscoroutinefunction`` rather than ``inspect``'s: asgiref also honours
# the marker ``markcoroutinefunction`` and ``sync_to_async`` set, which ``inspect``
# reads only from Python 3.12. On 3.11 ``acall_get_user`` would hand a marked hook
# or a ``sync_to_async`` wrapper to ``sync_to_async``, which refuses it with a
# ``TypeError``; ``call_get_user`` would call it directly and bridge the awaitable,
# one ``async_to_sync`` either way. ty takes asgiref's re-export of ``asyncio``'s
# function for this one and flags it as deprecated whatever Python it targets, so
# the two calls below carry an ignore for that and nothing else.
from asgiref.sync import async_to_sync, iscoroutinefunction, sync_to_async
from django.http import HttpRequest, JsonResponse

# The ``get_user`` hook: sync or async, returning the acting user. Sync hooks may
# use the ORM freely — the async callers below run them off the event loop.
GetUser = Callable[[HttpRequest], Any] | Callable[[HttpRequest], Awaitable[Any]]

# The ``authorize`` predicate: run *after* the acting user is established,
# returning ``True`` to allow. Rejecting denies with 403 (authenticated but
# forbidden), as distinct from ``require_authenticated``'s 401 (no user at all).
# The async caller runs it off the event loop, so reading the ORM stays safe.
AuthorizePredicate = Callable[[HttpRequest], bool]


async def acall_get_user(hook: GetUser, request: HttpRequest) -> Any:
    """Run a ``get_user`` hook from async code, ORM-safe either way."""
    if iscoroutinefunction(hook):  # ty: ignore[deprecated]
        return await hook(request)
    result = await sync_to_async(hook, thread_sensitive=True)(request)
    return await result if isawaitable(result) else result


def call_get_user(hook: GetUser, request: HttpRequest) -> Any:
    """Sync twin of ``acall_get_user`` for the sync catalog views."""
    if iscoroutinefunction(hook):  # ty: ignore[deprecated]
        return async_to_sync(hook)(request)
    result = hook(request)
    if isawaitable(result):
        return async_to_sync(_consume)(result)
    return result


async def _consume(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


def materialize_request_user(request: HttpRequest) -> Any:
    """Force the lazy ``request.user`` and return it — call **off** the loop."""
    user = getattr(request, "user", None)
    getattr(user, "is_authenticated", False)
    return user


async def aauthorize(
    request: HttpRequest,
    *,
    get_user: GetUser | None,
    require_authenticated: bool,
    authorize: AuthorizePredicate | None = None,
) -> int | None:
    """The shared authorize policy, async flavour."""
    if get_user is not None:
        request.user = await acall_get_user(get_user, request)
        user: Any = request.user
    else:
        user = await sync_to_async(materialize_request_user, thread_sensitive=True)(request)
    if require_authenticated and not getattr(user, "is_authenticated", False):
        return 401
    if authorize is not None and not await sync_to_async(authorize, thread_sensitive=True)(request):
        return 403
    return None


def authorize(
    request: HttpRequest,
    *,
    get_user: GetUser | None,
    require_authenticated: bool,
    authorize: AuthorizePredicate | None = None,
) -> int | None:
    """Sync flavour of ``aauthorize``, for the catalog views."""
    if get_user is not None:
        request.user = call_get_user(get_user, request)
        user: Any = request.user
    else:
        user = getattr(request, "user", None)
    if require_authenticated and not getattr(user, "is_authenticated", False):
        return 401
    if authorize is not None and not authorize(request):
        return 403
    return None


def auth_error_response(status: int) -> JsonResponse:
    """The JSON deny response for an ``authorize`` / ``aauthorize`` status."""
    message = "authentication required" if status == 401 else "forbidden"
    return JsonResponse({"error": message}, status=status)
