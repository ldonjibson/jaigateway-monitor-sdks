"""Automatic integration selection for ``init(integrations=None)``.

Only integrations that need **no app object** can be automatic. They are enabled when the framework module is
*already imported* (``sys.modules``); ``init()`` never imports a framework itself. One debug line (visible with
``init(debug=True)`` or the ``insight_sdk`` logger at DEBUG) lists what was enabled.

=========  ==========================================================  =====================================
Automatic  logging, excepthook, celery, rq, django (signal), flask (signal)
One line   fastapi, starlette, litestar, sanic, aiohttp, tornado, quart, pyramid, bottle, falcon, dramatiq, huey,
           django middleware (+ DRF handler), flask ``init_app``, WSGI/ASGI middleware
=========  ==========================================================  =====================================
"""
import logging
import sys
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger("insight_sdk")

# (integration name, module that must already be imported, or None for "always")
AUTOMATIC: Tuple[Tuple[str, Optional[str]], ...] = (
    ("logging", None),
    ("excepthook", None),
    ("django", "django"),
    ("flask", "flask"),
    ("celery", "celery"),
    ("rq", "rq"),
)

# What needs an explicit line, for documentation and ``insight-sdk`` diagnostics.
EXPLICIT: Dict[str, str] = {
    "fastapi": "insight_sdk.integrations.fastapi.init_app(app)",
    "starlette": "insight_sdk.integrations.starlette.init_app(app)",
    "litestar": "Litestar(..., plugins=[InsightPlugin()])",
    "sanic": "insight_sdk.integrations.sanic.init_app(app)",
    "aiohttp": "insight_sdk.integrations.aiohttp.setup(app)",
    "tornado": "insight_sdk.integrations.tornado.setup()",
    "quart": "insight_sdk.integrations.quart.init_app(app)",
    "pyramid": 'config.include("insight_sdk.integrations.pyramid")',
    "bottle": "app.install(InsightPlugin())",
    "falcon": "insight_sdk.integrations.falcon.init_app(app)",
    "dramatiq": "broker.add_middleware(InsightMiddleware())",
    "huey": "insight_sdk.integrations.huey.install(huey)",
}


def select(modules: Optional[Mapping[str, object]] = None) -> List[str]:
    """Names of the automatic integrations whose framework is already imported."""
    modules = sys.modules if modules is None else modules
    return [name for name, framework in AUTOMATIC if framework is None or framework in modules]


def log_enabled(enabled: Iterable[str]) -> None:
    logger.debug("insight_sdk: auto-enabled integrations: %s", ", ".join(enabled) or "(none)")
