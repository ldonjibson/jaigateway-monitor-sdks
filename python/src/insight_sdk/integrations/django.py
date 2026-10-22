"""Django (+ Django REST Framework).

    MIDDLEWARE = ["insight_sdk.integrations.django.InsightMiddleware", ...]                       # first
    REST_FRAMEWORK = {"EXCEPTION_HANDLER": "insight_sdk.integrations.django.drf_exception_handler"}  # only for DRF
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com")                         # settings.py / wsgi.py

Unhandled view/middleware/template-render errors are reported once with url, method, query (secrets filtered),
headers (no cookies/authorization), ``route`` (the URL pattern, e.g. ``/items/<int:pk>/``) and ``framework`` tags.
Three paths see the same exception object and are deduped: ``process_exception`` of the middleware, Django's
``got_request_exception`` signal (500 path, connected automatically when Django is imported) and, for DRF,
``drf_exception_handler``. 404s, ``PermissionDenied``, ``SuspiciousOperation`` and DRF 4xx are not reported.
The DRF handler cannot be enabled automatically because it is a setting: add the line above.
"""
import asyncio
import logging
import sys
from typing import Any, Callable, Dict, Optional

from ..event import request_info
from ._common import begin_request, end_request, framework_version, report, set_route, set_status

logger = logging.getLogger("insight_sdk")
_UID = "insight_sdk.got_request_exception"


def _request_data(request: Any) -> Dict[str, Any]:
    try:
        url = request.build_absolute_uri()
    except Exception:  # e.g. DisallowedHost
        url = request.path
    url = url.split("?", 1)[0]
    try:
        headers = list(request.headers.items())
    except Exception:
        headers = [(k[5:].replace("_", "-"), v) for k, v in request.META.items() if k.startswith("HTTP_")]
    return request_info(url, request.method, request.META.get("QUERY_STRING"), headers)


def _user_data(request: Any, client: Any) -> Optional[Dict[str, Any]]:
    """Only with send_default_pii: id/email/username of an authenticated request.user."""
    if not getattr(client, "send_default_pii", False):
        return None
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    data = {"id": getattr(user, "pk", None), "email": getattr(user, "email", None), "username": user.get_username()}
    meta = getattr(request, "META", {})
    ip = (meta.get("HTTP_X_FORWARDED_FOR") or meta.get("REMOTE_ADDR") or "").split(",")[0].strip()
    if ip:
        data["ip_address"] = ip
    return {k: str(v) for k, v in data.items() if v not in (None, "")}


def _route(request: Any) -> Optional[str]:
    """URL pattern of the resolved view (``items/<int:pk>/`` -> ``/items/<int:pk>/``)."""
    match = getattr(request, "resolver_match", None)
    route = getattr(match, "route", None)
    if not route:
        return None
    route = str(route)
    if route.startswith("^"):  # regex style patterns (re_path)
        route = route[1:]
    return route if route.startswith("/") else "/" + route


def _event_context(request: Any, client: Any) -> Dict[str, Any]:
    """request data + user (pii only) + framework/route tags for a captured exception."""
    ctx: Dict[str, Any] = {}
    tags = {"framework": "django"}
    version = framework_version("django")
    if version:
        tags["framework_version"] = version
    route = _route(request)
    if route:
        tags["route"] = route
    ctx["tags"] = tags
    ctx["request"] = _request_data(request)
    user = _user_data(request, client)
    if user:
        ctx["user"] = user
    return ctx


def _on_request_exception(sender: Any, request: Any = None, **kwargs: Any) -> None:
    """Django's ``got_request_exception`` signal: the 500 path (sent while the exception is being handled)."""
    from .. import get_client

    client = get_client()
    if client is None:
        return
    try:
        ctx = _event_context(request, client) if request is not None else {}
        report(sys.exc_info()[1], **ctx)
    except Exception as exc:
        logger.debug("insight_sdk: django handler failed: %r", exc)


def setup(client: Any) -> None:
    from django.core.signals import got_request_exception

    got_request_exception.connect(_on_request_exception, weak=False, dispatch_uid=_UID)  # idempotent (dispatch_uid)


def drf_exception_handler(exc: Any, context: Dict[str, Any]) -> Any:
    """Drop-in for DRF's ``EXCEPTION_HANDLER``: wraps DRF's default handler and reports what it would hide.

    ``REST_FRAMEWORK = {"EXCEPTION_HANDLER": "insight_sdk.integrations.django.drf_exception_handler"}``
    """
    from rest_framework.views import exception_handler as default_handler

    return wrap_drf_exception_handler(default_handler)(exc, context)


def wrap_drf_exception_handler(handler: Callable[[Any, Dict[str, Any]], Any]) -> Callable[[Any, Dict[str, Any]], Any]:
    """Wrap your own DRF exception handler: reports unexpected exceptions and 5xx responses, never 4xx."""

    def insight_drf_exception_handler(exc: Any, context: Dict[str, Any]) -> Any:
        response = handler(exc, context)
        try:
            from .. import get_client

            client = get_client()
            status = getattr(response, "status_code", None)
            if client is not None and (response is None or status >= 500):
                request = (context or {}).get("request")
                request = getattr(request, "_request", request)  # DRF Request -> HttpRequest
                ctx = _event_context(request, client) if request is not None else {}
                view = (context or {}).get("view")
                if view is not None:
                    ctx.setdefault("tags", {})["drf_view"] = type(view).__name__
                # response is None: DRF is about to re-raise, Django will answer 500 (same exception, deduped)
                report(exc, **ctx)
        except Exception as e:
            logger.debug("insight_sdk: drf handler failed: %r", e)
        return response

    return insight_drf_exception_handler


class InsightMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Callable[..., Any]) -> None:
        self.get_response = get_response
        self._is_async = asyncio.iscoroutinefunction(get_response)
        if self._is_async:
            try:
                from asgiref.sync import markcoroutinefunction

                markcoroutinefunction(self)
            except ImportError:  # older asgiref/Django
                self._is_coroutine = asyncio.coroutines._is_coroutine  # type: ignore[attr-defined]
        try:  # make sure errors are reported even if init() ran before Django was imported
            setup(None)
        except Exception:
            pass
        try:  # DB spans even when init() ran before Django was imported
            from .. import get_client
            from . import instrument

            c = get_client()
            if c is not None and getattr(c, "performance", None) is not None and getattr(c, "performance_spans", False):
                instrument.ensure_django_orm()
        except Exception:
            pass
        self._version = framework_version("django")

    def __call__(self, request: Any) -> Any:
        if self._is_async:
            return self.__acall__(request)
        cm = self._begin(request)
        try:
            response = self.get_response(request)
            self._finish(request, response)
            return response
        finally:
            end_request(cm)

    async def __acall__(self, request: Any) -> Any:
        cm = self._begin(request)
        try:
            response = await self.get_response(request)
            self._finish(request, response)
            return response
        finally:
            end_request(cm)

    def _begin(self, request: Any) -> Any:
        try:
            data = _request_data(request)
        except Exception:
            data = None
        try:
            method, path = request.method, request.path
        except Exception:
            method = path = None
        return begin_request("django", data, self._version, method=method, path=path)

    def _finish(self, request: Any, response: Any) -> None:
        """Response status + the resolved URL pattern (set after resolving, also when ``process_view`` never ran).
        The transaction covers the response being built, not streaming it."""
        try:
            set_status(getattr(response, "status_code", None))
            set_route(_route(request))
        except Exception:
            pass

    def process_view(self, request: Any, view_func: Any, view_args: Any, view_kwargs: Any) -> None:
        try:
            set_route(_route(request))
        except Exception:
            pass
        return None

    def process_exception(self, request: Any, exception: BaseException) -> None:
        """View, template-render and DRF-unhandled errors: report here (with the request scope), then let
        Django build its normal 500 response (returning None)."""
        from .. import get_client

        try:
            client = get_client()
            if client is not None:
                report(exception, **_event_context(request, client))
        except Exception as exc:
            logger.debug("insight_sdk: django process_exception failed: %r", exc)
        return None
