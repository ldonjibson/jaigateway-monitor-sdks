"""Plain WSGI middleware: ``app = InsightWSGIMiddleware(app)``.

Also times every request (one performance transaction, from the call until the response iterable is closed, so
streamed bodies are included). Without a route matcher the name is ``"GET <unmatched>"``: plain WSGI has no route
template, so pass ``route=lambda environ: "/items/{id}"`` to name transactions yourself (never the raw path).
"""
import sys
from typing import Any, Callable, Iterable, Optional

from ..event import environ_headers, request_info
from ..performance import begin_web_transaction
from ..scope import push_scope
from ._common import _as_status


def environ_request(environ: Any) -> Any:
    scheme = environ.get("wsgi.url_scheme", "http")
    host = environ.get("HTTP_HOST") or "%s:%s" % (environ.get("SERVER_NAME", ""), environ.get("SERVER_PORT", ""))
    path = (environ.get("SCRIPT_NAME", "") or "") + (environ.get("PATH_INFO", "") or "")
    return request_info(
        "%s://%s%s" % (scheme, host, path), environ.get("REQUEST_METHOD"), environ.get("QUERY_STRING"), environ_headers(environ)
    )


class _Body:
    """Wraps the response iterable so errors raised while streaming are captured too."""

    def __init__(self, result: Iterable[bytes], scope_cm: Any, txn: Any = None) -> None:
        self._result = result
        self._cm = scope_cm
        self._txn = txn

    def __iter__(self) -> Any:
        from .. import capture_exception

        try:
            for chunk in self._result:
                yield chunk
        except Exception:
            capture_exception(sys.exc_info())
            _note(self._txn, failed=True)
            raise
        _finish(self._txn)  # fully streamed: the response is complete

    def close(self) -> None:
        try:
            if hasattr(self._result, "close"):
                self._result.close()  # type: ignore[union-attr]
        finally:
            _finish(self._txn)
            self._cm.__exit__(None, None, None)


def _note(txn: Any, failed: bool = False) -> None:
    if txn is not None and failed:
        txn.failed = True


def _finish(txn: Any) -> None:
    if txn is not None:
        txn.finish()


class InsightWSGIMiddleware:
    def __init__(self, app: Callable[..., Any], route: Optional[Callable[[Any], Optional[str]]] = None) -> None:
        self.app = app
        self.route = route  # optional ``environ -> route template`` used to name performance transactions

    def _name(self, txn: Any, environ: Any) -> None:
        if txn is not None and self.route is not None:
            try:
                txn.set_route(self.route(environ))
            except Exception:
                pass

    def __call__(self, environ: Any, start_response: Callable[..., Any]) -> Any:
        from .. import capture_exception

        cm = push_scope()
        scope = cm.__enter__()
        txn = None
        try:
            scope.set_request(environ_request(environ))
            txn = begin_web_transaction(
                scope, environ.get("REQUEST_METHOD"), (environ.get("SCRIPT_NAME", "") or "") + (environ.get("PATH_INFO", "") or ""),
                traceparent=environ.get("HTTP_TRACEPARENT"),
            )
            if txn is not None:
                original_start_response = start_response

                def start_response(status: Any, headers: Any, exc_info: Any = None) -> Any:  # noqa: F811
                    txn.set_status(_as_status(status))
                    if exc_info is None:
                        return original_start_response(status, headers)
                    return original_start_response(status, headers, exc_info)

            shared = scope.txn  # ours, or the outer layer's: the route is fed to whichever transaction is running
            result = self.app(environ, start_response)
            self._name(shared, environ)
        except Exception:
            capture_exception(sys.exc_info())
            self._name(scope.txn, environ)
            if txn is not None:
                txn.failed = True
                txn.finish()
            cm.__exit__(None, None, None)
            raise
        except BaseException:
            if txn is not None:
                txn.finish()
            cm.__exit__(None, None, None)
            raise
        return _Body(result, cm, txn)
