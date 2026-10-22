"""FastAPI.

    import insight_sdk
    from insight_sdk.integrations.fastapi import init_app
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); init_app(app)

What you get: unhandled exceptions are reported once (with the ``route`` tag set to the route template, e.g.
``/items/{id}``, plus ``framework``/``framework_version`` tags, request url/method/query/headers without secrets
and an ``http`` breadcrumb); ``HTTPException`` with status >= 500 is reported and 4xx (and validation errors) are
not. The scope is shared with dependencies, endpoints (async or threadpool) and background tasks, so
``insight_sdk.set_user(...)`` inside a dependency ends up on the event. FastAPI's own responses are unchanged.
"""
from typing import Any

from .starlette import init_app as _init_starlette_app


def init_app(app: Any) -> Any:
    """Add the Insight ASGI middleware and ``HTTPException`` handler to ``app`` (a ``FastAPI`` instance)."""
    return _init_starlette_app(app, framework="fastapi", module="fastapi")
