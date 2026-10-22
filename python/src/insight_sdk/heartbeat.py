"""Cron job (heartbeat) monitoring helpers.

    @insight_sdk.monitor("https://insight.example.com/api/ping/<token>/")
    def nightly_backup(): ...

    with insight_sdk.monitor("<token>"):     # a bare token needs init() so the host is known
        run_job()

``check_in(target, status, message)`` sends a single ping (``status`` is "success", "start", "fail" or "log").
Nothing here ever raises: a monitoring hiccup must not break the job it watches.
"""
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

_ACTION = re.compile(r"/(start|fail|log|\d{1,3})?/*$", re.I)
_TIMEOUT = 5.0


def _base(target: str) -> Optional[str]:
    t = (target or "").strip()
    if re.match(r"^https?://", t, re.I):
        return _ACTION.sub("", t).rstrip("/") + "/"
    from . import get_client  # late import: __init__ imports this module

    client = get_client()
    endpoint = getattr(client, "endpoint", None) if client else None
    if not endpoint or not t:
        return None
    root = re.sub(r"/ingest/events/?$", "", endpoint).rstrip("/")
    return "%s/ping/%s/" % (root, urllib.parse.quote(t, safe=""))


def check_in(target: str, status: str = "success", message: Optional[str] = None, timeout: float = _TIMEOUT) -> bool:
    """Send one ping. Returns True when the server accepted it."""
    try:
        base = _base(target)
        if not base:
            return False
        suffix = status + "/" if status in ("start", "fail", "log") else ""  # keep the slash: without it the server redirects and urllib on 3.9 will not follow a 308
        data = message.encode("utf-8")[:10000] if message else None
        req = urllib.request.Request(base + suffix, data=data, method="POST" if data else "GET", headers={"User-Agent": "insight-sdk-python-heartbeat"})
        from .spans import suppressed

        with suppressed(), urllib.request.urlopen(req, timeout=timeout) as res:
            return 200 <= res.status < 300
    except Exception:
        return False


class monitor:
    """Context manager and decorator: start ping, then success, or fail (with the traceback) when it raises."""

    def __init__(self, target: str) -> None:
        self.target = target

    def __enter__(self) -> "monitor":
        check_in(self.target, "start")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            check_in(self.target, "success")
        else:
            check_in(self.target, "fail", "".join(traceback.format_exception(exc_type, exc, tb)))
        return False  # never swallow the job's own exception

    def __call__(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        import functools

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with monitor(self.target):
                return fn(*args, **kwargs)

        return wrapper
