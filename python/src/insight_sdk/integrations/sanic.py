"""Sanic.

    import insight_sdk
    from insight_sdk.integrations.sanic import init_app
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); init_app(app)

Request middleware gives every request its own scope (request data without secrets, ``framework`` tags,
``route`` tag with the route template such as ``/items/<id:int>``, ``http`` breadcrumb); an ``@app.exception(Exception)``
handler reports unhandled exceptions and 5xx ``SanicException`` once and then returns Sanic's *default* error
response, so the response is exactly what Sanic would have sent. 4xx (``NotFound``, ``InvalidUsage``, ...) is not
reported. Your own more specific ``@app.exception(SomeError)`` handlers keep precedence (and are then not reported).
"""
import logging
from typing import Any, Dict, Optional

from ..event import request_info
from ._common import begin_request, end_request, framework_version, once_flag, report, set_route, set_status

logger = logging.getLogger("insight_sdk")
_ATTR = "_insight_cm"


def _request_data(request: Any) -> Dict[str, Any]:
    return request_info(
        "%s://%s%s" % (request.scheme, request.host, request.path), request.method, request.query_string, request.headers.items()
    )


def _route(request: Any) -> Optional[str]:
    route = getattr(request, "route", None)
    path = getattr(route, "uri", None) or getattr(route, "path", None)
    return path if isinstance(path, str) and path else None


def _end(request: Any, response: Any = None) -> None:
    cm = getattr(request.ctx, _ATTR, None)
    if cm is not None:
        scope = getattr(cm, "scope", None)
        set_status(getattr(response, "status", None), scope)
        set_route(_route(request), scope)  # transaction name: the route template (404 -> "<unmatched>")
        setattr(request.ctx, _ATTR, None)
        end_request(cm)


def init_app(app: Any) -> Any:
    """Install on a ``Sanic`` app before it starts. Returns ``app``."""
    if not once_flag(app, "_insight_sdk_installed"):
        return app
    version = framework_version("sanic")

    @app.on_request
    async def insight_begin(request: Any) -> None:
        try:
            setattr(request.ctx, _ATTR, begin_request("sanic", _request_data(request), version, route=_route(request)))
        except Exception as exc:
            logger.debug("insight_sdk: sanic begin failed: %r", exc)

    @app.on_response
    async def insight_end(request: Any, response: Any) -> None:
        try:
            _end(request, response)
        except Exception:
            pass

    @app.exception(Exception)
    async def insight_exception(request: Any, exception: BaseException) -> Any:
        try:
            cm = getattr(request.ctx, _ATTR, None)
            if cm is None:  # failed before request middleware ran (e.g. no route matched): still time it
                cm = begin_request("sanic", _request_data(request), version, route=_route(request))
                setattr(request.ctx, _ATTR, cm)
            report(exception, scope=getattr(cm, "scope", None))
        except Exception as exc:
            logger.debug("insight_sdk: sanic exception handler failed: %r", exc)
        response = app.error_handler.default(request, exception)  # the response Sanic would have sent
        try:
            _end(request, response)
        except Exception as exc:
            logger.debug("insight_sdk: sanic end failed: %r", exc)
        return response

    return app
