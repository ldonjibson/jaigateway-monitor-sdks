"""Bottle.

    import insight_sdk
    from insight_sdk.integrations.bottle import InsightPlugin
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); app.install(InsightPlugin())

Wraps every route callback: per-request scope, request data without secrets, ``framework`` tags, ``route`` tag with the
rule template (``/items/<id>``), ``http`` breadcrumb. Exceptions and ``HTTPError`` >= 500 are reported once and
re-raised, so Bottle's normal error page is unchanged; ``abort(404)``/redirects and other < 500 responses are ignored.
"""
import functools
import logging
from typing import Any, Callable, Dict

from ..event import request_info
from ._common import begin_request, end_request, framework_version, report

logger = logging.getLogger("insight_sdk")


def _request_data() -> Dict[str, Any]:
    from bottle import request

    return request_info(request.url, request.method, request.query_string, request.headers.items())


def _route_rule(environ: Any) -> Any:
    return getattr(environ.get("bottle.route"), "rule", None)


class InsightPlugin:
    name = "insight"
    api = 2

    def setup(self, app: Any) -> None:
        """Time every request at the WSGI level: Bottle never calls route callbacks for unmatched URLs (404), and the
        response status is only known there. The transaction is named after the matched rule (``GET /items/<id>``)."""
        if getattr(app, "_insight_sdk_wsgi", False):
            return
        from .wsgi import InsightWSGIMiddleware

        try:
            app.wsgi = InsightWSGIMiddleware(app.wsgi, route=_route_rule)
            app._insight_sdk_wsgi = True
        except Exception as exc:  # never break the app
            logger.debug("insight_sdk: bottle timing unavailable: %r", exc)

    def apply(self, callback: Callable[..., Any], route: Any) -> Callable[..., Any]:
        version = framework_version("bottle")
        template = getattr(route, "rule", None)

        @functools.wraps(callback)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                cm = begin_request("bottle", _request_data(), version, route=template, shared=False)
            except Exception:
                return callback(*args, **kwargs)
            try:
                return callback(*args, **kwargs)
            except Exception as exc:  # includes HTTPResponse/HTTPError: filtered by status in report()
                report(exc, scope=cm.scope)
                raise
            finally:
                end_request(cm)

        return wrapper
