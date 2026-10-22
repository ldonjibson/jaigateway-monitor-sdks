"""Scopes: per-request/per-task context (user, tags, extra, breadcrumbs, request).

The current scope lives in a ``contextvars.ContextVar``. A scope is owned by the
(thread, asyncio task) that created it; the first write from any other thread or task
forks a private copy, so concurrent requests never see each other's data.
"""
import sys
import threading
from collections import deque
from contextvars import ContextVar, Token
from typing import Any, Deque, Dict, List, Optional, Tuple

_MAX_TAGS = 50
_MAX_TAG_VALUE = 200

_current: "ContextVar[Optional[Scope]]" = ContextVar("insight_scope", default=None)


def _owner_key() -> Tuple[int, int]:
    task_id = 0
    asyncio = sys.modules.get("asyncio")
    if asyncio is not None:
        try:
            task = asyncio.current_task()
            task_id = id(task) if task is not None else 0
        except RuntimeError:  # no running loop
            pass
    return (threading.get_ident(), task_id)


class Scope:
    def __init__(self) -> None:
        self.user: Optional[Dict[str, Any]] = None
        self.tags: Dict[str, str] = {}
        self.extra: Dict[str, Any] = {}
        self.request: Optional[Dict[str, Any]] = None
        self.fingerprint: Optional[List[str]] = None
        self.level: Optional[str] = None
        self.breadcrumbs: Deque[Dict[str, Any]] = deque(maxlen=100)
        self._owner: Optional[Tuple[int, int]] = None
        # A shared scope belongs to one request/job and may be written from helper threads or child
        # tasks of that request (thread-pool dependencies, ``asyncio.gather`` children) without forking.
        self._shared = False
        # The performance transaction of the request/job this scope belongs to (see performance.py); inherited by forks
        # so layered middleware report one transaction per request.
        self.txn: Any = None

    # -- mutation -------------------------------------------------------------------
    def set_user(self, user: Optional[Dict[str, Any]]) -> None:
        self.user = dict(user) if user else None

    def set_tag(self, key: str, value: Any) -> None:
        if key in self.tags or len(self.tags) < _MAX_TAGS:
            self.tags[str(key)] = str(value)[:_MAX_TAG_VALUE]

    def set_tags(self, tags: Dict[str, Any]) -> None:
        for k, v in tags.items():
            self.set_tag(k, v)

    def set_extra(self, key: str, value: Any) -> None:
        self.extra[str(key)] = value

    def set_request(self, request: Optional[Dict[str, Any]]) -> None:
        self.request = request

    def set_fingerprint(self, fingerprint: Optional[List[str]]) -> None:
        self.fingerprint = list(fingerprint) if fingerprint else None

    def set_level(self, level: Optional[str]) -> None:
        self.level = level

    def clear(self) -> None:
        self.__init__()  # type: ignore[misc]

    def add_breadcrumb(self, crumb: Dict[str, Any], max_breadcrumbs: int) -> None:
        if max_breadcrumbs <= 0:
            return
        if self.breadcrumbs.maxlen != max_breadcrumbs:
            self.breadcrumbs = deque(self.breadcrumbs, maxlen=max_breadcrumbs)
        self.breadcrumbs.append(crumb)

    def fork(self) -> "Scope":
        s = Scope()
        s.user = dict(self.user) if self.user else None
        s.tags = dict(self.tags)
        s.extra = dict(self.extra)
        s.request = dict(self.request) if self.request else None
        s.fingerprint = list(self.fingerprint) if self.fingerprint else None
        s.level = self.level
        s.breadcrumbs = deque(self.breadcrumbs, maxlen=self.breadcrumbs.maxlen)
        s.txn = self.txn
        return s


def get_current_scope() -> Scope:
    """Read access: the active scope (an empty one if nothing was written yet)."""
    return _current.get() or Scope()


def _writable_scope() -> Scope:
    s = _current.get()
    key = _owner_key()
    if s is None or (s._owner != key and not s._shared):
        s = s.fork() if s is not None else Scope()
        s._owner = key
        _current.set(s)
    return s


class push_scope:
    """``with push_scope() as scope:`` - changes inside the block are discarded on exit.

    ``shared=True`` (used by integrations for one request/job) lets code running in helper threads or
    child tasks of that request write to the same scope instead of a private fork.
    """

    def __init__(self, shared: bool = False) -> None:
        self._token: Optional[Token] = None
        self._shared = shared

    def __enter__(self) -> Scope:
        parent = _current.get()
        s = parent.fork() if parent is not None else Scope()
        s._owner = _owner_key()
        s._shared = self._shared
        self._token = _current.set(s)
        return s

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._token is not None:
                _current.reset(self._token)
        except ValueError:  # exited from a different context (e.g. WSGI close() in another thread)
            _current.set(None)
        self._token = None


class configure_scope:
    """``with configure_scope() as scope:`` gives write access to the current scope."""

    def __enter__(self) -> Scope:
        return _writable_scope()

    def __exit__(self, *exc: Any) -> None:
        return None
