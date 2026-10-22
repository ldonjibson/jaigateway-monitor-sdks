"""Flask.

    import insight_sdk
    from insight_sdk.integrations.flask import init_app
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); init_app(app)

Unhandled errors are captured via Flask's ``got_request_exception`` signal (auto-connected when Flask is imported);
``init_app(app)`` adds a scope per request, request data (url without secrets, method, filtered query, scrubbed
headers), ``framework``/``framework_version``/``route`` tags (route = rule template, e.g. ``/items/<int:id>``), an
``http`` breadcrumb, and also reports exceptions that your own ``@app.errorhandler(Exception)``/``(500)`` swallows.
``HTTPException`` below 500 (404, 400, ...) is never reported. Flask's own error handling is unchanged.
"""
import logging
from typing import Any, Dict, Optional

from ..event import request_info
from ._common import begin_request, end_request, framework_version, once_flag, report, set_route, set_status

logger = logging.getLogger("insight_sdk")


def _request_data() -> Dict[str, Any]:
    from flask import request

    return request_info(
        request.url.split("?", 1)[0], request.method, request.query_string.decode("utf-8", "replace"), request.headers.items()
    )


def _route() -> Optional[str]:
    from flask import request

    rule = getattr(request, "url_rule", None)
    return getattr(rule, "rule", None)


def _tags() -> Dict[str, str]:
    tags = {"framework": "flask"}
    version = framework_version("flask")
    if version:
        tags["framework_version"] = version
    route = _route()
    if route:
        tags["route"] = route
    return tags


def _on_exception(sender: Any, exception: Any = None, **kwargs: Any) -> None:
    from .. import get_client

    if get_client() is None or exception is None:
        return
    try:
        report(exception, request=_request_data(), tags=_tags())
    except Exception as exc:
        logger.debug("insight_sdk: flask handler failed: %r", exc)


def setup(client: Any) -> None:
    from flask import got_request_exception

    got_request_exception.connect(_on_exception, weak=False)


def init_app(app: Any) -> None:
    """Per-request scope + request data. Call once after creating the Flask app."""
    if not once_flag(app, "_insight_sdk_installed"):
        return
    from flask import g

    setup(None)
    version = framework_version("flask")

    @app.before_request
    def _insight_begin() -> None:
        try:
            cm = begin_request("flask", _request_data(), version, route=_route(), shared=False)
        except Exception:
            return
        g._insight_scope = cm

    @app.after_request
    def _insight_status(response: Any) -> Any:
        set_status(getattr(response, "status_code", None))
        return response

    @app.teardown_request
    def _insight_end(exc: Any) -> None:
        cm = g.pop("_insight_scope", None)
        try:  # the transaction is named after the rule template (none matched -> "<unmatched>")
            if cm is not None:
                set_route(_route(), cm.scope)
        except Exception:
            pass
        end_request(cm)

    original = app.handle_user_exception

    def handle_user_exception(e: Any) -> Any:
        result = original(e)  # re-raises when nobody handles it: Flask's 500 path + the signal report it
        try:  # a user error handler swallowed it: the signal never fires, so report here
            set_route(_route())
            report(e, request=_request_data(), tags=_tags())
        except Exception as exc:
            logger.debug("insight_sdk: flask handle_user_exception failed: %r", exc)
        return result

    app.handle_user_exception = handle_user_exception  # type: ignore[method-assign]
