"""The Client: configuration, event building, sampling, dedupe, rate guard and hand-off to the transport."""
import logging
import os
import random
import socket
import sys
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Pattern, Sequence, Union

from . import event as ev
from .performance import PerformanceMonitor
from .scope import Scope, get_current_scope
from .transport import Transport
from .version import SDK_NAME, VERSION

logger = logging.getLogger("insight_sdk")

BeforeSend = Callable[[Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]]
LEVELS = ("fatal", "error", "warning", "info", "debug")
_CTX_KEYS = ("level", "tags", "extra", "user", "fingerprint", "request")


def build_endpoint(host: Optional[str] = None, endpoint: Optional[str] = None) -> Optional[str]:
    """Turn ``host`` (or a full ``endpoint``) into the ingest URL, tolerating slashes and ``/api``."""
    if endpoint:
        url = endpoint.strip()
        if "?" not in url and not url.endswith("/"):
            url += "/"
        return url
    if not host:
        return None
    url = host.strip().rstrip("/")
    if "://" not in url:
        url = "https://" + url
    if url.endswith("/api/ingest/events"):
        return url + "/"
    if url.endswith("/api"):
        return url + "/ingest/events/"
    return url + "/api/ingest/events/"


def build_metrics_endpoint(host: Optional[str] = None, endpoint: Optional[str] = None) -> Optional[str]:
    """The performance-metrics URL (``.../api/ingest/metrics/``) next to the events URL built from the same options."""
    url = build_endpoint(host, endpoint)
    if not url:
        return None
    base, sep, query = url.partition("?")
    head = base.rstrip("/").rpartition("/")[0]  # drop the last path segment ("events")
    return head + "/metrics/" + (sep + query if sep else "")


class _RateGuard:
    """Sliding window: at most ``limit`` events per 60 s."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._times: Deque[float] = deque()
        self._lock = threading.Lock()
        self.warned = False

    def allow(self) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            while self._times and now - self._times[0] > 60:
                self._times.popleft()
            if len(self._times) >= self.limit:
                return False
            self._times.append(now)
            return True


class Client:
    def __init__(
        self,
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
        shutdown_timeout: float = 2.0,
        max_queue_size: int = 100,
        max_events_per_minute: int = 60,
        batch_size: int = 20,
        request_timeout: float = 10.0,
        max_attempts: int = 5,
        backoff_base: float = 1.0,
        performance: bool = True,
        performance_ignore: Optional[Sequence[Union[str, Pattern[str]]]] = None,
        performance_flush_interval: float = 60.0,
        performance_spans: bool = True,
        performance_max_spans: int = 200,
        trace_propagation_targets: Sequence[Union[str, Pattern[str]]] = (),
    ) -> None:
        self.api_key = api_key or os.environ.get("INSIGHT_API_KEY")
        self.endpoint = build_endpoint(host or os.environ.get("INSIGHT_HOST"), endpoint)
        self.release = release or os.environ.get("INSIGHT_RELEASE")
        self.environment = environment or os.environ.get("INSIGHT_ENVIRONMENT")
        self.server_name = server_name or _hostname()
        self.sample_rate = sample_rate
        self.max_breadcrumbs = max_breadcrumbs
        self.before_send = before_send
        self.send_default_pii = send_default_pii
        self.debug = debug
        self.in_app_include = tuple(in_app_include)
        self.in_app_exclude = tuple(in_app_exclude)
        self.shutdown_timeout = shutdown_timeout
        self.rate_guard = _RateGuard(max_events_per_minute)
        self.transport: Optional[Transport] = None
        self._teardowns: List[Callable[[], None]] = []
        self._span_undo: Optional[Callable[[], None]] = None
        if debug:
            _enable_debug_logging()
        if self.api_key and self.endpoint:
            self.transport = Transport(
                self.endpoint,
                self.api_key,
                queue_size=max_queue_size,
                batch_size=batch_size,
                timeout=request_timeout,
                max_attempts=max_attempts,
                backoff_base=backoff_base,
            )
        else:
            logger.warning("insight_sdk: api_key and host/endpoint are required; events will be dropped")
        # Performance monitoring has its own endpoint, quota and state: it can be off, paused or rejected while errors flow.
        self.performance: Optional[PerformanceMonitor] = None
        metrics_endpoint = build_metrics_endpoint(host or os.environ.get("INSIGHT_HOST"), endpoint)
        if performance and self.api_key and metrics_endpoint:
            self.performance = PerformanceMonitor(
                metrics_endpoint,
                self.api_key,
                release=self.release,
                environment=self.environment,
                server_name=self.server_name,
                ignore=performance_ignore,
                flush_interval=performance_flush_interval,
                timeout=request_timeout,
                spans=performance_spans,
                max_spans=performance_max_spans,
                trace_propagation_targets=trace_propagation_targets,
            )
        self.performance_spans = bool(performance_spans)

    @property
    def enabled(self) -> bool:
        return self.transport is not None

    # -- capture ----------------------------------------------------------------------
    def capture_exception(self, exc: Any = None, scope: Optional[Scope] = None, **ctx: Any) -> Optional[str]:
        try:
            exc_info = _normalise_exc_info(exc)
            if exc_info is None:
                return None
            exc_value = exc_info[1]
            if getattr(exc_value, "__insight_captured__", False):
                return None  # same exception object already reported (e.g. logging + middleware)
            try:
                exc_value.__insight_captured__ = True
            except Exception:
                pass
            if not self._admit():
                return None
            chain = ev.exception_chain(exc_value, self.in_app_include, self.in_app_exclude)
            main = chain[0]
            event = self._base_event(scope, ctx, level="error")
            event["description"] = ev.describe(main["type"], main["value"])
            event["exception"] = {"type": main["type"], "value": main["value"], "stacktrace": main["stacktrace"]}
            event["metadata"] = {"traceback": ev.format_traceback(exc_info)}
            if len(chain) > 1:
                event["extra"]["exception_chain"] = [
                    dict(c, stacktrace=c["stacktrace"][:20]) if i else {k: v for k, v in c.items() if k != "stacktrace"}
                    for i, c in enumerate(chain)
                ]
            return self._finish(event, {"exc_info": exc_info})
        except Exception as e:  # never raise into the host app
            logger.debug("insight_sdk: capture_exception failed: %r", e)
            return None

    def capture_message(self, message: str, level: str = "info", scope: Optional[Scope] = None, **ctx: Any) -> Optional[str]:
        try:
            if not self._admit():
                return None
            event = self._base_event(scope, dict(ctx, level=ctx.get("level", level)), level=level)
            event["description"] = str(message)[:255]
            event["message"] = str(message)[:2000]
            return self._finish(event, {"message": message})
        except Exception as e:
            logger.debug("insight_sdk: capture_message failed: %r", e)
            return None

    def add_breadcrumb(self, crumb: Dict[str, Any], scope: Scope) -> None:
        scope.add_breadcrumb(crumb, self.max_breadcrumbs)

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Deliver queued events, then the performance window recorded so far (sharing one time budget)."""
        budget = self.shutdown_timeout if timeout is None else timeout
        deadline = time.monotonic() + budget
        ok = True if self.transport is None else self.transport.flush(budget)
        if self.performance is not None:
            ok = self.performance.flush(max(0.25, deadline - time.monotonic())) and ok
        return ok

    def close(self, timeout: Optional[float] = None) -> None:
        for undo in self._teardowns:
            try:
                undo()
            except Exception:
                pass
        self._teardowns = []
        undo_spans = getattr(self, "_span_undo", None)
        if undo_spans is not None:
            self._span_undo = None
            try:
                undo_spans()
            except Exception:
                pass
        budget = self.shutdown_timeout if timeout is None else timeout
        if self.transport is not None:
            self.transport.close(budget)
        if self.performance is not None:
            self.performance.close(budget)

    # -- internals --------------------------------------------------------------------
    def _admit(self) -> bool:
        if self.transport is None:
            return False
        if self.sample_rate < 1.0 and random.random() >= self.sample_rate:
            return False
        if not self.rate_guard.allow():
            if not self.rate_guard.warned:
                self.rate_guard.warned = True
                logger.warning("insight_sdk: client-side rate limit reached; dropping events (see max_events_per_minute)")
            return False
        self.rate_guard.warned = False
        return True

    def _base_event(self, scope: Optional[Scope], ctx: Dict[str, Any], level: str) -> Dict[str, Any]:
        scope = scope or get_current_scope()
        extra_ctx = {k: v for k, v in ctx.items() if k not in _CTX_KEYS}
        tags = ev.runtime_tags()
        tags.update(scope.tags)
        tags.update({str(k): str(v)[:200] for k, v in (ctx.get("tags") or {}).items()})
        extra: Dict[str, Any] = dict(ev.runtime_extra())
        extra["server_name"] = self.server_name
        extra.update(scope.extra)
        extra.update(ctx.get("extra") or {})
        extra.update(extra_ctx)
        lvl = ctx.get("level") or scope.level or level
        event: Dict[str, Any] = {
            "event_id": uuid.uuid4().hex,
            "event_type": "error",
            "action": "exception",
            "status": "error",
            "level": lvl if lvl in LEVELS else level,
            "timestamp": ev.utcnow_iso(),
            "platform": "python",
            "sdk": {"name": SDK_NAME, "version": VERSION},
            "server_name": self.server_name,
            "tags": dict(list(tags.items())[:50]),
            "extra": extra,
            "breadcrumbs": list(scope.breadcrumbs),
        }
        if self.release:
            event["release"] = self.release
        if self.environment:
            event["environment"] = self.environment
        user = ctx.get("user") or scope.user
        if user:
            event["user"] = ev.jsonable(user)
        request = ctx.get("request") or scope.request
        if request:
            event["request"] = request
        try:  # link the error to the request/task trace it happened in
            txn = getattr(scope, "txn", None)
            if txn is not None and txn.trace_id:
                event["trace_id"] = txn.trace_id
            else:  # background work inside ``continue_trace`` without a transaction
                from .tracing import joined_trace

                joined = joined_trace()
                if joined is not None:
                    event["trace_id"] = joined.trace_id
        except Exception:
            pass
        fingerprint = ctx.get("fingerprint") or scope.fingerprint
        if fingerprint:
            event["fingerprint"] = [str(f) for f in fingerprint]
        return event

    def _finish(self, event: Dict[str, Any], hint: Dict[str, Any]) -> Optional[str]:
        event["extra"] = ev.fit_extra(event["extra"])
        for crumb in event["breadcrumbs"]:
            if "data" in crumb:
                crumb["data"] = ev.jsonable(crumb["data"])
        if self.before_send is not None:
            try:
                result = self.before_send(event, hint)
            except Exception as e:
                logger.warning("insight_sdk: before_send raised %r; event dropped", e)
                return None
            if result is None:
                return None
            event = result
        assert self.transport is not None
        return event.get("event_id") or "" if self.transport.submit(event) else None


def _normalise_exc_info(exc: Any) -> Optional[ev.ExcInfo]:
    if exc is None:
        info = sys.exc_info()
        return info if info[1] is not None else None  # type: ignore[return-value]
    if isinstance(exc, tuple) and len(exc) == 3:
        return exc if exc[1] is not None else None
    if isinstance(exc, BaseException):
        return (type(exc), exc, exc.__traceback__)
    return None


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return ""


def _enable_debug_logging() -> None:
    lg = logging.getLogger("insight_sdk")
    lg.setLevel(logging.DEBUG)
    if not any(getattr(h, "_insight_debug", False) for h in lg.handlers):
        h = logging.StreamHandler()
        h._insight_debug = True  # type: ignore[attr-defined]
        h.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(h)
