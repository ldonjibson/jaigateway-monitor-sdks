"""Building wire-format pieces: stack frames, exception chains, sanitised request data."""
import json
import os
import platform
import re
import sys
import sysconfig
import traceback
from datetime import datetime, timezone
from types import TracebackType
from urllib.parse import unquote_plus
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ExcInfo = Tuple[Any, Any, Optional[TracebackType]]

MAX_FRAMES = 250
MAX_EXTRA_BYTES = 16000
MAX_TRACEBACK_CHARS = 30000
SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}

_STDLIB_PATHS = tuple(
    os.path.normcase(p)
    for p in {sysconfig.get_paths().get("stdlib"), sysconfig.get_paths().get("platstdlib")}
    if p
)
_SELF_DIR = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))


def utcnow_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def is_in_app(filename: str, module: Optional[str], include: Sequence[str], exclude: Sequence[str]) -> bool:
    if module:
        if any(module == p or module.startswith(p + ".") for p in include):
            return True
        if any(module == p or module.startswith(p + ".") for p in exclude):
            return False
    if not filename or filename.startswith("<") or filename.startswith("frozen "):
        return False
    norm = os.path.normcase(filename)
    if "site-packages" in norm or "dist-packages" in norm:
        return False
    if norm.startswith(_SELF_DIR):
        return False
    if any(norm.startswith(p + os.sep) for p in _STDLIB_PATHS):
        return False
    return True


def frames_from_traceback(
    tb: Optional[TracebackType], include: Sequence[str] = (), exclude: Sequence[str] = ()
) -> List[Dict[str, Any]]:
    """Frames MOST RECENT FIRST. Local variables are never read."""
    frames: List[Dict[str, Any]] = []
    while tb is not None:
        frame = tb.tb_frame
        filename = frame.f_code.co_filename
        module = frame.f_globals.get("__name__")
        frames.append(
            {
                "filename": filename,
                "function": frame.f_code.co_name,
                "lineno": tb.tb_lineno,
                "module": module,
                "in_app": is_in_app(filename, module, include, exclude),
            }
        )
        tb = tb.tb_next
    frames.reverse()
    return frames[:MAX_FRAMES]


def exception_value(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:
        return "<unprintable %s>" % type(exc).__name__


def describe(type_name: str, value: str) -> str:
    text = "%s: %s" % (type_name, value) if value else type_name
    return text[:255]


def exception_chain(exc: BaseException, include: Sequence[str] = (), exclude: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """[main, cause/context, ...]; main (outermost, the one raised last) first."""
    chain: List[Dict[str, Any]] = []
    seen = set()
    current: Optional[BaseException] = exc
    relation = None
    while current is not None and id(current) not in seen and len(chain) < 10:
        seen.add(id(current))
        chain.append(
            {
                "type": type(current).__name__,
                "value": exception_value(current),
                "relation": relation,
                "stacktrace": frames_from_traceback(current.__traceback__, include, exclude),
            }
        )
        if current.__cause__ is not None:
            current, relation = current.__cause__, "cause"
        elif current.__context__ is not None and not current.__suppress_context__:
            current, relation = current.__context__, "context"
        else:
            current = None
    return chain


def format_traceback(exc_info: ExcInfo) -> str:
    try:
        text = "".join(traceback.format_exception(*exc_info))
    except Exception:
        return ""
    if len(text) > MAX_TRACEBACK_CHARS:
        text = "...[truncated]\n" + text[-MAX_TRACEBACK_CHARS:]
    return text


def jsonable(value: Any, depth: int = 0) -> Any:
    """Make arbitrary data safe to serialise: bounded depth, string length and size."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 2000 else value[:2000] + "...[truncated]"
    if depth >= 6:
        return repr(value)[:200]
    if isinstance(value, Mapping):
        return {str(k)[:200]: jsonable(v, depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, depth + 1) for v in list(value)[:100]]
    try:
        return repr(value)[:500]
    except Exception:
        return "<unrepresentable>"


def fit_extra(extra: Dict[str, Any]) -> Dict[str, Any]:
    """Keep serialised extra under the server's 16 KB limit by dropping the largest keys."""
    extra = {k: jsonable(v) for k, v in extra.items()}
    sizes = {k: len(json.dumps(v, default=str)) for k, v in extra.items()}
    if sum(sizes.values()) <= MAX_EXTRA_BYTES:
        return extra
    for key in sorted(sizes, key=lambda k: sizes[k], reverse=True):
        extra[key] = "<dropped: too large>"
        sizes[key] = 24
        if sum(sizes.values()) <= MAX_EXTRA_BYTES:
            break
    return extra


def scrub_headers(headers: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in headers:
        if str(k).lower() not in SENSITIVE_HEADERS:
            out[str(k).lower()] = str(v)[:1000]
    return out


_SECRET_QUERY_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[-_]?key|apikey|auth|credential|session|signature|jwt|bearer|private)", re.I
)


def scrub_query(query: Optional[str]) -> Optional[str]:
    """Mask the values of secret-looking query parameters (``token=...``, ``api_key=...``, ``password=...``)."""
    if not query:
        return query
    parts = []
    for part in query.split("&"):
        key, sep, _value = part.partition("=")
        if sep and _SECRET_QUERY_KEY.search(unquote_plus(key)):
            part = key + "=[Filtered]"
        parts.append(part)
    return "&".join(parts)


def strip_url_query(url: Optional[str]) -> Optional[str]:
    """URL without query string or fragment (secrets travel in query strings)."""
    return url.split("?", 1)[0].split("#", 1)[0] if url else url


def request_info(
    url: Optional[str], method: Optional[str], query: Optional[str], headers: Iterable[Tuple[str, str]]
) -> Dict[str, Any]:
    info: Dict[str, Any] = {"url": strip_url_query(url), "method": method, "headers": scrub_headers(headers)}
    query = scrub_query(query)
    if query:
        info["query"] = query
    return info


def environ_headers(environ: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """HTTP headers from a WSGI environ (HTTP_* plus content-type/length)."""
    out = []
    for k, v in environ.items():
        if k.startswith("HTTP_"):
            out.append((k[5:].replace("_", "-").lower(), v))
        elif k in ("CONTENT_TYPE", "CONTENT_LENGTH") and v:
            out.append((k.replace("_", "-").lower(), v))
    return out


def runtime_tags() -> Dict[str, str]:
    return {
        "runtime": "%s %s" % (platform.python_implementation(), platform.python_version()),
        "os": platform.system() or sys.platform,
    }


def runtime_extra() -> Dict[str, Any]:
    return {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
    }
