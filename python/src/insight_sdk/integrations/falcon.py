"""Falcon (``falcon.App`` and ``falcon.asgi.App``).

    import insight_sdk
    from insight_sdk.integrations.falcon import init_app
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); init_app(app)

``init_app`` adds ``InsightMiddleware`` (per-request scope, request data without secrets, ``framework`` tags, ``route``
tag with the URI template such as ``/items/{id}``, ``http`` breadcrumb) and wraps the ``Exception`` and ``HTTPError``
error handlers: unexpected exceptions and ``HTTPError`` >= 500 are reported once, then Falcon's own handler runs, so
the response is unchanged. 4xx ``HTTPError`` and ``HTTPStatus`` are not reported. You may also add
``InsightMiddleware()`` yourself (``falcon.App(middleware=[InsightMiddleware()])``) and call ``register_error_handlers(app)``.
"""
import inspect
import logging
from typing import Any, Dict, Optional

from ..event import request_info
from ._common import _as_status, begin_request, end_request, framework_version, once_flag, report, set_route, set_status

logger = logging.getLogger("insight_sdk")
_ATTR = "_insight_cm"


def _request_data(req: Any) -> Dict[str, Any]:
    return request_info(req.prefix + req.path, req.method, req.query_string, req.headers.items())


def _begin(req: Any) -> None:
    try:
        setattr(req.context, _ATTR, begin_request("falcon", _request_data(req), framework_version("falcon"), shared=True))
    except Exception as exc:
        logger.debug("insight_sdk: falcon begin failed: %r", exc)


def _set_route(req: Any, cm: Any = None) -> None:
    try:
        cm = cm or getattr(req.context, _ATTR, None)
        set_route(getattr(req, "uri_template", None), getattr(cm, "scope", None))
    except Exception:
        pass


def _end(req: Any, resp: Any = None) -> None:
    try:
        cm = getattr(req.context, _ATTR, None)
        setattr(req.context, _ATTR, None)
        if cm is not None:
            scope = getattr(cm, "scope", None)
            _set_route(req, cm)  # transaction name: URI template (unrouted 404 -> "<unmatched>")
            status = getattr(resp, "status_code", None)  # Falcon 4
            set_status(status if status is not None else _as_status(getattr(resp, "status", None)), scope)
        end_request(cm)
    except Exception:
        pass


def _report(req: Any, ex: BaseException) -> None:
    try:
        cm = getattr(req.context, _ATTR, None)
        scope = getattr(cm, "scope", None)
        _set_route(req)
        if scope is not None:
            report(ex, scope=scope)
        else:
            report(ex, request=_request_data(req), tags={"framework": "falcon"})
    except Exception as exc:
        logger.debug("insight_sdk: falcon report failed: %r", exc)


class InsightMiddleware:
    """Works for both the WSGI and the ASGI app (Falcon picks the ``*_async`` methods for ASGI)."""

    def process_request(self, req: Any, resp: Any) -> None:
        _begin(req)

    def process_resource(self, req: Any, resp: Any, resource: Any, params: Any) -> None:
        _set_route(req)

    def process_response(self, req: Any, resp: Any, resource: Any, req_succeeded: bool) -> None:
        _end(req, resp)

    async def process_request_async(self, req: Any, resp: Any) -> None:
        _begin(req)

    async def process_resource_async(self, req: Any, resp: Any, resource: Any, params: Any) -> None:
        _set_route(req)

    async def process_response_async(self, req: Any, resp: Any, resource: Any, req_succeeded: bool) -> None:
        _end(req, resp)


def register_error_handlers(app: Any) -> None:
    """Wrap the ``Exception`` and ``HTTPError`` handlers (report, then delegate to the existing ones)."""
    import falcon

    is_async = hasattr(app, "_error_handlers") and inspect.iscoroutinefunction(
        app._error_handlers.get(Exception) or app._error_handlers.get(falcon.HTTPError)
    )
    prev_exc = getattr(app, "_error_handlers", {}).get(Exception)
    prev_http = getattr(app, "_error_handlers", {}).get(falcon.HTTPError)

    def fallback_exc() -> None:
        raise falcon.HTTPInternalServerError()

    if is_async:

        async def handle_exception(req: Any, resp: Any, ex: Any, params: Any) -> None:
            _report(req, ex)
            if prev_exc is None:
                fallback_exc()
            await prev_exc(req, resp, ex, params)

        async def handle_http_error(req: Any, resp: Any, ex: Any, params: Any) -> None:
            _report(req, ex)
            if prev_http is None:
                raise ex
            await prev_http(req, resp, ex, params)

    else:

        def handle_exception(req: Any, resp: Any, ex: Any, params: Any) -> None:  # type: ignore[misc]
            _report(req, ex)
            if prev_exc is None:
                fallback_exc()
            prev_exc(req, resp, ex, params)

        def handle_http_error(req: Any, resp: Any, ex: Any, params: Any) -> None:  # type: ignore[misc]
            _report(req, ex)
            if prev_http is None:
                raise ex
            prev_http(req, resp, ex, params)

    app.add_error_handler(Exception, handle_exception)
    app.add_error_handler(falcon.HTTPError, handle_http_error)


def init_app(app: Any) -> Any:
    """Add the middleware and error-handler wrappers. Returns ``app``."""
    if not once_flag(app, "_insight_sdk_installed"):
        return app
    app.add_middleware(InsightMiddleware())
    register_error_handlers(app)
    return app
