"""aiohttp.

    import insight_sdk
    from insight_sdk.integrations.aiohttp import setup
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); setup(app)

or ``web.Application(middlewares=[insight_middleware()])``. Each request gets its own scope (request data without
secrets, ``framework`` tags, ``route`` tag with the resource template such as ``/items/{id}``, ``http`` breadcrumb).
Unhandled exceptions and ``web.HTTPException`` with status >= 500 are reported once and re-raised, so aiohttp still
builds its normal 500 response; ``HTTPException`` below 500 (404, redirects, ...) is ignored. Sub-applications need
their own ``setup(subapp)``.
"""
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from ..event import request_info
from ._common import begin_request, end_request, framework_version, once_flag, report, set_route, set_status

logger = logging.getLogger("insight_sdk")


def _request_data(request: Any) -> Dict[str, Any]:
    return request_info(
        "%s://%s%s" % (request.scheme, request.host, request.path), request.method, request.query_string, request.headers.items()
    )


def _route(request: Any) -> Optional[str]:
    resource = getattr(getattr(request, "match_info", None), "route", None)
    resource = getattr(resource, "resource", None)
    canonical = getattr(resource, "canonical", None)
    return canonical if isinstance(canonical, str) and canonical else None


def insight_middleware() -> Callable[..., Awaitable[Any]]:
    """An ``@web.middleware`` that reports exceptions raised by handlers (re-raising them)."""
    from aiohttp import web

    version = framework_version("aiohttp")

    @web.middleware
    async def insight_aiohttp_middleware(request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        try:
            cm = begin_request("aiohttp", _request_data(request), version, route=_route(request))
        except Exception:  # never break the request because of the SDK
            return await handler(request)
        try:
            response = await handler(request)
            set_status(getattr(response, "status", None), cm.scope)
            return response
        except Exception as exc:
            set_route(_route(request), cm.scope)
            report(exc, scope=cm.scope)  # an HTTPException also carries its status (404 is not an error)
            raise
        finally:
            set_route(_route(request), cm.scope)  # transaction name: resource template (404 -> "<unmatched>")
            end_request(cm)

    return insight_aiohttp_middleware


def setup(app: Any) -> Any:
    """Insert the middleware first in ``app.middlewares``. Returns ``app``."""
    if once_flag(app, "_insight_sdk_installed"):
        app.middlewares.insert(0, insight_middleware())
    return app
