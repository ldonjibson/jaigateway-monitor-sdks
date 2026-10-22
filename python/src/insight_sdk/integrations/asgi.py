"""ASGI middleware (Starlette/FastAPI/any ASGI app): ``app.add_middleware(InsightASGIMiddleware)``.

For FastAPI/Starlette/Litestar prefer the dedicated one-liners (``insight_sdk.integrations.fastapi.init_app``),
which also register exception handlers and tag the route template.
"""
from typing import Any, Awaitable, Callable, Dict, MutableMapping, Optional

from ..event import request_info
from ._common import add_http_breadcrumb, begin_request, end_request, normalize_route, report, set_route, set_status

Scope = MutableMapping[str, Any]
SCOPE_KEY = "insight_sdk.scope"  # the Insight scope of the request, stored in the ASGI scope


def asgi_request(scope: Scope) -> Dict[str, Any]:
    headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])]
    host = dict(headers).get("host")
    if not host:
        server = scope.get("server") or ("", "")
        host = "%s:%s" % (server[0], server[1]) if server[1] else server[0]
    path = (scope.get("root_path", "") or "") + (scope.get("path", "") or "")
    url = "%s://%s%s" % (scope.get("scheme", "http"), host, path)
    return request_info(url, scope.get("method"), scope.get("query_string", b"").decode("latin-1"), headers)


def _find_endpoint(routes: Any, endpoint: Any, prefix: str = "", depth: int = 0) -> Optional[str]:
    if depth > 6:
        return None
    for route in list(routes or []):
        sub = getattr(route, "routes", None)
        path = getattr(route, "path", "") or ""
        if sub is not None:
            found = _find_endpoint(sub, endpoint, prefix + path.rstrip("/"), depth + 1)
            if found:
                return found
        elif getattr(route, "endpoint", None) is endpoint:
            return prefix + path
    return None


def asgi_route(scope: Scope) -> Optional[str]:
    """Route template (``/items/{id}``) of the matched route, if routing has happened."""
    template = scope.get("path_template")  # Litestar
    if isinstance(template, str) and template:
        return normalize_route(template)
    route = scope.get("route")  # FastAPI
    path = getattr(route, "path", None) or getattr(route, "path_format", None)
    if isinstance(path, str):
        base = scope.get("insight_sdk.root_path", "")
        root = scope.get("root_path", "") or ""
        prefix = root[len(base):] if root.startswith(base) else ""  # mounted sub-applications
        return normalize_route(prefix.rstrip("/") + path)
    endpoint = scope.get("endpoint")  # plain Starlette: find the route that owns the endpoint
    if endpoint is not None:
        found = _find_endpoint(getattr(scope.get("app"), "routes", None), endpoint)
        if found:
            return normalize_route(found)
    return None


class InsightASGIMiddleware:
    def __init__(
        self, app: Callable[..., Awaitable[None]], framework: Optional[str] = None, version: Optional[str] = None
    ) -> None:
        self.app = app
        self.framework = framework
        self.version = version

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        import sys

        request = asgi_request(scope)
        is_http = scope.get("type") == "http"
        cm = begin_request(self.framework or "asgi", request, self.version, crumb=is_http, perf=is_http)
        s = cm.scope
        scope["insight_sdk.root_path"] = scope.get("root_path", "") or ""
        scope[SCOPE_KEY] = s
        crumb = s.breadcrumbs[-1] if s.breadcrumbs and s.breadcrumbs[-1].get("category") == "http" else None

        async def send_wrapper(message: Any) -> None:
            if message.get("type") == "http.response.start":
                set_status(message.get("status"), s)
                if crumb is not None:
                    try:
                        crumb["data"]["status_code"] = message.get("status")
                    except Exception:
                        pass
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            set_route(asgi_route(scope), s)
            report(sys.exc_info()[1], scope=s)
            raise
        finally:
            if cm.txn is not None:  # name the transaction after the matched route template (never the raw path)
                set_route(asgi_route(scope), s)
            end_request(cm)
