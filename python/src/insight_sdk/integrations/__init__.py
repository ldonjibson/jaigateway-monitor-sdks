"""Integrations. Each module is imported lazily so the base package never imports a framework."""
import importlib
import logging
import sys
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from ..client import Client

logger = logging.getLogger("insight_sdk")

# name -> (module, "is the framework in use?" module name or None for always)
AVAILABLE = {
    "logging": ("logging_integration", None),
    "excepthook": ("excepthook", None),
    "django": ("django", "django"),
    "flask": ("flask", "flask"),
    "celery": ("celery", "celery"),
    "rq": ("rq", "rq"),
}


def setup_integrations(client: "Client", names: Optional[Iterable[str]] = None) -> None:
    """``names=None`` enables every *automatic* integration whose framework is already imported by the app
    (see ``integrations.auto``); app-object based ones (FastAPI, Sanic, ...) need their explicit one-liner."""
    from . import auto, instrument

    explicit = names is not None
    if explicit:
        names = list(names)  # type: ignore[arg-type]
        span_names = [n for n in names if n in instrument.NAMES]
        names = [n for n in names if n not in instrument.NAMES]
    else:
        span_names = list(instrument.NAMES)
    try:  # spans: automatic DB / outbound HTTP / cache instrumentation (needs performance + performance_spans)
        if span_names and getattr(client, "performance", None) is not None and getattr(client, "performance_spans", False):
            client._span_undo = instrument.enable(span_names)
    except Exception as exc:
        logger.debug("insight_sdk: span instrumentation unavailable: %r", exc)
    enabled = []
    for name in names if explicit else auto.select():  # type: ignore[arg-type]
        if name not in AVAILABLE:
            if name in auto.EXPLICIT:
                logger.warning("insight_sdk: %r needs an app object, enable it with: %s", name, auto.EXPLICIT[name])
            else:
                logger.warning("insight_sdk: unknown integration %r", name)
            continue
        module_name, framework = AVAILABLE[name]
        if not explicit and framework is not None and framework not in sys.modules:
            continue
        try:
            module = importlib.import_module("%s.%s" % (__name__, module_name))
            undo = module.setup(client)
            if undo:
                client._teardowns.append(undo)
            enabled.append(name)
        except Exception as exc:  # a broken integration must never break init()
            logger.debug("insight_sdk: integration %s unavailable: %r", name, exc)
    if not explicit:
        auto.log_enabled(enabled)
