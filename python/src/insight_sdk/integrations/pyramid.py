"""Pyramid.

    import insight_sdk
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com")
    config.include("insight_sdk.integrations.pyramid")

Adds a tween just below Pyramid's exception-view tween, so it sees the raw exception before an exception view turns it
into a response. Each request gets its own scope (request data without secrets, ``framework`` tags, ``route`` tag with
the route pattern such as ``/items/{id}``, ``http`` breadcrumb). Unhandled exceptions and ``HTTPServerError``
(>= 500) are reported once and re-raised, so Pyramid's exception views and error handling still run; ``HTTPNotFound``,
``HTTPForbidden`` and other < 500 ``HTTPException`` are not reported.
"""
import logging
from typing import Any, Callable, Dict, Optional

from ..event import request_info
from ._common import begin_request, end_request, framework_version, report, set_route, set_status

logger = logging.getLogger("insight_sdk")
TWEEN = "insight_sdk.integrations.pyramid.insight_tween_factory"


def _request_data(request: Any) -> Dict[str, Any]:
    return request_info(request.path_url, request.method, request.query_string, request.headers.items())


def _route(request: Any) -> Optional[str]:
    return getattr(getattr(request, "matched_route", None), "pattern", None)


def insight_tween_factory(handler: Callable[[Any], Any], registry: Any) -> Callable[[Any], Any]:
    version = framework_version("pyramid")

    def insight_tween(request: Any) -> Any:
        try:
            cm = begin_request("pyramid", _request_data(request), version, shared=False)
        except Exception:
            return handler(request)
        try:
            response = handler(request)
            set_status(getattr(response, "status_code", None), cm.scope)
            return response
        except Exception as exc:
            set_route(_route(request), cm.scope)
            report(exc, scope=cm.scope)  # HTTPException (404...) carries its status; unexpected ones mark the failure
            raise
        finally:
            set_route(_route(request), cm.scope)  # transaction name: the matched route pattern
            end_request(cm)

    return insight_tween


def includeme(config: Any) -> None:
    from pyramid.tweens import EXCVIEW, MAIN

    config.add_tween(TWEEN, over=MAIN, under=EXCVIEW)
