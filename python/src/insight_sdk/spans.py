"""Spans: timed steps inside a transaction (database queries, outbound HTTP calls, your own blocks).

``start_span(op, description)`` is the public API; ``record_span(...)`` is what the automatic instrumentations
(``integrations/instrument.py``) call. Recording never raises and never blocks. Descriptions are cleaned here
(SQL: no literals; HTTP: ``METHOD host`` only) because they must never contain values.
"""
import functools
import inspect
import logging
import re
import time
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from .performance import current_monitor
from .scope import get_current_scope
from .tracing import _current_span, current_context, format_traceparent, new_span_id

logger = logging.getLogger("insight_sdk")

MAX_DESCRIPTION = 300
MAX_OP = 40
DATA_KEYS = ("db.system", "db.name", "http.method", "http.status_code", "host", "cache.hit")

# -- text hygiene (ports of normalizeSql / normalizeHttp in server/perf.ts) ------------------------------------------
_A = re.ASCII
_RE_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_RE_LINE = re.compile(r"--[^\n]*")
_RE_STR = re.compile(r"'(?:[^']|'')*'")
_RE_ESTR = re.compile(r"\bE'(?:[^'\\]|\\.)*'", _A)
_RE_HEX = re.compile(r"\b0x[0-9a-f]+\b", _A | re.IGNORECASE)
_RE_NUM = re.compile(r"(?<![\w$.])-?\d+(?:\.\d+)?(?:e[+-]?\d+)?\b", _A | re.IGNORECASE)
_RE_IN = re.compile(r"\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", _A | re.IGNORECASE)
_RE_VALUES = re.compile(
    r"\bVALUES\s*\(\s*\?(?:\s*,\s*\?)*\s*\)(?:\s*,\s*\(\s*\?(?:\s*,\s*\?)*\s*\))*", _A | re.IGNORECASE
)
_RE_WS = re.compile(r"\s+")


def normalize_sql(sql: Any) -> str:
    """SQL with every literal removed: no values, no comments, one line, at most 300 chars (same as the server)."""
    s = "" if sql is None else str(sql)
    s = _RE_LINE.sub(" ", _RE_BLOCK.sub(" ", s))
    s = _RE_ESTR.sub("?", _RE_STR.sub("?", s))
    s = _RE_NUM.sub("?", _RE_HEX.sub("?", s))
    s = _RE_IN.sub("IN (?)", s)
    s = _RE_VALUES.sub("VALUES (?)", s)
    s = _RE_WS.sub(" ", s).strip()
    return s[:297] + "..." if len(s) > 300 else s


def normalize_http(desc: Any) -> str:
    """``"POST https://user:pw@api.stripe.com/v1/charges?x=1"`` -> ``"POST api.stripe.com"``."""
    text = "" if desc is None else str(desc).strip()
    m = re.match(r"^([A-Z]+)\s+(\S+)", text)
    method = m.group(1) if m else ""
    target = m.group(2) if m else text
    host = target
    try:
        parts = urlsplit(target if re.match(r"^[a-z][a-z0-9+.-]*://", target, re.I) else "http://" + target)
        host = _host_of(parts)
    except Exception:
        host = re.sub(r"^.*@", "", re.split(r"[/?#]", target)[0])
    return ((method + " " if method else "") + host)[:300]


def _host_of(parts: Any) -> str:
    """Host (+ non-default port) of a SplitResult, no credentials."""
    hostname = parts.hostname or ""
    if ":" in hostname:
        hostname = "[%s]" % hostname
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not ((parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)):
        return "%s:%d" % (hostname, port)
    return hostname


def host_of_url(url: Any) -> str:
    """Host of a URL string (no scheme, path, query or credentials); '' when unknown."""
    try:
        return _host_of(urlsplit(str(url)))
    except Exception:
        return ""


_sql_cache: Dict[str, str] = {}


def _cached_sql(sql: str) -> str:
    out = _sql_cache.get(sql)
    if out is None:
        out = normalize_sql(sql)
        if len(sql) <= 2000:
            if len(_sql_cache) >= 512:
                _sql_cache.clear()
            _sql_cache[sql] = out
    return out


def _clean_description(op: str, description: Any) -> str:
    text = "" if description is None else str(description)
    if op.startswith("db"):
        return _cached_sql(text)
    if op.startswith("http"):
        return normalize_http(text)
    return _RE_WS.sub(" ", text).strip()[:MAX_DESCRIPTION]


def _clean_data(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not data:
        return None
    out: Dict[str, Any] = {}
    for k in DATA_KEYS:
        v = data.get(k)
        if isinstance(v, (bool, int, float)):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:200]
    return out or None


# -- recording ------------------------------------------------------------------------------------------------------
_suppress: "ContextVar[bool]" = ContextVar("insight_spans_suppressed", default=False)


class suppressed:
    """``with suppressed():`` the SDK's own network calls are not recorded as spans."""

    def __enter__(self) -> "suppressed":
        self._tok = _suppress.set(True)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            _suppress.reset(self._tok)
        except Exception:
            pass


def record_span(
    op: str,
    description: str,
    start: float,
    duration_ms: float,
    ok: bool = True,
    data: Optional[Dict[str, Any]] = None,
    span_id: Optional[str] = None,
) -> None:
    """Record a finished span. ``start`` is a ``time.perf_counter()`` value (seconds). The span joins the current
    transaction; outside one it is only summarised under the transaction name ``""``. ``span_id`` (16 hex) is the id the span
    carries in samples (set it when the same id was sent to another service in a ``traceparent``). Never raises."""
    try:
        if _suppress.get():
            return
        monitor = current_monitor()
        if monitor is None or not monitor.spans_enabled:
            return
        if duration_ms != duration_ms or duration_ms < 0:
            duration_ms = 0.0
        op = str(op or "custom")[:MAX_OP]
        desc = _clean_description(op, description)
        txn = get_current_scope().txn
        if txn is not None and txn.ignored:
            return
        if txn is not None and not txn.done and txn.monitor is monitor:
            txn.add_span(op, desc, max(0.0, (start - txn.t0) * 1000.0), duration_ms, bool(ok), _clean_data(data), span_id)
        else:
            monitor.record_span_loose(op, desc, duration_ms, bool(ok))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("insight_sdk: record_span failed: %r", e)


class start_span:
    """Time a block of your own code::

        with insight_sdk.start_span("db.query", "SELECT * FROM items WHERE id = 1"):
            ...
        async with insight_sdk.start_span("cache", "GET"): ...

        @insight_sdk.start_span("task", "resize image")        # also async functions
        def resize(): ...

    An exception that propagates marks the span as an error. SQL (``db*`` ops) and HTTP (``http*`` ops) descriptions are
    normalised; never put ids or personal data in other descriptions. ``data`` keeps only the allowed keys
    (db.system, db.name, http.method, http.status_code, host, cache.hit).
    """

    def __init__(self, op: str, description: str = "", data: Optional[Dict[str, Any]] = None) -> None:
        self.op = op
        self.description = description
        self.data: Dict[str, Any] = dict(data) if data else {}
        self._t0: Optional[float] = None
        self._error = False
        self.span_id: Optional[str] = None
        self._tok: Any = None

    def set_data(self, key: str, value: Any) -> None:
        self.data[key] = value

    def set_error(self) -> None:
        self._error = True

    def __enter__(self) -> "start_span":
        self._t0 = time.perf_counter()
        self._error = False
        self.span_id = new_span_id()
        try:
            self._tok = _current_span.set(self.span_id)
        except Exception:
            self._tok = None
        return self

    @property
    def traceparent(self) -> Optional[str]:
        """``traceparent`` header value whose parent is this span (valid inside the block; None outside a trace)."""
        try:
            ctx = current_context()
            return format_traceparent(ctx[0], self.span_id or new_span_id(), ctx[2]) if ctx else None
        except Exception:
            return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        t0, self._t0 = self._t0, None
        if t0 is None:
            return None
        tok, self._tok = self._tok, None
        if tok is not None:
            try:
                _current_span.reset(tok)
            except Exception:
                pass
        try:
            ok = not self._error and not (exc_type is not None and issubclass(exc_type, Exception))
            record_span(self.op, self.description, t0, (time.perf_counter() - t0) * 1000.0, ok, self.data, self.span_id)
        except Exception:  # pragma: no cover
            pass
        return None

    async def __aenter__(self) -> "start_span":
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return self.__exit__(exc_type, exc, tb)

    def __call__(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        op, description, data = self.op, self.description, self.data
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                with start_span(op, description, data):
                    return await fn(*args, **kwargs)

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with start_span(op, description, data):
                return fn(*args, **kwargs)

        return wrapper
