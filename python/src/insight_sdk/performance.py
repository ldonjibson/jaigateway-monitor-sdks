"""Backend performance monitoring (protocol "Performance metrics (protocol v1)").

Requests and tasks are *aggregated* in memory per transaction name (count, errors, sum/min/max, a fixed-bucket latency
histogram) plus a few individual samples per window, and one small summary is POSTed to ``/api/ingest/metrics/`` about
every minute from a background daemon thread. Recording never blocks and never raises.
"""
import atexit  # noqa: F401  (flush at exit is driven by insight_sdk.init's atexit hook -> Client.flush)
import datetime
import functools
import heapq
import inspect
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from bisect import bisect_left
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Pattern, Sequence, Tuple, Union

from .tracing import new_span_id, parse_traceparent
from .transport import MAX_QUOTA_PAUSE, MAX_RETRY_AFTER, _error_code, http_post, parse_retry_after
from .version import SDK_NAME, VERSION

logger = logging.getLogger("insight_sdk")

BOUNDS_MS: List[int] = [
    1, 2, 3, 5, 8, 12, 18, 25, 40, 60, 90, 130, 200, 300, 450, 700, 1000, 1500, 2200, 3300, 5000, 7500, 11000, 17000,
    25000, 40000, 60000,
]
N_BUCKETS = len(BOUNDS_MS) + 1  # the last bucket (index 27) holds everything above 60 s

# Matched on the raw path. Entries ending in ``*`` are prefixes, everything else is an exact path (a trailing
# slash on the request path is ignored, so ``/health/`` matches ``/health``).
DEFAULT_IGNORE: Tuple[str, ...] = (
    "/health", "/healthz", "/ping", "/status", "/metrics", "/favicon.ico", "/robots.txt", "/ready", "/live",
    "/livez", "/readyz", "/static/*", "/assets/*",
)

MAX_NAMES_PER_WINDOW = 499  # + "<other>" = the server's 500 per environment
OTHER = "<other>"
UNMATCHED = "<unmatched>"
MAX_NAME_LEN = 300
MAX_PENDING_WINDOWS = 5
MAX_BODY_BYTES = 900_000
MIN_FLUSH_INTERVAL = 5.0
SLOWEST_SAMPLES = 3
FAILED_SAMPLES = 5
MAX_SAMPLES = 10
FORCED_SAMPLES = 20  # requests of a trace whose sampled flag is set: kept regardless of speed or status
MAX_SPANS_PER_TXN = 200  # collected per transaction (more are only counted in the aggregates)
MAX_SPANS_PER_SAMPLE = 100
MAX_SPAN_SUMMARIES = 300  # per payload
MAX_SPAN_KEYS = 2000  # distinct (transaction, op, description) kept in memory per window


def bucket_index(duration_ms: float) -> int:
    """Index of the first bucket with ``duration_ms <= BOUNDS_MS[i]``; above 60000 ms -> 27."""
    try:
        return bisect_left(BOUNDS_MS, duration_ms)
    except TypeError:
        return 0


def _iso(ts: float, millis: bool = False) -> str:
    d = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    if millis:
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def compile_ignore(patterns: Optional[Sequence[Union[str, Pattern[str]]]]) -> Callable[[str], bool]:
    """Build the ``path -> ignored?`` predicate from strings (exact, or prefix with a trailing ``*``) and regexes."""
    exact = set()
    prefixes: List[str] = []
    regexes: List[Any] = []
    for p in DEFAULT_IGNORE if patterns is None else patterns:
        if isinstance(p, str):
            if p.endswith("*"):
                prefixes.append(p[:-1])
            else:
                exact.add(p.rstrip("/") or "/")
        elif hasattr(p, "search"):
            regexes.append(p)
    tuple_prefixes = tuple(prefixes)

    def ignored(path: str) -> bool:
        path = path or "/"
        if (path.rstrip("/") or "/") in exact:
            return True
        if tuple_prefixes and path.startswith(tuple_prefixes):
            return True
        return any(r.search(path) for r in regexes)

    return ignored


def path_of(url_or_path: Optional[str]) -> str:
    """Raw path of a URL or path (no scheme, host or query)."""
    value = (url_or_path or "").split("?", 1)[0].split("#", 1)[0]
    if "://" in value:
        rest = value.split("://", 1)[1]
        value = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    return value or "/"


def _span_id_of(sp: Any) -> Optional[str]:
    return sp[6] if len(sp) > 6 and sp[6] else None


def sample_spans(spans: Sequence[Any]) -> List[Dict[str, Any]]:
    """Serialise a transaction's spans for a sample: at most 100, the slowest kept, in chronological order."""
    chosen = list(spans)
    if len(chosen) > MAX_SPANS_PER_SAMPLE:
        # spans whose id was sent to another service (traceparent) rank first so the two halves can still be joined
        idx = sorted(range(len(chosen)), key=lambda i: (0 if _span_id_of(chosen[i]) else 1, -chosen[i][3]))[:MAX_SPANS_PER_SAMPLE]
        chosen = [chosen[i] for i in sorted(idx)]
    chosen.sort(key=lambda sp: sp[2])
    out = []
    for sp in chosen:
        op, desc, start, dur, ok, data = tuple(sp[:6])
        d: Dict[str, Any] = {
            "span_id": _span_id_of(sp) or os.urandom(8).hex(), "parent_id": None, "op": op, "description": desc,
            "start_ms": round(max(0.0, start), 3), "duration_ms": round(dur, 3), "status": "ok" if ok else "error",
        }
        if data:
            d["data"] = data
        out.append(d)
    return out


# -- aggregation ------------------------------------------------------------------------------------------------
# A recorded request, kept as a tuple until it is serialised: (duration_ms, name, status, ok, started_wall)
# + trace_id and the transaction's span list (tuples, see spans.py) for samples
# + (op, span_id, parent_span_id, forced)
_Rec = Tuple[float, str, Optional[int], bool, float, Optional[str], Optional[list], str, Optional[str], Optional[str], bool]


class _Stat:
    __slots__ = ("op", "count", "errors", "sum", "min", "max", "buckets")

    def __init__(self, op: str) -> None:
        self.op = op
        self.count = 0
        self.errors = 0
        self.sum = 0.0
        self.min = float("inf")
        self.max = 0.0
        self.buckets = [0] * N_BUCKETS


class Window:
    """Everything recorded between two flushes."""

    def __init__(self, start_wall: float, start_mono: float) -> None:
        self.start_wall = start_wall
        self.start_mono = start_mono
        self.seconds = 0
        self.stats: Dict[str, _Stat] = {}
        self.total = 0
        self.slow: List[Tuple[float, int, _Rec]] = []  # min-heap of the slowest SLOWEST_SAMPLES
        self.failed: List[_Rec] = []  # reservoir of FAILED_SAMPLES failed requests
        self.failed_seen = 0
        self.random: Optional[_Rec] = None  # reservoir of 1 over everything
        self.strip_samples = False  # drop every sample
        self.strip_sample_spans = False  # drop the spans of the regular samples
        self.strip_forced_spans = False  # drop the spans of the forced samples
        self.strip_regular_samples = False  # drop the regular samples, keep the forced ones
        self.strip_spans = False
        self.forced: List[_Rec] = []  # reservoir of FORCED_SAMPLES requests of sampled traces
        self.forced_seen = 0
        self.span_stats: Dict[Tuple[str, str, str], _Stat] = {}

    def add_span_stat(self, txn: str, op: str, desc: str, count: int, errors: int, total: float, mn: float, mx: float, buckets: Optional[List[int]] = None, single: Optional[float] = None) -> None:
        key = (txn, op, desc)
        st = self.span_stats.get(key)
        if st is None:
            if len(self.span_stats) >= MAX_SPAN_KEYS:
                key = (txn, op, OTHER)
                st = self.span_stats.get(key)
            if st is None:
                st = self.span_stats[key] = _Stat(op)
        st.count += count
        st.errors += errors
        st.sum += total
        if mn < st.min:
            st.min = mn
        if mx > st.max:
            st.max = mx
        if buckets is not None:
            for i, c in enumerate(buckets):
                if c:
                    st.buckets[i] += c
        elif single is not None:
            st.buckets[bucket_index(single)] += count

    def add_spans(self, txn: str, spans: Sequence[Any], extra: Optional[Dict[Tuple[str, str], _Stat]]) -> None:
        """Fold one finished transaction's spans (and the ones it only counted) into the window's summaries."""
        for sp in spans:
            op, desc, _start, dur, ok = sp[0], sp[1], sp[2], sp[3], sp[4]
            self.add_span_stat(txn, op, desc, 1, 0 if ok else 1, dur, dur, dur, single=dur)
        if extra:
            for (op, desc), st in extra.items():
                self.add_span_stat(txn, op, desc, st.count, st.errors, st.sum, st.min, st.max, buckets=st.buckets)

    def span_summaries(self) -> List[Dict[str, Any]]:
        items = sorted(self.span_stats.items(), key=lambda kv: -kv[1].sum)
        keep = min(len(items), MAX_SPAN_SUMMARIES)
        while keep > 0:
            groups = {(k[0], k[1]) for k, _ in items[keep:]}
            if keep + len(groups) <= MAX_SPAN_SUMMARIES:
                break
            keep -= 1
        merged: Dict[Tuple[str, str, str], _Stat] = {}
        for k, st in items[:keep]:
            merged[k] = st
        for k, st in items[keep:]:
            ok = (k[0], k[1], OTHER)
            tgt = merged.get(ok)
            if tgt is None:
                tgt = merged[ok] = _Stat(st.op)
            tgt.count += st.count
            tgt.errors += st.errors
            tgt.sum += st.sum
            tgt.min = min(tgt.min, st.min)
            tgt.max = max(tgt.max, st.max)
            for i, c in enumerate(st.buckets):
                tgt.buckets[i] += c
        out = []
        for (txn, op, desc), s in sorted(merged.items(), key=lambda kv: -kv[1].sum):
            out.append({
                "transaction": txn, "op": op, "description": desc, "count": s.count, "errors": s.errors,
                "sum_ms": round(s.sum, 3), "min_ms": round(s.min if s.count else 0.0, 3), "max_ms": round(s.max, 3),
                "buckets": {str(i): c for i, c in enumerate(s.buckets) if c},
            })
        return out

    def add(self, name: str, op: str, duration_ms: float, status: Optional[int], ok: bool, started: float, trace_id: Optional[str] = None, spans: Optional[list] = None, span_id: Optional[str] = None, parent_span_id: Optional[str] = None, sampled: bool = False) -> None:
        stat = self.stats.get(name)
        if stat is None:
            if len(self.stats) >= MAX_NAMES_PER_WINDOW:  # cardinality guard: fold into "<other>"
                name = OTHER
                stat = self.stats.get(name)
            if stat is None:
                stat = self.stats[name] = _Stat(op)
        stat.count += 1
        if not ok:
            stat.errors += 1
        stat.sum += duration_ms
        if duration_ms < stat.min:
            stat.min = duration_ms
        if duration_ms > stat.max:
            stat.max = duration_ms
        stat.buckets[bucket_index(duration_ms)] += 1
        self.total += 1
        rec: _Rec = (duration_ms, name, status, ok, started, trace_id, spans or None, op, span_id, parent_span_id, bool(sampled))
        if sampled:
            self.forced_seen += 1
            if len(self.forced) < FORCED_SAMPLES:
                self.forced.append(rec)
            else:
                j = random.randrange(self.forced_seen)
                if j < FORCED_SAMPLES:
                    self.forced[j] = rec
        if len(self.slow) < SLOWEST_SAMPLES:
            heapq.heappush(self.slow, (duration_ms, self.total, rec))
        elif duration_ms > self.slow[0][0]:
            heapq.heapreplace(self.slow, (duration_ms, self.total, rec))
        if not ok:
            self.failed_seen += 1
            if len(self.failed) < FAILED_SAMPLES:
                self.failed.append(rec)
            else:
                j = random.randrange(self.failed_seen)
                if j < FAILED_SAMPLES:
                    self.failed[j] = rec
        if random.randrange(self.total) == 0:
            self.random = rec

    def samples(self) -> List[Dict[str, Any]]:
        regular: List[_Rec] = [r for _, _, r in sorted(self.slow, key=lambda x: (x[0], x[1]), reverse=True)]
        regular += self.failed
        if self.random is not None:
            regular.append(self.random)
        out: List[Dict[str, Any]] = []
        seen: set = set()
        chosen: List[_Rec] = []
        for rec in regular:  # the usual slowest 3 / failed 5 / 1 random (at most MAX_SAMPLES)
            if id(rec) not in seen and len(seen) < MAX_SAMPLES:
                seen.add(id(rec))
                chosen.append(rec)
        # in addition: up to FORCED_SAMPLES requests of sampled traces (listed first; a forced request that is also
        # one of the regular picks appears once)
        forced_ids = {id(r) for r in self.forced}
        ordered = [r for r in self.forced] + [r for r in chosen if id(r) not in forced_ids]
        for rec in ordered:
            is_forced = id(rec) in forced_ids
            if self.strip_samples or (self.strip_regular_samples and not is_forced):
                continue
            duration, name, status, ok, started, trace_id, spans, op, span_id, parent_span_id, _f = rec
            sample: Dict[str, Any] = {
                "kind": "server",
                "op": op,
                "trace_id": trace_id or uuid.uuid4().hex,
                "span_id": span_id or os.urandom(8).hex(),
            }
            if parent_span_id:
                sample["parent_span_id"] = parent_span_id
            sample.update({
                "name": name,
                "duration_ms": round(duration, 3),
                "ok": ok,
                "started_at": _iso(started, True),
            })
            if status is not None:
                sample["status_code"] = status
            if spans and not (self.strip_forced_spans if is_forced else self.strip_sample_spans):
                sample["spans"] = sample_spans(spans)
            out.append(sample)
        return out

    def transactions(self) -> List[Dict[str, Any]]:
        out = []
        for name, s in sorted(self.stats.items(), key=lambda kv: -kv[1].count):
            out.append({
                "name": name,
                "op": s.op,
                "count": s.count,
                "errors": s.errors,
                "sum_ms": round(s.sum, 3),
                "min_ms": round(s.min, 3),
                "max_ms": round(s.max, 3),
                "buckets": {str(i): c for i, c in enumerate(s.buckets) if c},
            })
        return out


# -- the monitor ------------------------------------------------------------------------------------------------
_monitors: "List[Any]" = []  # weak-ish registry for fork handling (monitors remove themselves on close)


def _after_fork_in_child() -> None:
    for m in list(_monitors):
        m._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


class PerformanceMonitor:
    """Aggregates transactions and delivers them in the background."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        release: Optional[str] = None,
        environment: Optional[str] = None,
        server_name: Optional[str] = None,
        ignore: Optional[Sequence[Union[str, Pattern[str]]]] = None,
        flush_interval: float = 60.0,
        timeout: float = 10.0,
        spans: bool = True,
        max_spans: int = MAX_SPANS_PER_TXN,
        trace_propagation_targets: Optional[Sequence[Union[str, Pattern[str]]]] = None,
    ) -> None:
        from .tracing import compile_targets

        self.propagation_match = compile_targets(trace_propagation_targets)
        self.spans_enabled = bool(spans)
        self.max_spans = max(0, int(max_spans))
        self.endpoint = endpoint
        self.api_key = api_key
        self.release = release
        self.environment = environment
        self.server_name = server_name
        self.is_ignored = compile_ignore(ignore)
        self.flush_interval = max(MIN_FLUSH_INTERVAL, float(flush_interval))
        self.timeout = timeout
        # observable state
        self.active = True  # False after a 401/403 (metrics only)
        self.paused_until = 0.0  # monotonic; quota pause
        self.last_status: Optional[int] = None
        self.sent_windows = 0
        self.sent_requests = 0
        self.dropped_requests = 0
        self._logged: set = set()
        self._init_state()
        _monitors.append(self)

    def _init_state(self) -> None:
        self._cv = threading.Condition(threading.Lock())
        self._send_lock = threading.Lock()
        self._window: Optional[Window] = None
        self._pending: Deque[Window] = deque()
        self._next_send = 0.0  # monotonic: worker-driven retry after a transient failure
        self._rl_until = 0.0  # monotonic: Retry-After of a 429 rate_limited
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pid = os.getpid()

    def _reset_after_fork(self) -> None:
        # The parent still owns the aggregated data and its worker thread; start clean in the child.
        self._init_state()

    # -- recording (request path: must be cheap and never raise) --------------------------------------------------
    def record(
        self,
        name: str,
        op: str,
        duration_ms: float,
        status_code: Optional[int] = None,
        ok: bool = True,
        started_at: Optional[float] = None,
        trace_id: Optional[str] = None,
        spans: Optional[list] = None,
        span_extra: Optional[Dict[Tuple[str, str], _Stat]] = None,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        sampled: bool = False,
    ) -> None:
        try:
            if not self.active:
                return
            if self.paused_until:
                if time.monotonic() < self.paused_until:
                    return
                self.paused_until = 0.0
                self._logged.discard("quota")
                logger.debug("insight_sdk: performance quota pause over, recording again")
            if duration_ms != duration_ms or duration_ms < 0:  # NaN / negative clock hiccup
                duration_ms = 0.0
            if started_at is None:
                started_at = time.time() - duration_ms / 1000.0
            name = str(name)[:MAX_NAME_LEN]
            with self._cv:
                if self._pid != os.getpid():
                    self._reset_after_fork()
                    return self.record(name, op, duration_ms, status_code, ok, started_at, trace_id, spans, span_extra, span_id, parent_span_id, sampled)
                w = self._window
                if w is None:
                    w = self._window = Window(time.time(), time.monotonic())
                    self._ensure_worker()
                    self._cv.notify()
                w.add(name, op, duration_ms, status_code, ok, started_at, trace_id, spans, span_id, parent_span_id, sampled)
                if spans or span_extra:
                    w.add_spans(name, spans or (), span_extra)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("insight_sdk: performance record failed: %r", e)

    def record_span_loose(self, op: str, description: str, duration_ms: float, ok: bool) -> None:
        """A span outside any transaction: summarised under transaction "" (no sample spans)."""
        try:
            if not self.active or not self.spans_enabled:
                return
            if self.paused_until and time.monotonic() < self.paused_until:
                return
            with self._cv:
                if self._pid != os.getpid():
                    self._reset_after_fork()
                w = self._window
                if w is None:
                    w = self._window = Window(time.time(), time.monotonic())
                    self._ensure_worker()
                    self._cv.notify()
                w.add_spans("", [(op, description, 0.0, duration_ms, ok)], None)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("insight_sdk: span record failed: %r", e)

    # -- delivery ------------------------------------------------------------------------------------------------
    def _ensure_worker(self) -> None:  # call with self._cv held
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="insight-sdk-metrics", daemon=True)
            self._thread.start()

    def _rotate_locked(self, now_mono: float) -> None:
        w = self._window
        if w is None:
            return
        self._window = None
        w.seconds = max(1, min(3600, int(round(now_mono - w.start_mono))))
        if len(self._pending) >= MAX_PENDING_WINDOWS:
            old = self._pending.popleft()
            self.dropped_requests += old.total
            logger.debug("insight_sdk: dropped the oldest unsent performance window (%d buffered)", MAX_PENDING_WINDOWS)
        self._pending.append(w)

    def _run(self) -> None:
        cv = self._cv
        while True:
            with cv:
                while True:
                    if self._stop.is_set():
                        return
                    now = time.monotonic()
                    w = self._window
                    if w is not None and now >= w.start_mono + self.flush_interval:
                        self._rotate_locked(now)
                    due: List[float] = []
                    if self._window is not None:
                        due.append(self._window.start_mono + self.flush_interval)
                    if self._pending:
                        ready = max(self._next_send, self._rl_until)
                        if ready <= now:
                            break
                        due.append(ready)
                    if due:
                        cv.wait(max(0.0, min(due) - now))
                    else:
                        cv.wait()
            try:
                self._send_pending(None)
            except Exception as e:  # never let the worker die
                logger.debug("insight_sdk: performance delivery error: %r", e)

    def _log_once(self, key: str, message: str) -> None:
        if key not in self._logged:
            self._logged.add(key)
            logger.warning(message)

    def _payload(self, w: Window, with_samples: bool = True) -> bytes:
        body: Dict[str, Any] = {
            "sdk": {"name": SDK_NAME, "version": VERSION},
            "platform": "python",
            "interval_start": _iso(w.start_wall),
            "interval_seconds": w.seconds,
            "transactions": w.transactions(),
        }
        if not w.strip_spans:
            summaries = w.span_summaries()
            if summaries:
                body["spans"] = summaries
        if self.release:
            body["release"] = self.release
        if self.environment:
            body["environment"] = self.environment
        if self.server_name:
            body["server_name"] = self.server_name
        if with_samples and not w.strip_samples:
            samples = w.samples()
            if samples:
                body["samples"] = samples
        return json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")

    def _drop_all_locked(self) -> None:
        for w in self._pending:
            self.dropped_requests += w.total
        self._pending.clear()
        if self._window is not None:
            self.dropped_requests += self._window.total
            self._window = None

    def _send_pending(self, deadline: Optional[float]) -> bool:
        """Send buffered windows oldest first. True when none is left to send (sent or dropped)."""
        if deadline is None:
            self._send_lock.acquire()
        elif not self._send_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            while True:
                with self._cv:
                    if not self.active or (self.paused_until and time.monotonic() < self.paused_until):
                        self._drop_all_locked()
                        return True
                    if not self._pending:
                        return True
                    w = self._pending[0]
                now = time.monotonic()
                if self._rl_until > now:  # the server told us to wait (429 rate_limited)
                    wait = self._rl_until - now
                    if deadline is None or now + wait > deadline:
                        return False
                    time.sleep(wait)
                body = self._payload(w)
                if len(body) > MAX_BODY_BYTES:
                    # under pressure drop, in this order: regular sample spans, forced (sampled-trace) sample spans,
                    # the regular samples, every sample, the span summaries
                    for step in ("strip_sample_spans", "strip_forced_spans", "strip_regular_samples", "strip_samples", "strip_spans"):
                        setattr(w, step, True)
                        body = self._payload(w)
                        if len(body) <= MAX_BODY_BYTES:
                            break
                    if len(body) > MAX_BODY_BYTES:
                        self._finish_window(w, dropped=True)
                        continue
                timeout = self.timeout if deadline is None else max(0.5, min(self.timeout, deadline - time.monotonic()))
                status, headers, data, _ = http_post(self.endpoint, self.api_key, body, timeout)
                self.last_status = status
                if status is not None and 200 <= status < 300:
                    self.sent_windows += 1
                    self.sent_requests += w.total
                    self._finish_window(w)
                    continue
                if status == 400:
                    logger.debug("insight_sdk: server rejected a performance window as malformed (400); dropped")
                    self._finish_window(w, dropped=True)
                    continue
                if status in (401, 403):
                    with self._cv:
                        self.active = False
                        self._drop_all_locked()
                    self._log_once(
                        "auth",
                        "insight_sdk: the server answered %d to performance metrics (%s); performance monitoring is "
                        "disabled for this process (error reporting is unaffected)."
                        % (status, "invalid or expired API key" if status == 401 else "API key lacks write permission"),
                    )
                    return True
                if status == 413:
                    if w.strip_samples:
                        self._finish_window(w, dropped=True)
                    elif w.forced and not w.strip_regular_samples:
                        w.strip_regular_samples = True  # keep the forced samples (sampled traces) as long as possible
                        w.strip_sample_spans = True
                    else:
                        w.strip_samples = True  # keep the counts, drop the samples, resend now
                    continue
                if status == 429:
                    retry_after = parse_retry_after(headers.get("retry-after"))
                    if _error_code(data) == "quota_exceeded":
                        pause = min(retry_after if retry_after is not None else MAX_QUOTA_PAUSE, MAX_QUOTA_PAUSE)
                        with self._cv:
                            self.paused_until = time.monotonic() + pause
                            self._drop_all_locked()
                        self._log_once(
                            "quota",
                            "insight_sdk: monthly performance quota (tracked requests) exceeded; pausing performance "
                            "metrics for %d s and dropping them (error reporting is unaffected)." % int(pause),
                        )
                        return True
                    delay = min(retry_after if retry_after is not None else 5.0, MAX_RETRY_AFTER)
                    self._rl_until = self._next_send = time.monotonic() + delay
                    continue  # waits for the Retry-After (flush: if the deadline allows), then retries this window
                if status is not None and 400 <= status < 500 and status != 408:
                    self._log_once("http%d" % status, "insight_sdk: unexpected HTTP %d from %s" % (status, self.endpoint))
                    self._finish_window(w, dropped=True)
                    continue
                if status is not None and 300 <= status < 400:
                    self._log_once("redirect", "insight_sdk: %s redirects (HTTP %d); check host/endpoint" % (self.endpoint, status))
                    self._finish_window(w, dropped=True)
                    continue
                # 5xx / 408 / network error: keep the window, try again at the next flush
                self._next_send = time.monotonic() + self.flush_interval
                logger.debug("insight_sdk: performance window kept for the next flush (status=%r)", status)
                return False
        finally:
            self._send_lock.release()

    def _finish_window(self, w: Window, dropped: bool = False) -> None:
        with self._cv:
            if self._pending and self._pending[0] is w:
                self._pending.popleft()
            if dropped:
                self.dropped_requests += w.total

    # -- public --------------------------------------------------------------------------------------------------
    def flush(self, timeout: float = 2.0) -> bool:
        """Send everything recorded so far (blocks up to ``timeout``). True if nothing is left unsent."""
        try:
            deadline = time.monotonic() + timeout
            with self._cv:
                if self._pid != os.getpid():
                    self._reset_after_fork()
                self._rotate_locked(time.monotonic())
                if not self._pending:
                    return True
            return self._send_pending(deadline)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("insight_sdk: performance flush failed: %r", e)
            return False

    def close(self, timeout: float = 2.0) -> None:
        self.flush(timeout)
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        try:
            _monitors.remove(self)
        except ValueError:
            pass

    def buffered_windows(self) -> int:
        with self._cv:
            return len(self._pending)


# -- transactions -----------------------------------------------------------------------------------------------
def current_monitor() -> Optional[PerformanceMonitor]:
    """The active monitor of the global client, or None (performance off, client disabled, key rejected)."""
    from . import get_client

    client = get_client()
    monitor = getattr(client, "performance", None)
    return monitor if monitor is not None and monitor.active else None


def _norm_route(route: Optional[str]) -> Optional[str]:
    if not route:
        return None
    route = str(route)
    return route if route.startswith("/") else "/" + route


class Transaction:
    """One request / task / custom block being timed. ``finish()`` records it (idempotent)."""

    __slots__ = (
        "monitor", "name", "op", "method", "route", "status", "failed", "t0", "wall0", "done", "ignored",
        "trace_id", "spans", "span_extra", "max_spans", "span_id", "parent_span_id", "sampled",
    )

    def __init__(
        self,
        monitor: Optional[PerformanceMonitor],
        name: Optional[str] = None,
        op: str = "http.server",
        method: Optional[str] = None,
        ignored: bool = False,
    ) -> None:
        self.monitor = monitor
        self.name = name  # fixed name (jobs, custom); web transactions derive it from method + route
        self.op = op
        self.method = (method or "").upper() or None
        self.route: Optional[str] = None
        self.status: Optional[int] = None
        self.failed = False
        self.ignored = ignored
        self.done = ignored
        self.t0 = time.perf_counter()
        self.wall0 = time.time()
        # trace id (32 hex): the sample and every error event captured while the transaction is active carry it
        self.trace_id: Optional[str] = os.urandom(16).hex() if monitor is not None and not ignored else None
        # this request's root span (16 hex); parent_span_id / sampled come from an incoming ``traceparent`` (see adopt)
        self.span_id: Optional[str] = new_span_id() if self.trace_id is not None else None
        self.parent_span_id: Optional[str] = None
        self.sampled = False
        self.spans: List[Any] = []
        self.span_extra: Dict[Tuple[str, str], _Stat] = {}
        self.max_spans = monitor.max_spans if monitor is not None else 0

    def adopt(self, ctx: Any) -> None:
        """Continue the trace of an incoming ``traceparent`` (a ``tracing.TraceContext``): same trace id, its span is our parent."""
        if ctx is not None and self.trace_id is not None:
            self.trace_id = ctx.trace_id
            self.parent_span_id = ctx.parent_span_id
            self.sampled = bool(ctx.sampled)

    def add_span(self, op: str, description: str, start_ms: float, duration_ms: float, ok: bool, data: Optional[Dict[str, Any]], span_id: Optional[str] = None) -> None:
        """Collect a span (max ``max_spans``); beyond that it is only counted in the aggregates. A span whose id was sent
        to another service is always collected (up to 50 beyond the cap) so the two halves of the trace can be joined."""
        if len(self.spans) < self.max_spans or (span_id and len(self.spans) < self.max_spans + 50):
            self.spans.append((op, description, start_ms, duration_ms, ok, data, span_id))
            return
        key = (op, description)
        st = self.span_extra.get(key)
        if st is None:
            if len(self.span_extra) >= 100:
                key = (op, OTHER)
                st = self.span_extra.get(key)
            if st is None:
                st = self.span_extra[key] = _Stat(op)
        st.count += 1
        if not ok:
            st.errors += 1
        st.sum += duration_ms
        st.min = min(st.min, duration_ms)
        st.max = max(st.max, duration_ms)
        st.buckets[bucket_index(duration_ms)] += 1

    def set_route(self, route: Optional[str]) -> None:
        route = _norm_route(route)
        if route and self.route is None:  # the innermost layer (the framework) knows the template first: first one wins
            self.route = route

    def set_status(self, status: Any) -> None:
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            self.status = status

    def set_error(self) -> None:
        self.failed = True

    def finish(self, status: Any = None, failed: Optional[bool] = None) -> None:
        if self.done:
            return
        self.done = True
        try:
            if status is not None:
                self.set_status(status)
            if failed:
                self.failed = True
            monitor = self.monitor
            if monitor is None:
                return
            duration_ms = (time.perf_counter() - self.t0) * 1000.0
            ok = not (self.status >= 500 if self.status is not None else self.failed)
            status_code = self.status if self.status is not None else (500 if self.failed and self.op == "http.server" else None)
            name = self.name or "%s %s" % (self.method or "?", self.route or UNMATCHED)
            monitor.record(
                name, self.op, duration_ms, status_code, ok, self.wall0, self.trace_id,
                list(self.spans) if self.spans else None, self.span_extra or None,
                self.span_id, self.parent_span_id, self.sampled,
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("insight_sdk: transaction finish failed: %r", e)


def _adopt_trace(txn: Transaction, traceparent: Any) -> None:
    """Continue the incoming trace (``traceparent`` header value, else a trace joined with ``continue_trace``)."""
    try:
        from .tracing import joined_trace

        ctx = parse_traceparent(traceparent) if traceparent else None
        txn.adopt(ctx if ctx is not None else joined_trace())
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("insight_sdk: adopt trace failed: %r", e)


def begin_web_transaction(scope: Any, method: Optional[str], url_or_path: Optional[str], route: Optional[str] = None, traceparent: Any = None) -> Optional[Transaction]:
    """Start the request transaction on ``scope`` unless one is already running (layered middleware) or metrics are off.
    Returns the transaction the *caller owns* (it must ``finish()`` it), or None."""
    try:
        monitor = current_monitor()
        if monitor is None or getattr(scope, "txn", None) is not None:
            return None
        if monitor.is_ignored(path_of(url_or_path)):
            txn = Transaction(None, method=method, ignored=True)  # marks the request so inner layers skip it too
        else:
            txn = Transaction(monitor, method=method)
            _adopt_trace(txn, traceparent)
            if route:
                txn.set_route(route)
        scope.txn = txn
        return txn
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("insight_sdk: begin transaction failed: %r", e)
        return None


def begin_job_transaction(scope: Any, name: str, traceparent: Any = None) -> Optional[Transaction]:
    """One transaction per task execution (always its own, even inside a request, e.g. eager Celery)."""
    try:
        monitor = current_monitor()
        if monitor is None:
            return None
        txn = Transaction(monitor, name="TASK %s" % (name or "?"), op="queue.task")
        _adopt_trace(txn, traceparent)
        scope.txn = txn
        return txn
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("insight_sdk: begin job transaction failed: %r", e)
        return None


class start_transaction:
    """Time your own code (management commands, scripts, scheduled jobs)::

        with insight_sdk.start_transaction("import_catalog", op="script"):
            ...

        @insight_sdk.start_transaction("nightly_report", op="cron")
        def report(): ...

    An exception that propagates counts as an error. The name is used as given: never put ids in it.
    """

    def __init__(self, name: str, op: str = "custom") -> None:
        self.name = name
        self.op = op
        self.transaction: Optional[Transaction] = None
        self._scope: Any = None
        self._prev: Any = None

    def __enter__(self) -> "start_transaction":
        monitor = current_monitor()
        self.transaction = Transaction(monitor, name=self.name, op=self.op) if monitor is not None else None
        if self.transaction is not None:
            _adopt_trace(self.transaction, None)  # a trace joined with continue_trace()
        self._scope = None
        self._prev = None
        if self.transaction is not None:
            try:  # make it the current transaction so spans and error events inside the block attach to it
                from .scope import _writable_scope

                self._scope = _writable_scope()
                self._prev = self._scope.txn
                self._scope.txn = self.transaction
            except Exception:
                self._scope = None
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        t = self.transaction
        self.transaction = None
        if t is None:
            return None
        try:
            if self._scope is not None and self._scope.txn is t:
                self._scope.txn = self._prev
        except Exception:
            pass
        failed = False
        if exc_type is not None:
            if issubclass(exc_type, SystemExit):
                code = getattr(exc, "code", 0)
                failed = code not in (None, 0)
            else:
                failed = issubclass(exc_type, Exception)
        t.finish(failed=failed)
        return None

    def set_error(self) -> None:
        if self.transaction is not None:
            self.transaction.set_error()

    def __call__(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        name, op = self.name, self.op
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                with start_transaction(name, op):
                    return await fn(*args, **kwargs)

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with start_transaction(name, op):
                return fn(*args, **kwargs)

        return wrapper
