"""Quart (Flask-like, async).

    import insight_sdk
    from insight_sdk.integrations.quart import init_app
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); init_app(app)

Same behaviour as the Flask integration: per-request scope, request data (no secrets), ``route`` tag with the rule
template (``/items/<int:id>``), ``framework`` tags, ``http`` breadcrumb; unhandled exceptions (and ones swallowed by
your own error handlers) are reported once; ``HTTPException`` below 500 is not. Quart's responses are unchanged.
"""
import logging
from typing import Any, Dict, Optional

from ..event import request_info
from ._common import begin_request, end_request, framework_version, once_flag, report, set_route, set_status

logger = logging.getLogger("insight_sdk")


def _request_data() -> Dict[str, Any]:
    from quart import request

    return request_info(
        request.url.split("?", 1)[0], request.method, request.query_string.decode("utf-8", "replace"), request.headers.items()
    )


def _route() -> Optional[str]:
    from quart import request

    return getattr(getattr(request, "url_rule", None), "rule", None)


def _tags() -> Dict[str, str]:
    tags = {"framework": "quart"}
    version = framework_version("quart")
    if version:
        tags["framework_version"] = version
    route = _route()
    if route:
        tags["route"] = route
    return tags


def _on_exception(sender: Any, exception: Any = None, **kwargs: Any) -> None:
    try:
        report(exception, request=_request_data(), tags=_tags())
    except Exception as exc:
        logger.debug("insight_sdk: quart handler failed: %r", exc)


def init_app(app: Any) -> None:
    """Install on a ``Quart`` app (call once, before serving)."""
    if not once_flag(app, "_insight_sdk_installed"):
        return
    from quart import g
    from quart.signals import got_request_exception

    version = framework_version("quart")
    got_request_exception.connect(_on_exception, app, weak=False)

    @app.before_request
    async def _insight_begin() -> None:
        try:
            g._insight_scope = begin_request("quart", _request_data(), version, route=_route())
        except Exception as exc:
            logger.debug("insight_sdk: quart begin failed: %r", exc)

    @app.after_request
    async def _insight_status(response: Any) -> Any:
        set_status(getattr(response, "status_code", None))
        return response

    @app.teardown_request
    async def _insight_end(exc: Any) -> None:
        cm = g.pop("_insight_scope", None)
        try:
            if cm is not None:
                set_route(_route(), cm.scope)
        except Exception:
            pass
        end_request(cm)

    original = app.handle_user_exception

    async def handle_user_exception(e: Any) -> Any:
        result = await original(e)  # re-raises when unhandled: Quart's 500 path + signal report it
        try:
            set_route(_route())
            report(e, request=_request_data(), tags=_tags())
        except Exception as exc:
            logger.debug("insight_sdk: quart handle_user_exception failed: %r", exc)
        return result

    app.handle_user_exception = handle_user_exception  # type: ignore[method-assign]
