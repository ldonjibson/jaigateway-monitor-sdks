"""stdlib logging: ERROR+ records become events, INFO/WARNING records become breadcrumbs."""
import logging
import threading
from typing import Any, Callable

_LEVELS = {
    logging.CRITICAL: "fatal",
    logging.ERROR: "error",
    logging.WARNING: "warning",
    logging.INFO: "info",
    logging.DEBUG: "debug",
}
_guard = threading.local()


def _level_name(levelno: int) -> str:
    for threshold in sorted(_LEVELS, reverse=True):
        if levelno >= threshold:
            return _LEVELS[threshold]
    return "debug"


class InsightLoggingHandler(logging.Handler):
    def __init__(self, event_level: int = logging.ERROR, breadcrumb_level: int = logging.INFO) -> None:
        super().__init__(level=logging.NOTSET)
        self.event_level = event_level
        self.breadcrumb_level = breadcrumb_level

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("insight_sdk") or getattr(_guard, "busy", False):
            return  # never report the SDK's own logging (recursion guard)
        _guard.busy = True
        try:
            from .. import add_breadcrumb, capture_exception, capture_message

            if record.levelno >= self.event_level:
                level = _level_name(record.levelno)
                ctx: Any = {
                    "level": level,
                    "tags": {"logger": record.name},
                    "extra": {"log_message": record.getMessage()[:2000], "logger": record.name},
                }
                if record.exc_info and record.exc_info[1] is not None:
                    capture_exception(record.exc_info, **ctx)
                else:
                    capture_message(record.getMessage(), **ctx)
            elif record.levelno >= self.breadcrumb_level:
                add_breadcrumb(
                    message=record.getMessage()[:1000],
                    category="log",
                    level=_level_name(record.levelno),
                    data={"logger": record.name},
                )
        except Exception:
            pass
        finally:
            _guard.busy = False


def setup(client: Any) -> Callable[[], None]:
    root = logging.getLogger()
    handler = InsightLoggingHandler()
    root.addHandler(handler)
    return lambda: root.removeHandler(handler)
