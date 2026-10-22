"""Litestar.

    import insight_sdk
    from insight_sdk.integrations.litestar import InsightPlugin
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); app = Litestar([...], plugins=[InsightPlugin()])

The plugin adds an ASGI middleware (per-request scope, request data, ``http`` breadcrumb, ``framework`` tags, and the
``route`` tag from Litestar's ``path_template``, e.g. ``/items/{id}``) and an ``after_exception`` hook that reports
unhandled exceptions and ``HTTPException`` with status >= 500 once. 4xx and validation errors are not reported;
Litestar's own error responses are untouched.
"""
import logging
from typing import Any

from ..performance import begin_web_transaction
from ..tracing import header_value
from ..scope import Scope
from ._common import framework_version, note_exception, report, set_route
from .asgi import SCOPE_KEY, InsightASGIMiddleware, asgi_request, asgi_route

logger = logging.getLogger("insight_sdk")

try:  # Litestar >= 2.15 renamed the protocol
    from litestar.plugins import InitPlugin as _PluginBase
except ImportError:  # pragma: no cover - older Litestar
    from litestar.plugins import InitPluginProtocol as _PluginBase  # type: ignore[assignment]


async def after_exception(exc: BaseException, scope: Any) -> None:
    """Litestar ``after_exception`` hook: report 5xx/unexpected exceptions (deduped with the middleware)."""
    try:
        s = scope.get(SCOPE_KEY)
        ctx: Any = {}
        txn = None
        if s is None:  # raised before our middleware ran (e.g. no route matched: Litestar answers 404 before any middleware)
            ctx["request"] = asgi_request(scope)
            ctx["tags"] = {"framework": "litestar", "route": asgi_route(scope) or ""}
            ctx["tags"] = {k: v for k, v in ctx["tags"].items() if v}
            # still count it (name "<METHOD> <unmatched>"); no start time exists here, so its duration is ~0
            holder = Scope()
            txn = begin_web_transaction(
                holder, scope.get("method"), scope.get("path"), asgi_route(scope), header_value(scope.get("headers"))
            )
            note_exception(exc, holder)
        else:
            set_route(asgi_route(scope), s)
        report(exc, scope=s, **ctx)
        if txn is not None:
            txn.finish()
    except Exception as e:
        logger.debug("insight_sdk: litestar after_exception failed: %r", e)


class InsightPlugin(_PluginBase):  # type: ignore[misc,valid-type]
    """``Litestar([...], plugins=[InsightPlugin()])``"""

    def on_app_init(self, app_config: Any) -> Any:
        from litestar.middleware import DefineMiddleware

        app_config.middleware.insert(
            0, DefineMiddleware(InsightASGIMiddleware, framework="litestar", version=framework_version("litestar"))
        )
        app_config.after_exception.append(after_exception)
        return app_config
