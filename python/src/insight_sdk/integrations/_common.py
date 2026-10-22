"""Helpers shared by every web/job integration (internal; not a public API).

Everything here is defensive: an integration must never raise into the host framework.
"""
import logging
import sys
from typing import Any, Callable, Dict, Optional

from ..performance import begin_job_transaction, begin_web_transaction
from ..scope import Scope, get_current_scope, push_scope

logger = logging.getLogger("insight_sdk")

# Exceptions that are control flow, not failures (never reported).
_NEVER_REPORT_NAMES = {
    "Http404", "Resolver404", "PermissionDenied", "SuspiciousOperation", "DisallowedHost", "BadRequest",
    "Finish", "StopAsyncIteration", "HTTPContinueRouting", "Redirect", "NotFound", "Ignore", "Reject", "Retry",
}


def framework_version(module_name: str) -> Optional[str]:
    """Installed version of a framework (distribution metadata first, then ``__version__``), or None."""
    top = module_name.split(".")[0]
    try:
        from importlib import metadata

        return str(metadata.version(top))
    except Exception:
        pass
    try:
        module = sys.modules.get(module_name)
        version: Any = getattr(module, "__version__", None) or getattr(module, "VERSION", None)
        if isinstance(version, tuple):
            version = ".".join(str(v) for v in version)
        return str(version) if version is not None and not callable(version) else None
    except Exception:
        return None


def _as_status(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 100 <= value <= 599 else None
    if isinstance(value, str):
        head = value.strip().split(" ", 1)[0]
        if head.isdigit() and 100 <= int(head) <= 599:
            return int(head)
    return None


def exception_status(exc: BaseException) -> Optional[int]:
    """HTTP status carried by a framework's HTTP exception (``status_code``, ``status`` or ``code``)."""
    for attr in ("status_code", "status"):
        try:
            status = _as_status(getattr(exc, attr, None))
        except Exception:
            status = None
        if status is not None:
            return status
    if any("HTTP" in klass.__name__.upper() for klass in type(exc).__mro__):  # werkzeug/pyramid style ``code``
        try:
            return _as_status(getattr(exc, "code", None))
        except Exception:
            return None
    return None


def should_report(exc: Any) -> bool:
    """False for expected exceptions: 1xx-4xx HTTP exceptions, 404s, redirects, control flow."""
    if not isinstance(exc, Exception):
        return False
    status = exception_status(exc)
    if status is not None:
        return status >= 500
    return not any(c.__name__ in _NEVER_REPORT_NAMES for c in type(exc).__mro__)


def report(exc: Any, scope: Optional[Scope] = None, force: bool = False, **ctx: Any) -> Optional[str]:
    """Capture ``exc`` unless it is expected. Never raises. The client dedupes repeated sightings
    of the same exception object, so every layer may call this."""
    try:
        from .. import get_client

        client = get_client()
        if client is None or exc is None:
            return None
        note_exception(exc, scope)
        if not force and not should_report(exc):
            return None
        if scope is not None:
            return client.capture_exception(exc, scope=scope, **ctx)
        return client.capture_exception(exc, **ctx)
    except Exception as e:
        logger.debug("insight_sdk: report failed: %r", e)
        return None


def note_exception(exc: Any, scope: Optional[Scope] = None) -> None:
    """Tell the request's performance transaction about an exception: an HTTP exception carries its status (4xx is not an
    error); any other unexpected exception marks the transaction as failed. Never raises."""
    try:
        txn = (scope if scope is not None else get_current_scope()).txn
        if txn is None or txn.done:
            return
        status = exception_status(exc) if isinstance(exc, Exception) else None
        if status is not None:
            if txn.status is None:
                txn.set_status(status)
        elif should_report(exc):
            txn.failed = True
    except Exception:
        pass


def set_status(status: Any, scope: Optional[Scope] = None) -> None:
    """Record the response status on the current (or given) request's performance transaction. Never raises."""
    try:
        txn = (scope if scope is not None else get_current_scope()).txn
        if txn is not None and not txn.done:
            txn.set_status(_as_status(status))
    except Exception:
        pass


def safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for SDK hooks: swallow and debug-log anything the hook itself raises."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.debug("insight_sdk: %s failed: %r", getattr(fn, "__name__", "hook"), e)
            return None

    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = fn.__doc__
    return wrapper


def normalize_route(route: Optional[str]) -> Optional[str]:
    if not route:
        return None
    route = str(route)
    return route if route.startswith("/") else "/" + route


def begin_request(
    framework: str,
    request: Optional[Dict[str, Any]] = None,
    version: Optional[str] = None,
    route: Optional[str] = None,
    shared: bool = True,
    crumb: bool = True,
    perf: bool = True,
    method: Optional[str] = None,
    path: Optional[str] = None,
) -> Any:
    """Open a per-request scope (framework tags, request data, ``http`` breadcrumb). Returns the context
    manager; pass it to ``end_request`` when the request ends. Never raises.

    Also starts the request's performance transaction (``cm.txn``) unless an outer layer already did or ``perf=False``;
    ``end_request`` finishes it. Integrations feed it the route (``set_route``) and the response status (``set_status``)."""
    cm = push_scope(shared=shared)
    cm.txn = None  # type: ignore[attr-defined]
    try:
        scope = cm.__enter__()
        cm.scope = scope  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        return cm
    if perf:
        try:  # W3C trace context: continue the caller's trace (request headers are lower-cased by request_info)
            traceparent = ((request or {}).get("headers") or {}).get("traceparent")
        except Exception:
            traceparent = None
        cm.txn = begin_web_transaction(  # type: ignore[attr-defined]
            scope, method or (request or {}).get("method"), path or (request or {}).get("url"), route, traceparent
        )
    try:
        scope.set_tag("framework", framework)
        if version:
            scope.set_tag("framework_version", version)
        if request:
            scope.set_request(request)
        if route:
            scope.set_tag("route", normalize_route(route) or "")
        if crumb and request:
            add_http_breadcrumb(scope, request)
    except Exception as e:
        logger.debug("insight_sdk: begin_request failed: %r", e)
    return cm


def begin_job(
    framework: str,
    tags: Optional[Dict[str, Any]] = None,
    message: Optional[str] = None,
    version: Optional[str] = None,
    task: Optional[str] = None,
    traceparent: Any = None,
) -> Any:
    """Per-job scope for background workers: framework tags + job tags + a breadcrumb. Returns the context
    manager (``.scope`` is the scope); pass it to ``end_request``. Never raises. ``task`` (the task/actor name) starts the
    ``TASK <name>`` performance transaction (continuing the trace of ``traceparent`` when given and valid), finished by ``end_request``; the final-failure ``report()`` marks it failed."""
    cm = push_scope(shared=True)
    cm.txn = None  # type: ignore[attr-defined]
    try:
        scope = cm.__enter__()
        cm.scope = scope  # type: ignore[attr-defined]
        if task:
            cm.txn = begin_job_transaction(scope, task, traceparent)  # type: ignore[attr-defined]
        scope.set_tag("framework", framework)
        if version:
            scope.set_tag("framework_version", version)
        for key, value in (tags or {}).items():
            if value is not None and value != "":
                scope.set_tag(key, value)
        if message:
            from .. import get_client
            from ..event import utcnow_iso

            client = get_client()
            if client is not None:
                client.add_breadcrumb(
                    {"timestamp": utcnow_iso(), "category": "custom", "level": "info", "message": message[:1000]}, scope
                )
    except Exception as e:
        logger.debug("insight_sdk: begin_job failed: %r", e)
    return cm


def end_request(cm: Any) -> None:
    try:
        txn = getattr(cm, "txn", None)
        if txn is not None:
            txn.finish()
    except Exception:
        pass
    try:
        if cm is not None:
            cm.__exit__(None, None, None)
    except Exception:
        pass


def add_http_breadcrumb(scope: Scope, request: Dict[str, Any], status: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Add the ``http`` breadcrumb for a request; returns it so the status code can be filled in later."""
    from .. import get_client
    from ..event import utcnow_iso

    client = get_client()
    if client is None:
        return None
    data: Dict[str, Any] = {"url": request.get("url"), "method": request.get("method")}
    if status is not None:
        data["status_code"] = status
    rest = (request.get("url") or "").split("://", 1)[-1]
    path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    crumb: Dict[str, Any] = {
        "timestamp": utcnow_iso(),
        "category": "http",
        "level": "info",
        "message": "%s %s" % (request.get("method"), path),
        "data": data,
    }
    client.add_breadcrumb(crumb, scope)
    return crumb


def set_route(route: Optional[str], scope: Optional[Scope] = None) -> None:
    """Tag the current (or given) scope with the route template; no-op when unknown."""
    route = normalize_route(route)
    if not route:
        return
    try:
        if scope is None:
            from ..scope import _writable_scope

            scope = _writable_scope()
        scope.set_tag("route", route)
        if scope.txn is not None:
            scope.txn.set_route(route)
    except Exception:
        pass


def once_flag(obj: Any, attr: str) -> bool:
    """True the first time it is called for ``obj`` (marks it), False afterwards."""
    if getattr(obj, attr, False):
        return False
    try:
        setattr(obj, attr, True)
    except Exception:
        return True
    return True
