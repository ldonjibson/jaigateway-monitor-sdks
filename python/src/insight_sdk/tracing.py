"""Distributed tracing helpers (protocol "Distributed tracing": the W3C ``traceparent`` header).

Inbound: ``parse_traceparent`` (used by every web integration through ``begin_request``).
Outbound: ``compile_targets`` / ``outbound_context`` (used by the HTTP client instrumentations in
``integrations/instrument.py``). Public API: ``get_traceparent()`` and ``continue_trace()``.
Nothing here ever raises into the host application.
"""
import fnmatch
import logging
import os
import re
from contextvars import ContextVar
from typing import Any, Callable, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("insight_sdk")

_RE_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16


class TraceContext(NamedTuple):
    trace_id: str
    parent_span_id: str
    sampled: bool


def new_span_id() -> str:
    """A random 16-hex span id (never all zeros)."""
    while True:
        value = os.urandom(8).hex()
        if value != _ZERO_SPAN:
            return value


def parse_traceparent(value: Any) -> Optional[TraceContext]:
    """Parse a ``traceparent`` header value. Returns None for anything malformed (wrong version, bad length, non-hex,
    all-zero ids). Surrounding whitespace is ignored and upper-case hex is accepted (normalised to lower case)."""
    try:
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        if not isinstance(value, str) or len(value) > 200:
            return None
        m = _RE_TRACEPARENT.match(value.strip().lower())
        if m is None:
            return None
        trace_id, parent_id, flags = m.group(1), m.group(2), m.group(3)
        if trace_id == _ZERO_TRACE or parent_id == _ZERO_SPAN:
            return None
        return TraceContext(trace_id, parent_id, bool(int(flags, 16) & 1))
    except Exception:
        return None


def format_traceparent(trace_id: str, span_id: str, sampled: bool) -> str:
    return "00-%s-%s-%s" % (trace_id, span_id, "01" if sampled else "00")


def header_value(headers: Any, name: str = "traceparent") -> Optional[str]:
    """Case-insensitive lookup in a mapping or an iterable of (name, value) pairs."""
    try:
        if headers is None:
            return None
        items = headers.items() if hasattr(headers, "items") else headers
        for k, v in items:
            if isinstance(k, bytes):
                k = k.decode("latin-1")
            if str(k).lower() == name:
                return v.decode("latin-1") if isinstance(v, bytes) else str(v)
    except Exception:
        pass
    return None


# -- the trace joined by continue_trace() (read by transactions started inside it) ----------------------------------------
_joined: "ContextVar[Optional[TraceContext]]" = ContextVar("insight_joined_trace", default=None)
_current_span: "ContextVar[Optional[str]]" = ContextVar("insight_current_span", default=None)


def joined_trace() -> Optional[TraceContext]:
    return _joined.get()


def _active_txn() -> Any:
    from .scope import get_current_scope

    txn = get_current_scope().txn
    if txn is not None and not txn.done and txn.trace_id and not txn.ignored:
        return txn
    return None


def current_context() -> Optional[Tuple[str, str, bool]]:
    """(trace_id, current span id, sampled) of the active transaction or joined trace, or None."""
    txn = _active_txn()
    if txn is not None:
        return txn.trace_id, _current_span.get() or txn.span_id, bool(txn.sampled)
    joined = _joined.get()
    if joined is not None:
        return joined.trace_id, _current_span.get() or joined.parent_span_id, joined.sampled
    return None


def current_trace_id() -> Optional[str]:
    ctx = current_context()
    return ctx[0] if ctx else None


def get_traceparent() -> Optional[str]:
    """The ``traceparent`` header value for the current trace (use it when you call a service with your own client), or
    None outside a transaction. Inside ``with start_span(...) as span`` the parent is that span, otherwise the request's
    root span. Never raises."""
    try:
        ctx = current_context()
        return format_traceparent(*ctx) if ctx else None
    except Exception:
        return None


class continue_trace:
    """Join a trace for background work::

        with insight_sdk.continue_trace(traceparent, name="rebuild_index", op="task"):
            ...

    Without ``name`` it only makes transactions started inside the block (``start_transaction``, job integrations) and
    ``get_traceparent()`` / error events use the given trace. With ``name`` a transaction (``start_transaction``) is
    opened for you. An invalid or missing header is ignored (the block runs untouched, a new trace is started). Never raises.
    """

    def __init__(self, traceparent: Any, name: Optional[str] = None, op: str = "custom") -> None:
        self.ctx = parse_traceparent(traceparent)
        self.name = name
        self.op = op
        self._token: Any = None
        self._txn: Any = None

    def __enter__(self) -> "continue_trace":
        try:
            if self.ctx is not None:
                self._token = _joined.set(self.ctx)
        except Exception:
            self._token = None
        if self.name:
            try:
                from .performance import start_transaction

                self._txn = start_transaction(self.name, self.op)
                self._txn.__enter__()
            except Exception:
                self._txn = None
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._txn is not None:
            try:
                self._txn.__exit__(exc_type, exc, tb)
            except Exception:
                pass
            self._txn = None
        if self._token is not None:
            try:
                _joined.reset(self._token)
            except Exception:
                try:
                    _joined.set(None)
                except Exception:
                    pass
            self._token = None
        return None

    async def __aenter__(self) -> "continue_trace":
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return self.__exit__(exc_type, exc, tb)


# -- outbound propagation targets ---------------------------------------------------------------------------------------
Matcher = Callable[[str], bool]


def compile_targets(targets: Any) -> Optional[Matcher]:
    """``host -> bool`` for ``tracePropagationTargets`` (strings with ``*`` wildcards, or compiled regexes), or None when empty.
    The argument is the host with a non-default port (``api.example.com``, ``localhost:8000``)."""
    if targets is None:
        return None
    if isinstance(targets, str) or hasattr(targets, "search"):
        targets = [targets]
    globs = []
    regexes = []
    try:
        for t in targets:
            if isinstance(t, str):
                t = t.strip().lower()
                if t:
                    globs.append(t)
            elif hasattr(t, "search"):
                regexes.append(t)
    except Exception:
        return None
    if not globs and not regexes:
        return None

    def match(host_port: str) -> bool:
        try:
            host_port = (host_port or "").lower()
            if not host_port:
                return False
            hostname = host_port.rsplit(":", 1)[0] if re.search(r":\d+$", host_port) else host_port
            for g in globs:
                if fnmatch.fnmatchcase(host_port, g) or fnmatch.fnmatchcase(hostname, g):
                    return True
            for r in regexes:
                if r.search(host_port) or r.search(hostname):
                    return True
        except Exception:
            pass
        return False

    return match


def _insight_host() -> str:
    try:
        from . import get_client

        client = get_client()
        endpoint = getattr(client, "endpoint", None)
        if endpoint:
            from .spans import host_of_url

            return host_of_url(endpoint).lower()
    except Exception:
        pass
    return ""


def outbound_context(host: str) -> Optional[Tuple[str, bool]]:
    """(trace_id, sampled) when a ``traceparent`` should be added to a call to ``host`` (host[:port]), else None.

    Requires: targets configured and matching, an active transaction (or a trace joined with ``continue_trace``), and
    the call not being the SDK's own (suppressed, or the Insight host). Never raises."""
    try:
        from .performance import current_monitor
        from .spans import _suppress

        if _suppress.get() or not host:
            return None
        monitor = current_monitor()
        match = getattr(monitor, "propagation_match", None) if monitor is not None else None
        if match is None or not match(host):
            return None
        ctx = current_context()
        if ctx is None:
            return None
        if host.lower() == _insight_host():
            return None
        return ctx[0], ctx[2]
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("insight_sdk: outbound_context failed: %r", e)
        return None

