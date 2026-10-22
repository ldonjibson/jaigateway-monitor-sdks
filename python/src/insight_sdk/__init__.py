"""Insight SDK for Python.

    import insight_sdk
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com", release="web@1.4.2")
    insight_sdk.capture_exception()
"""
import atexit
import datetime
import logging
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern, Sequence, Union

from .client import BeforeSend, Client, build_endpoint
from .integrations import setup_integrations
from .scope import Scope, configure_scope, get_current_scope, push_scope
from .scope import _writable_scope
from .version import VERSION
from .heartbeat import check_in, monitor
from .performance import start_transaction
from .spans import record_span, start_span
from .tracing import continue_trace, get_traceparent

__version__ = VERSION
__all__ = [
    "init", "capture_exception", "capture_message", "set_user", "set_tag", "set_tags", "set_extra",
    "add_breadcrumb", "push_scope", "configure_scope", "get_current_scope", "flush", "close",
    "get_client", "Client", "Scope", "__version__", "check_in", "monitor", "start_transaction", "start_span", "record_span",
    "get_traceparent", "continue_trace",
]

_client: Optional[Client] = None
_lock = threading.Lock()
_atexit_registered = False


def init(
    api_key: Optional[str] = None,
    host: Optional[str] = None,
    endpoint: Optional[str] = None,
    release: Optional[str] = None,
    environment: Optional[str] = None,
    server_name: Optional[str] = None,
    sample_rate: float = 1.0,
    max_breadcrumbs: int = 100,
    before_send: Optional[BeforeSend] = None,
    send_default_pii: bool = False,
    debug: bool = False,
    in_app_include: Sequence[str] = (),
    in_app_exclude: Sequence[str] = (),
    integrations: Optional[Iterable[str]] = None,
    shutdown_timeout: float = 2.0,
    max_queue_size: int = 100,
    max_events_per_minute: int = 60,
    performance: bool = True,
    performance_ignore: Optional[Sequence[Union[str, Pattern[str]]]] = None,
    performance_flush_interval: float = 60.0,
    performance_spans: bool = True,
    performance_max_spans: int = 200,
    trace_propagation_targets: Sequence[Union[str, Pattern[str]]] = (),
    **options: Any,
) -> Client:
    """Create the global client. ``integrations=None`` auto-enables what the app uses; ``[]`` disables all.

    Performance monitoring (request/task timing) is on by default: ``performance=False`` turns it off,
    ``performance_ignore`` replaces the default ignore list (see ``insight_sdk.performance.DEFAULT_IGNORE``) and
    ``performance_flush_interval`` (seconds, minimum 5) sets how often the summary is sent.
    ``performance_spans=False`` turns off spans (automatic DB / outbound HTTP / cache instrumentation and the
    ``start_span`` summaries); ``performance_max_spans`` (default 200) caps the spans collected per transaction.

    ``trace_propagation_targets`` (default ``()``: nothing is sent) lists the hosts (``"api.example.com"``, ``"*.example.com"``,
    ``"localhost:8000"`` or compiled regexes matched against ``host[:port]``) that receive a W3C ``traceparent`` header on
    outgoing HTTP calls made inside a request/task, so their server joins this trace. Incoming ``traceparent`` headers are
    always read.

    Extra options: ``batch_size``, ``request_timeout``, ``max_attempts``, ``backoff_base``.
    """
    global _client, _atexit_registered
    with _lock:
        if _client is not None:
            _client.close(0.5)
        client = Client(
            api_key=api_key, host=host, endpoint=endpoint, release=release, environment=environment,
            server_name=server_name, sample_rate=sample_rate, max_breadcrumbs=max_breadcrumbs,
            before_send=before_send, send_default_pii=send_default_pii, debug=debug,
            in_app_include=in_app_include, in_app_exclude=in_app_exclude,
            shutdown_timeout=shutdown_timeout, max_queue_size=max_queue_size,
            max_events_per_minute=max_events_per_minute, performance=performance,
            performance_ignore=performance_ignore, performance_flush_interval=performance_flush_interval,
            performance_spans=performance_spans, performance_max_spans=performance_max_spans,
            trace_propagation_targets=trace_propagation_targets, **options,
        )
        _client = client
        if not _atexit_registered:
            atexit.register(_flush_at_exit)
            _atexit_registered = True
    setup_integrations(client, None if integrations is None else list(integrations))
    return client


def _flush_at_exit() -> None:
    try:
        if _client is not None:
            _client.flush()
    except Exception:
        pass


def get_client() -> Optional[Client]:
    return _client


def capture_exception(exc: Any = None, **ctx: Any) -> Optional[str]:
    """Report ``exc`` (an exception, an exc_info tuple, or the one being handled). Returns the event id."""
    c = _client
    return c.capture_exception(exc, **ctx) if c is not None else None


def capture_message(message: str, level: str = "info", **ctx: Any) -> Optional[str]:
    c = _client
    return c.capture_message(message, level, **ctx) if c is not None else None


def set_user(user: Optional[Dict[str, Any]]) -> None:
    _writable_scope().set_user(user)


def set_tag(key: str, value: Any) -> None:
    _writable_scope().set_tag(key, value)


def set_tags(tags: Dict[str, Any]) -> None:
    _writable_scope().set_tags(tags)


def set_extra(key: str, value: Any) -> None:
    _writable_scope().set_extra(key, value)


def add_breadcrumb(
    message: Optional[str] = None,
    category: str = "custom",
    level: str = "info",
    data: Optional[Dict[str, Any]] = None,
    timestamp: Optional[str] = None,
) -> None:
    c = _client
    if c is None:
        return
    crumb: Dict[str, Any] = {
        "timestamp": timestamp or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "category": category,
        "level": level,
        "message": (message or "")[:1000],
    }
    if data:
        crumb["data"] = data
    c.add_breadcrumb(crumb, _writable_scope())


def flush(timeout: Optional[float] = None) -> bool:
    """Block until queued events and the pending performance summary are sent (or ``timeout`` elapses). True if everything was delivered."""
    return _client.flush(timeout) if _client is not None else True


def close(timeout: Optional[float] = None) -> None:
    """Flush, stop the worker, undo integrations and disable the SDK."""
    global _client
    with _lock:
        c, _client = _client, None
    if c is not None:
        c.close(timeout)
