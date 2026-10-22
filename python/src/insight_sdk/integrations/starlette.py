"""Starlette (and the shared core of the FastAPI integration).

    import insight_sdk
    from insight_sdk.integrations.starlette import init_app
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); init_app(app)

Adds ``InsightASGIMiddleware`` (per-request scope, request data, ``http`` breadcrumb, ``route`` tag with the
route template such as ``/items/{id}``) and wraps the ``HTTPException`` handler so ``HTTPException`` with
status >= 500 is reported while 4xx is not. Starlette's own 500 handling is untouched: unhandled exceptions are
re-raised after being captured once.
"""
import inspect
import logging
from typing import Any, Callable, Optional

from ._common import framework_version, once_flag, report, set_route
from .asgi import SCOPE_KEY, InsightASGIMiddleware, asgi_route

logger = logging.getLogger("insight_sdk")


def _default_http_exception_handler(request: Any, exc: Any) -> Any:
    from starlette.responses import PlainTextResponse, Response

    headers = getattr(exc, "headers", None)
    if exc.status_code in {204, 304}:
        return Response(status_code=exc.status_code, headers=headers)
    return PlainTextResponse(exc.detail, status_code=exc.status_code, headers=headers)


def make_http_exception_handler(previous: Optional[Callable[..., Any]]) -> Callable[..., Any]:
    """Handler for HTTPException: report status >= 500, then delegate to the previous/default handler."""

    async def insight_http_exception_handler(request: Any, exc: Any) -> Any:
        try:
            if getattr(exc, "status_code", 0) >= 500:
                scope = request.scope
                s = scope.get(SCOPE_KEY)
                set_route(asgi_route(scope), s)
                report(exc, scope=s)
        except Exception as e:  # never break the framework's own response
            logger.debug("insight_sdk: http exception handler failed: %r", e)
        handler = previous or _default_http_exception_handler
        result = handler(request, exc)
        if inspect.isawaitable(result):
            result = await result
        return result

    return insight_http_exception_handler


def init_app(app: Any, framework: str = "starlette", module: str = "starlette") -> Any:
    """Install the middleware and the HTTPException handler on a Starlette (or FastAPI) app. Returns ``app``."""
    if not once_flag(app, "_insight_sdk_installed"):
        return app
    from starlette.exceptions import HTTPException

    version = framework_version(module)
    try:
        app.add_middleware(InsightASGIMiddleware, framework=framework, version=version)
    except RuntimeError as exc:  # the app already started serving: middleware can no longer be added
        logger.warning("insight_sdk: could not add middleware to %s (call init_app before the app starts): %s", framework, exc)
        return app
    previous = getattr(app, "exception_handlers", {}).get(HTTPException)
    app.add_exception_handler(HTTPException, make_http_exception_handler(previous))
    return app
