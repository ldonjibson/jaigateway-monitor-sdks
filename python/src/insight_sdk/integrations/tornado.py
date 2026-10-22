"""Tornado.

    import insight_sdk
    from insight_sdk.integrations.tornado import setup
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); setup()

``setup()`` wraps ``RequestHandler.log_exception`` (where Tornado reports every unhandled handler error) and
``RequestHandler._execute`` (for the per-request scope) once, process-wide, without subclassing. Prefer explicit
classes? Put ``InsightMixin`` first in your base handler's bases instead; both paths together still report once.

Each event carries request data (no secrets), ``framework`` tags, the ``route`` tag (the URL pattern with its groups
as ``{n}``/``{name}``, e.g. ``/items/{0}``) and an ``http`` breadcrumb. ``HTTPError`` below 500 and ``Finish`` are not
reported; Tornado's normal error page/response is unchanged.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from ..event import request_info
from ._common import begin_request, end_request, framework_version, report, set_route, set_status

logger = logging.getLogger("insight_sdk")
_PATCHED = "_insight_sdk_patched"
_ACTIVE = "_insight_sdk_cm"


def _request_data(handler: Any) -> Dict[str, Any]:
    req = handler.request
    return request_info("%s://%s%s" % (req.protocol, req.host, req.path), req.method, req.query, req.headers.get_all())


def regex_to_template(pattern: str) -> str:
    """``/items/(\\d+)/(?P<name>\\w+)$`` -> ``/items/{0}/{name}``."""
    out: List[str] = []
    depth = 0
    index = 0
    group_chars: List[str] = []
    i = 0
    pattern = pattern.lstrip("^")
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            if depth == 0:
                out.append(pattern[i + 1])
            else:
                group_chars.append(pattern[i:i + 2])
            i += 2
            continue
        if c == "(":
            if depth == 0:
                group_chars = []
            depth += 1
            if depth > 1:
                group_chars.append(c)
        elif c == ")":
            depth -= 1
            if depth == 0:
                body = "".join(group_chars)
                if body.startswith("?P<"):
                    out.append("{%s}" % body[3:body.index(">")])
                elif not body.startswith("?"):  # capturing group; (?:...) and lookarounds add nothing
                    out.append("{%d}" % index)
                    index += 1
                group_chars = []
                while i + 1 < len(pattern) and pattern[i + 1] in "?*+":  # quantifier of the group
                    i += 1
            else:
                group_chars.append(c)
        elif depth > 0:
            group_chars.append(c)
        elif c != "$":
            out.append(c)
        i += 1
    return "".join(out)


def _find_template(rules: Any, handler: Any, depth: int = 0) -> Optional[str]:
    if depth > 5:
        return None
    for rule in list(rules or []):
        matcher, target = rule.matcher, rule.target
        try:
            if matcher.match(handler.request) is None:
                continue
        except Exception:
            continue
        if target is type(handler):
            regex = getattr(matcher, "regex", None)
            return regex_to_template(regex.pattern) if regex is not None else None
        nested = getattr(target, "rules", None) or getattr(getattr(target, "default_router", None), "rules", None)
        if nested:
            found = _find_template(nested, handler, depth + 1)
            if found:
                return found
    return None


def route_template(handler: Any) -> Optional[str]:
    try:
        app = handler.application
        return _find_template(app.default_router.rules, handler)
    except Exception:
        return None


def _report(handler: Any, value: Any) -> None:
    try:
        tags = {"framework": "tornado"}
        version = framework_version("tornado")
        if version:
            tags["framework_version"] = version
        route = route_template(handler)
        scope = getattr(handler, _ACTIVE, None)
        scope = getattr(scope, "scope", None)
        if route:
            tags["route"] = route if route.startswith("/") else "/" + route
        if scope is not None:
            set_route(route, scope)
            report(value, scope=scope)
        else:
            report(value, request=_request_data(handler), tags=tags)
    except Exception as exc:
        logger.debug("insight_sdk: tornado report failed: %r", exc)


async def _scoped_execute(handler: Any, call: Any, transforms: Any, args: Any, kwargs: Any) -> Any:
    if getattr(handler, _ACTIVE, None) is not None:  # mixin + setup() both active
        return await call(transforms, *args, **kwargs)
    cm = None
    try:
        cm = begin_request("tornado", _request_data(handler), framework_version("tornado"), route=route_template(handler))
        setattr(handler, _ACTIVE, cm)
    except Exception:
        pass
    try:
        return await call(transforms, *args, **kwargs)
    finally:
        try:
            scope = getattr(cm, "scope", None)
            if scope is not None:
                set_status(handler.get_status(), scope)
                set_route(route_template(handler), scope)
        except Exception:
            pass
        end_request(cm)
        try:
            setattr(handler, _ACTIVE, None)
        except Exception:
            pass


class InsightMixin:
    """``class Base(InsightMixin, tornado.web.RequestHandler)``: reports errors seen by ``log_exception``."""

    def log_exception(self, typ: Any, value: Any, tb: Any) -> None:
        _report(self, value)
        super().log_exception(typ, value, tb)  # type: ignore[misc]

    async def _execute(self, transforms: Any, *args: Any, **kwargs: Any) -> Any:
        return await _scoped_execute(self, super()._execute, transforms, args, kwargs)  # type: ignore[misc]


def setup() -> None:
    """Wrap ``RequestHandler.log_exception`` / ``_execute`` once, process-wide."""
    from tornado.web import RequestHandler

    if RequestHandler.__dict__.get(_PATCHED):
        return
    original_log = RequestHandler.log_exception
    original_execute = RequestHandler._execute

    def log_exception(self: Any, typ: Any, value: Any, tb: Any) -> None:
        _report(self, value)
        original_log(self, typ, value, tb)

    async def _execute(self: Any, transforms: Any, *args: Any, **kwargs: Any) -> Any:
        return await _scoped_execute(self, lambda t, *a, **k: original_execute(self, t, *a, **k), transforms, args, kwargs)

    RequestHandler._insight_originals = (original_log, original_execute)  # type: ignore[attr-defined]
    RequestHandler.log_exception = log_exception  # type: ignore[method-assign]
    RequestHandler._execute = _execute  # type: ignore[method-assign]
    setattr(RequestHandler, _PATCHED, True)


def uninstall() -> None:
    """Undo ``setup()`` (used by tests)."""
    from tornado.web import RequestHandler

    originals: Optional[Tuple[Any, Any]] = RequestHandler.__dict__.get("_insight_originals")
    if originals:
        RequestHandler.log_exception, RequestHandler._execute = originals  # type: ignore[method-assign,assignment]
        delattr(RequestHandler, "_insight_originals")
        delattr(RequestHandler, _PATCHED)
