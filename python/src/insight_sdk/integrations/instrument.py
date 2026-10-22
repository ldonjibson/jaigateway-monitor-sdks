"""Automatic span instrumentation (outbound HTTP, database, cache). Internal; enabled by ``init()``.

========================  =========================================================  ==============================
Integration name          What is patched                                            Span
========================  =========================================================  ==============================
``urllib``                ``urllib.request.urlopen``                                 ``http.client``  ``GET host``
``requests``              ``requests.Session.send``                                 ``http.client``
``urllib3``               ``urllib3.HTTPConnectionPool.urlopen`` (not under requests) ``http.client``
``httpx``                 ``httpx.Client.send`` / ``AsyncClient.send``               ``http.client``
``aiohttp_client``        ``aiohttp.ClientSession._request``                         ``http.client``
``django_orm``            ``connection.execute_wrapper`` on every connection         ``db.query`` (SQL, no params)
``sqlalchemy``            Engine ``before/after_cursor_execute`` + ``handle_error``  ``db.query``
``redis``                 ``redis.Redis.execute_command`` (sync and asyncio)         ``cache``  command name only
========================  =========================================================  ==============================

Every library is imported lazily: when it is already in ``sys.modules`` it is patched at once, otherwise a post-import
hook patches it the moment the app imports it (nothing is ever imported by the SDK). Every patch is idempotent,
removable (``init()`` again / ``close()``) and wrapped so that an SDK bug can never break the host application.
When ``trace_propagation_targets`` is set, the HTTP client patches also add the W3C ``traceparent`` header (see ``tracing.py``).
Descriptions never contain URL paths, queries, credentials, SQL literals, bind parameters or cache keys/values.
"""
import functools
import importlib.abc
import importlib.util
import logging
import sys
import threading
import time
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..spans import host_of_url, record_span
from ..tracing import format_traceparent, new_span_id, outbound_context

logger = logging.getLogger("insight_sdk")

NAMES = ("urllib", "requests", "urllib3", "httpx", "aiohttp_client", "django_orm", "sqlalchemy", "redis")
_MARK = "__insight_span_wrapper__"
_lock = threading.RLock()
_undo: Dict[str, Callable[[], None]] = {}
_depth: "ContextVar[bool]" = ContextVar("insight_http_depth", default=False)


# -- patch helpers --------------------------------------------------------------------------------------------------
def _patch(obj: Any, name: str, make: Callable[[Any], Any]) -> Callable[[], None]:
    """Replace ``obj.name`` by ``make(original)``; no-op when already patched. Returns the undo function."""
    current = obj.__dict__.get(name) if hasattr(obj, "__dict__") else getattr(obj, name, None)
    if current is None:
        current = getattr(obj, name)
    if getattr(current, _MARK, False):
        return lambda: None
    wrapper = make(current)
    setattr(wrapper, _MARK, True)
    setattr(wrapper, "__insight_original__", current)
    setattr(obj, name, wrapper)

    def undo() -> None:
        try:
            if obj.__dict__.get(name) is wrapper:
                setattr(obj, name, current)
        except Exception:
            pass

    return undo


def _safe(fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as e:
        logger.debug("insight_sdk: span instrumentation error: %r", e)


def _http_span(t0: float, method: Any, host: str, status: Any, failed: bool, span_id: Optional[str] = None) -> None:
    def go() -> None:
        m = str(method or "GET").upper()[:10]
        code = status if isinstance(status, int) and not isinstance(status, bool) else None
        ok = not failed and not (code is not None and code >= 500)
        data: Dict[str, Any] = {"http.method": m}
        if host:
            data["host"] = host
        if code is not None:
            data["http.status_code"] = code
        record_span("http.client", "%s %s" % (m, host), t0, (time.perf_counter() - t0) * 1000.0, ok, data, span_id)

    _safe(go)


# -- outbound HTTP ----------------------------------------------------------------------------------------------------
_Inject = Callable[[Tuple[Any, ...], Dict[str, Any]], Optional[Tuple[Tuple[Any, ...], Dict[str, Any], str]]]


def _prepare(inject: Optional[_Inject], args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Tuple[Tuple[Any, ...], Dict[str, Any], Optional[str]]:
    """Add the ``traceparent`` header when the call goes to a propagation target (see ``tracing.outbound_context``).
    Returns the (possibly replaced) arguments and the span id used in the header (the same id the span is recorded with)."""
    if inject is None:
        return args, kwargs, None
    try:
        from ..performance import current_monitor

        monitor = current_monitor()
        if monitor is None or getattr(monitor, "propagation_match", None) is None:
            return args, kwargs, None
        prepared = inject(args, kwargs)
        if prepared is not None:
            return prepared
    except Exception as e:
        logger.debug("insight_sdk: traceparent injection failed: %r", e)
    return args, kwargs, None


def _header_for(host: str) -> Optional[Tuple[str, str]]:
    """(header value, span id) for a call to ``host``, or None when no header must be added."""
    ctx = outbound_context(host)
    if ctx is None:
        return None
    span_id = new_span_id()
    return format_traceparent(ctx[0], span_id, ctx[1]), span_id


def _has_header(headers: Any) -> bool:
    """Case-insensitive: did the app set a ``traceparent`` itself?"""
    try:
        for k in headers.keys() if hasattr(headers, "keys") else [h[0] for h in headers]:
            if str(k.decode("latin-1") if isinstance(k, bytes) else k).lower() == "traceparent":
                return True
    except Exception:
        pass
    return False


def _sync_http(orig: Callable[..., Any], describe: Callable[..., Tuple[Any, str, Any]], set_depth: bool = False,
               skip_nested: bool = False, inject: Optional[_Inject] = None) -> Callable[..., Any]:
    """Wrap a sync call. ``describe(args, kwargs, result_or_exc, is_exc) -> (method, host, status)``."""

    @functools.wraps(orig)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if skip_nested and _depth.get():
            return orig(*args, **kwargs)
        t0 = time.perf_counter()
        args, kwargs, span_id = _prepare(inject, args, kwargs)
        token = _depth.set(True) if set_depth else None
        try:
            result = orig(*args, **kwargs)
        except BaseException as exc:
            _reset(token)
            _finish(describe, args, kwargs, exc, True, t0, span_id)
            raise
        _reset(token)
        _finish(describe, args, kwargs, result, False, t0, span_id)
        return result

    return wrapper


def _reset(token: Any) -> None:
    if token is not None:
        try:
            _depth.reset(token)
        except Exception:
            pass


def _finish(describe: Callable[..., Any], args: Any, kwargs: Any, outcome: Any, is_exc: bool, t0: float, span_id: Optional[str] = None) -> None:
    def go() -> None:
        method, host, status = describe(args, kwargs, outcome, is_exc)
        _http_span(t0, method, host, status, is_exc and not isinstance(status, int), span_id)

    _safe(go)


def _async_http(orig: Callable[..., Any], describe: Callable[..., Tuple[Any, str, Any]], inject: Optional[_Inject] = None) -> Callable[..., Any]:
    @functools.wraps(orig)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        args, kwargs, span_id = _prepare(inject, args, kwargs)
        try:
            result = await orig(*args, **kwargs)
        except BaseException as exc:
            _finish(describe, args, kwargs, exc, True, t0, span_id)
            raise
        _finish(describe, args, kwargs, result, False, t0, span_id)
        return result

    return wrapper


# injectors: (args, kwargs) -> (args, kwargs, span_id) after adding the header, or None
def _inject_request_object(request_of: Callable[[Tuple[Any, ...], Dict[str, Any]], Any], host_of: Callable[[Any], str]) -> _Inject:
    """requests / httpx: the prepared request object carries a mutable, case-insensitive ``headers``."""

    def inject(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Optional[Tuple[Tuple[Any, ...], Dict[str, Any], str]]:
        req = request_of(args, kwargs)
        if req is None or _has_header(req.headers):
            return None
        found = _header_for(host_of(req))
        if found is None:
            return None
        req.headers["traceparent"] = found[0]
        return args, kwargs, found[1]

    return inject


def _inject_urllib(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Optional[Tuple[Tuple[Any, ...], Dict[str, Any], str]]:
    import urllib.request as ur

    target = args[0] if args else kwargs.get("url")
    if hasattr(target, "full_url"):
        host = host_of_url(target.full_url)
        if target.has_header("Traceparent") or any(str(k).lower() == "traceparent" for k in list(target.headers) + list(target.unredirected_hdrs)):
            return None
        found = _header_for(host)
        if found is None:
            return None
        target.add_header("Traceparent", found[0])
        return args, kwargs, found[1]
    if not isinstance(target, str):
        return None
    found = _header_for(host_of_url(target))
    if found is None:
        return None
    req = ur.Request(target)
    req.add_header("Traceparent", found[0])
    if args:
        args = (req,) + tuple(args[1:])
    else:
        kwargs = dict(kwargs, url=req)
    return args, kwargs, found[1]


def _inject_urllib3(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Optional[Tuple[Tuple[Any, ...], Dict[str, Any], str]]:
    pool = args[0]
    scheme, port = getattr(pool, "scheme", "http"), getattr(pool, "port", None)
    host = host_of_url("%s://%s%s/" % (scheme, _bracket(getattr(pool, "host", "")), ":%s" % port if port else ""))
    in_args = len(args) > 4
    headers = args[4] if in_args else kwargs.get("headers")
    if headers is None:
        headers = getattr(pool, "headers", None)
    if headers is not None and _has_header(headers):
        return None
    found = _header_for(host)
    if found is None:
        return None
    try:
        new = headers.copy() if headers is not None else {}
    except Exception:
        new = dict(headers)
    new["traceparent"] = found[0]
    if in_args:
        args = args[:4] + (new,) + tuple(args[5:])
    else:
        kwargs = dict(kwargs, headers=new)
    return args, kwargs, found[1]


def _inject_aiohttp(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Optional[Tuple[Tuple[Any, ...], Dict[str, Any], str]]:
    host = _aiohttp_describe(args, kwargs, None, False)[1]
    session = args[0]
    if _has_header(getattr(session, "_default_headers", None) or {}):
        return None
    headers = kwargs.get("headers")
    if headers is not None and _has_header(headers):
        return None
    found = _header_for(host)
    if found is None:
        return None
    if headers is None:
        new: Any = {}
    else:
        try:
            new = headers.copy()
        except Exception:
            new = dict(headers)
    new["traceparent"] = found[0]
    return args, dict(kwargs, headers=new), found[1]


def _urllib_describe(args: Any, kwargs: Any, out: Any, is_exc: bool) -> Tuple[Any, str, Any]:
    target = args[0] if args else kwargs.get("url")
    if hasattr(target, "full_url"):
        method, url = target.get_method(), target.full_url
    else:
        method = "POST" if (args[1:2] and args[1] is not None) or kwargs.get("data") is not None else "GET"
        url = target
    status = getattr(out, "code", None) if is_exc else getattr(out, "status", None) or getattr(out, "code", None)
    return method, host_of_url(url), status


def _install_urllib() -> Callable[[], None]:
    import urllib.request as ur

    return _patch(ur, "urlopen", lambda o: _sync_http(o, _urllib_describe, inject=_inject_urllib))


def _requests_describe(args: Any, kwargs: Any, out: Any, is_exc: bool) -> Tuple[Any, str, Any]:
    req = args[1] if len(args) > 1 else kwargs.get("request")
    return getattr(req, "method", None), host_of_url(getattr(req, "url", "")), None if is_exc else getattr(out, "status_code", None)


_inject_requests = _inject_request_object(
    lambda args, kwargs: args[1] if len(args) > 1 else kwargs.get("request"), lambda req: host_of_url(getattr(req, "url", ""))
)
_inject_httpx = _inject_request_object(
    lambda args, kwargs: args[1] if len(args) > 1 else kwargs.get("request"), lambda req: host_of_url(str(getattr(req, "url", "")))
)


def _install_requests() -> Callable[[], None]:
    import requests

    return _patch(requests.Session, "send", lambda o: _sync_http(o, _requests_describe, set_depth=True, inject=_inject_requests))


def _urllib3_describe(args: Any, kwargs: Any, out: Any, is_exc: bool) -> Tuple[Any, str, Any]:
    pool = args[0]
    method = args[1] if len(args) > 1 else kwargs.get("method")
    scheme, port = getattr(pool, "scheme", "http"), getattr(pool, "port", None)
    host = host_of_url("%s://%s%s/" % (scheme, _bracket(getattr(pool, "host", "")), ":%s" % port if port else ""))
    return method, host, None if is_exc else getattr(out, "status", None)


def _bracket(host: str) -> str:
    return "[%s]" % host if ":" in (host or "") else (host or "")


def _install_urllib3() -> Callable[[], None]:
    import urllib3

    return _patch(urllib3.HTTPConnectionPool, "urlopen", lambda o: _sync_http(o, _urllib3_describe, set_depth=True, skip_nested=True, inject=_inject_urllib3))


def _httpx_describe(args: Any, kwargs: Any, out: Any, is_exc: bool) -> Tuple[Any, str, Any]:
    req = args[1] if len(args) > 1 else kwargs.get("request")
    return getattr(req, "method", None), host_of_url(str(getattr(req, "url", ""))), None if is_exc else getattr(out, "status_code", None)


def _install_httpx() -> Callable[[], None]:
    import httpx

    u1 = _patch(httpx.Client, "send", lambda o: _sync_http(o, _httpx_describe, inject=_inject_httpx))
    u2 = _patch(httpx.AsyncClient, "send", lambda o: _async_http(o, _httpx_describe, _inject_httpx))
    return lambda: (u1(), u2())[0]


def _aiohttp_describe(args: Any, kwargs: Any, out: Any, is_exc: bool) -> Tuple[Any, str, Any]:
    method = args[1] if len(args) > 1 else kwargs.get("method")
    url = args[2] if len(args) > 2 else kwargs.get("str_or_url")
    session = args[0]
    base = getattr(session, "_base_url", None)
    text = str(url)
    if base is not None and "://" not in text:
        text = str(base)
    return method, host_of_url(text), None if is_exc else getattr(out, "status", None)


def _install_aiohttp_client() -> Callable[[], None]:
    import aiohttp

    return _patch(aiohttp.ClientSession, "_request", lambda o: _async_http(o, _aiohttp_describe, _inject_aiohttp))


# -- databases --------------------------------------------------------------------------------------------------------
def _db_span(t0: float, sql: Any, system: Optional[str], ok: bool) -> None:
    def go() -> None:
        data = {"db.system": system} if system else None
        record_span("db.query", str(sql), t0, (time.perf_counter() - t0) * 1000.0, ok, data)

    _safe(go)


def _django_wrapper(execute: Callable[..., Any], sql: Any, params: Any, many: Any, context: Any) -> Any:
    t0 = time.perf_counter()
    try:
        result = execute(sql, params, many, context)
    except BaseException:
        _django_record(t0, sql, context, False)
        raise
    _django_record(t0, sql, context, True)
    return result


def _django_record(t0: float, sql: Any, context: Any, ok: bool) -> None:
    system = None
    try:
        system = getattr(context.get("connection"), "vendor", None)
    except Exception:
        pass
    _db_span(t0, sql, system, ok)


_django_wrapper.__insight_django__ = True  # type: ignore[attr-defined]


def _django_wrap_connection(connection: Any) -> None:
    wrappers = connection.execute_wrappers
    if not any(getattr(w, "__insight_django__", False) for w in wrappers):
        wrappers.append(_django_wrapper)


def _django_on_connection(sender: Any = None, connection: Any = None, **kwargs: Any) -> None:
    _safe(lambda: _django_wrap_connection(connection))


def ensure_django_orm() -> None:
    """Idempotently hook Django's ORM (also called by the Django middleware). Never raises."""
    try:
        with _lock:
            if "django_orm" not in _undo:
                _undo["django_orm"] = _install_django_orm()
    except Exception as e:
        logger.debug("insight_sdk: django orm spans unavailable: %r", e)


def _install_django_orm() -> Callable[[], None]:
    from django.db.backends.signals import connection_created

    uid = "insight_sdk.django_orm_spans"
    connection_created.connect(_django_on_connection, weak=False, dispatch_uid=uid)  # every new connection, every thread
    try:  # connections that already exist in this thread
        from django.db import connections

        for conn in connections.all():
            _safe(lambda c=conn: _django_wrap_connection(c))
    except Exception:
        pass

    def undo() -> None:
        try:
            connection_created.disconnect(dispatch_uid=uid)
            from django.db import connections

            for conn in connections.all():
                conn.execute_wrappers[:] = [w for w in conn.execute_wrappers if not getattr(w, "__insight_django__", False)]
        except Exception:
            pass

    return undo


def _install_sqlalchemy() -> Callable[[], None]:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    def before(conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any) -> None:
        try:
            context._insight_t0 = time.perf_counter()
        except Exception:
            pass

    def _system(conn: Any) -> Optional[str]:
        try:
            return str(conn.dialect.name)
        except Exception:
            return None

    def after(conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any) -> None:
        def go() -> None:
            t0 = getattr(context, "_insight_t0", None)
            if t0 is not None:
                context._insight_t0 = None
                _db_span(t0, statement, _system(conn), True)

        _safe(go)

    def on_error(exc_ctx: Any) -> None:
        def go() -> None:
            ctx = exc_ctx.execution_context
            t0 = getattr(ctx, "_insight_t0", None) if ctx is not None else None
            if t0 is not None and exc_ctx.statement:
                ctx._insight_t0 = None
                _db_span(t0, exc_ctx.statement, _system(exc_ctx.connection), False)

        _safe(go)

    event.listen(Engine, "before_cursor_execute", before)
    event.listen(Engine, "after_cursor_execute", after)
    event.listen(Engine, "handle_error", on_error)

    def undo() -> None:
        for name, fn in (("before_cursor_execute", before), ("after_cursor_execute", after), ("handle_error", on_error)):
            try:
                event.remove(Engine, name, fn)
            except Exception:
                pass

    return undo


# -- redis (command name only: never keys or values) ------------------------------------------------------------------
_NO_SPAN = {
    "BLPOP", "BRPOP", "BRPOPLPUSH", "BLMOVE", "BLMPOP", "BZPOPMIN", "BZPOPMAX", "BZMPOP", "SUBSCRIBE", "PSUBSCRIBE",
    "XREAD", "XREADGROUP", "WAIT", "MONITOR",
}


def _redis_cmd(args: Any) -> Optional[str]:
    first = args[1] if len(args) > 1 else None
    if isinstance(first, bytes):
        first = first.decode("ascii", "ignore")
    name = "".join(ch for ch in str(first or "")[:24] if ch.isalnum() or ch in "_.").upper()
    return name or None


def _redis_record(t0: float, args: Any, result: Any, ok: bool) -> None:
    def go() -> None:
        cmd = _redis_cmd(args)
        if cmd is None or cmd in _NO_SPAN:
            return
        data: Dict[str, Any] = {"db.system": "redis"}
        if ok and cmd in ("GET", "GETEX", "GETDEL", "HGET", "MGET") and not isinstance(result, (list, tuple)):
            data["cache.hit"] = result is not None
        record_span("cache", cmd, t0, (time.perf_counter() - t0) * 1000.0, ok, data)

    _safe(go)


def _redis_sync(orig: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(orig)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            result = orig(*args, **kwargs)
        except BaseException:
            _redis_record(t0, args, None, False)
            raise
        _redis_record(t0, args, result, True)
        return result

    return wrapper


def _redis_async(orig: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(orig)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            result = await orig(*args, **kwargs)
        except BaseException:
            _redis_record(t0, args, None, False)
            raise
        _redis_record(t0, args, result, True)
        return result

    return wrapper


def _install_redis() -> Callable[[], None]:
    import redis

    undos = [_patch(redis.Redis, "execute_command", _redis_sync)]
    try:
        import redis.asyncio as aredis

        undos.append(_patch(aredis.Redis, "execute_command", _redis_async))
    except Exception:
        pass
    return lambda: [u() for u in undos] and None


# -- lazy enabling ------------------------------------------------------------------------------------------------------
_INSTALLERS: Dict[str, Tuple[Optional[str], Callable[[], Callable[[], None]]]] = {
    "urllib": (None, _install_urllib),
    "requests": ("requests", _install_requests),
    "urllib3": ("urllib3", _install_urllib3),
    "httpx": ("httpx", _install_httpx),
    "aiohttp_client": ("aiohttp", _install_aiohttp_client),
    "django_orm": ("django.db", _install_django_orm),
    "sqlalchemy": ("sqlalchemy", _install_sqlalchemy),
    "redis": ("redis", _install_redis),
}


class _Loader:
    """Wraps a loader to run a callback once the module finished executing."""

    def __init__(self, loader: Any, callback: Callable[[], None]) -> None:
        self._loader, self._callback = loader, callback

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)

    def create_module(self, spec: Any) -> Any:
        return self._loader.create_module(spec)

    def exec_module(self, module: Any) -> None:
        self._loader.exec_module(module)
        try:
            self._callback()
        except Exception:
            pass


class _ImportHook(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self.waiting: Dict[str, List[Callable[[], None]]] = {}
        self._busy: set = set()

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        try:
            cbs = self.waiting.get(fullname)
            if not cbs or fullname in self._busy:
                return None
            self._busy.add(fullname)
            try:
                spec = importlib.util.find_spec(fullname)
            finally:
                self._busy.discard(fullname)
            if spec is None or spec.loader is None:
                return None

            def fire(name: str = fullname) -> None:
                for cb in self.waiting.pop(name, []):
                    cb()

            spec.loader = _Loader(spec.loader, fire)
            return spec
        except Exception:
            return None


_hook = _ImportHook()


def _enable_one(name: str) -> None:
    module_name, installer = _INSTALLERS[name]

    def install() -> None:
        with _lock:
            if name in _undo:
                return
            try:
                _undo[name] = installer()
            except Exception as e:  # the library is missing or has another layout: never an error for the host
                logger.debug("insight_sdk: span instrumentation %s unavailable: %r", name, e)

    if module_name is None or module_name in sys.modules:
        install()
        return
    try:
        if importlib.util.find_spec(module_name.split(".")[0]) is None:
            return
    except Exception:
        return
    with _lock:
        if _hook not in sys.meta_path:
            sys.meta_path.insert(0, _hook)
        _hook.waiting.setdefault(module_name, []).append(install)


def enable(names: Optional[List[str]] = None) -> Callable[[], None]:
    """Instrument the given names (default: all whose library is present). Idempotent. Returns the undo function."""
    for name in names if names is not None else NAMES:
        if name in _INSTALLERS:
            try:
                _enable_one(name)
            except Exception as e:
                logger.debug("insight_sdk: span instrumentation %s failed: %r", name, e)
    return disable


def disable() -> None:
    """Remove every patch (used by ``Client.close`` and by tests)."""
    with _lock:
        for name, undo in list(_undo.items()):
            try:
                undo()
            except Exception:
                pass
        _undo.clear()
        _hook.waiting.clear()
        try:
            sys.meta_path.remove(_hook)
        except ValueError:
            pass


def active() -> List[str]:
    return sorted(_undo)
