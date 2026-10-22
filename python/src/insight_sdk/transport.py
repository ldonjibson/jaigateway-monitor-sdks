"""Background delivery: bounded queue, batching, retries, and the protocol's response handling."""
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.request
import weakref
from collections import deque
from email.utils import parsedate_to_datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

from .version import SDK_NAME, VERSION

logger = logging.getLogger("insight_sdk")

MAX_BODY_BYTES = 900_000  # server limit is 1 MB per request
MAX_QUOTA_PAUSE = 3600.0
MAX_RETRY_AFTER = 120.0
MAX_BACKOFF = 60.0

_transports: "weakref.WeakSet[Transport]" = weakref.WeakSet()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # surface 3xx as an error
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _after_fork_in_child() -> None:
    for t in list(_transports):
        t._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except Exception:
        return None


def _error_code(body: bytes) -> Optional[str]:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None
    if isinstance(data, dict):
        code = data.get("code")
        if code is None and isinstance(data.get("error"), dict):
            code = data["error"].get("code")
        return str(code) if code else None
    return None


class Transport:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        queue_size: int = 100,
        batch_size: int = 20,
        timeout: float = 10.0,
        max_attempts: int = 5,
        backoff_base: float = 1.0,
    ) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.queue_size = max(1, queue_size)
        self.batch_size = max(1, batch_size)
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base
        # state observable by callers/tests
        self.disabled = False
        self.paused_until = 0.0
        self.last_status: Optional[int] = None
        self.last_error: Optional[str] = None
        self.sent = 0
        self.dropped = 0
        self._logged: set = set()
        self._init_threading()
        _transports.add(self)

    def _init_threading(self) -> None:
        self._cv = threading.Condition()
        self._q: Deque[Dict[str, Any]] = deque()
        self._inflight = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pid = os.getpid()

    def _reset_after_fork(self) -> None:
        # The parent still owns the queued events and its worker thread; start clean here.
        self._init_threading()

    # -- public ---------------------------------------------------------------------
    def submit(self, event: Dict[str, Any]) -> bool:
        """Queue an event. Never blocks, never raises. Returns False if it was dropped."""
        try:
            if self._blocked():
                self.dropped += 1
                return False
            with self._cv:
                self._ensure_worker()
                if len(self._q) >= self.queue_size:
                    self._q.popleft()
                    self.dropped += 1
                self._q.append(event)
                self._cv.notify()
            return True
        except Exception:  # pragma: no cover - defensive
            return False

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until everything queued has been delivered or dropped. True if finished in time."""
        deadline = time.monotonic() + timeout
        with self._cv:
            if self._pid != os.getpid():
                self._reset_after_fork()
            if self._q:
                self._ensure_worker()
            while self._q or self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)
        return True

    def close(self, timeout: float = 2.0) -> None:
        self.flush(timeout)
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

    # -- worker ---------------------------------------------------------------------
    def _ensure_worker(self) -> None:  # call with self._cv held
        if self._pid != os.getpid():
            self._reset_after_fork()
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="insight-sdk-worker", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        cv, q = self._cv, self._q
        while True:
            with cv:
                while not q and not self._stop.is_set():
                    cv.wait()
                if self._stop.is_set():
                    return
                batch = [q.popleft() for _ in range(min(self.batch_size, len(q)))]
                self._inflight += len(batch)
            try:
                self._deliver(batch)
            except Exception as exc:  # never let the worker die
                self.dropped += len(batch)
                logger.debug("insight_sdk: delivery error: %r", exc)
            finally:
                with cv:
                    self._inflight -= len(batch)
                    cv.notify_all()

    def _blocked(self) -> bool:
        if self.disabled:
            return True
        if self.paused_until:
            if time.monotonic() < self.paused_until:
                return True
            self.paused_until = 0.0
            self._logged.discard("quota")
            logger.debug("insight_sdk: quota pause over, sending again")
        return False

    def _log_once(self, key: str, message: str) -> None:
        if key not in self._logged:
            self._logged.add(key)
            logger.warning(message)

    def _deliver(self, events: List[Dict[str, Any]]) -> None:
        if self._blocked():
            self.dropped += len(events)
            return
        body = json.dumps(events, default=str, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            self._split_or_drop(events)
            return
        attempt = 0
        while True:
            attempt += 1
            status, headers, resp = self._post(body)
            self.last_status = status
            if status is not None and 200 <= status < 300:
                self.sent += len(events)
                return
            delay: Optional[float] = None
            if status == 400:
                logger.debug("insight_sdk: server rejected %d event(s) as malformed (400)", len(events))
                self.dropped += len(events)
                return
            if status in (401, 403):
                self.disabled = True
                self._log_once(
                    "auth",
                    "insight_sdk: the server answered %d (%s); sending is disabled for this process."
                    % (status, "invalid or expired API key" if status == 401 else "API key lacks write permission"),
                )
                self.dropped += len(events)
                return
            if status == 413:
                self._split_or_drop(events)
                return
            if status == 429:
                retry_after = parse_retry_after(headers.get("retry-after"))
                if _error_code(resp) == "quota_exceeded":
                    pause = min(retry_after if retry_after is not None else MAX_QUOTA_PAUSE, MAX_QUOTA_PAUSE)
                    self.paused_until = time.monotonic() + pause
                    self._log_once(
                        "quota",
                        "insight_sdk: monthly event quota exceeded; pausing for %d s and dropping events."
                        % int(pause),
                    )
                    self.dropped += len(events)
                    return
                delay = min(retry_after if retry_after is not None else self._backoff(attempt), MAX_RETRY_AFTER)
            elif status is not None and 400 <= status < 500 and status != 408:
                self._log_once("http%d" % status, "insight_sdk: unexpected HTTP %d from %s" % (status, self.endpoint))
                self.dropped += len(events)
                return
            elif status is not None and 300 <= status < 400:
                self._log_once("redirect", "insight_sdk: %s redirects (HTTP %d); check host/endpoint" % (self.endpoint, status))
                self.dropped += len(events)
                return
            if attempt >= self.max_attempts:
                logger.debug("insight_sdk: giving up on %d event(s) after %d attempts", len(events), attempt)
                self.dropped += len(events)
                return
            if delay is None:
                delay = self._backoff(attempt)
            if self._stop.wait(delay):
                self.dropped += len(events)
                return

    def _backoff(self, attempt: int) -> float:
        base = min(MAX_BACKOFF, self.backoff_base * (2 ** (attempt - 1)))
        return base * random.uniform(0.5, 1.0)

    def _split_or_drop(self, events: List[Dict[str, Any]]) -> None:
        if len(events) <= 1:
            logger.debug("insight_sdk: dropping an event that is too large")
            self.dropped += len(events)
            return
        mid = len(events) // 2
        self._deliver(events[:mid])
        self._deliver(events[mid:])

    def _post(self, body: bytes) -> Tuple[Optional[int], Dict[str, str], bytes]:
        status, headers, data, error = http_post(self.endpoint, self.api_key, body, self.timeout)
        self.last_error = error
        return status, headers, data


def http_post(url: str, api_key: str, body: bytes, timeout: float) -> Tuple[Optional[int], Dict[str, str], bytes, Optional[str]]:
    """POST a JSON body. Returns ``(status or None, lower-cased headers, response bytes, error text or None)``; never raises."""
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "User-Agent": "%s/%s" % (SDK_NAME, VERSION),
        },
    )
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(65536), None
    except urllib.error.HTTPError as e:
        try:
            data = e.read(65536)
        except Exception:
            data = b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, data, "HTTP %d" % e.code
    except Exception as e:  # URLError, timeouts, connection resets, ...
        return None, {}, b"", repr(e)
