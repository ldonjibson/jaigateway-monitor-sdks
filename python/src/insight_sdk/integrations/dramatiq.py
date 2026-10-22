"""Dramatiq.

    import insight_sdk
    from insight_sdk.integrations.dramatiq import InsightMiddleware
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); broker.add_middleware(InsightMiddleware())

Every message runs in its own scope tagged ``framework=dramatiq``, ``dramatiq_actor``, ``dramatiq_message_id``,
``dramatiq_queue`` and ``dramatiq_retries`` (message arguments are never sent). Only the *final* failure of a message
is reported: attempts that Dramatiq's ``Retries`` middleware will retry only leave a breadcrumb, and exceptions listed
in the actor's ``throws=`` option are ignored. Never raises into the worker.
"""
import logging
from typing import Any, Dict, Optional

from dramatiq.middleware import Middleware

from ._common import begin_job, end_request, framework_version, report

logger = logging.getLogger("insight_sdk")


def _message_tags(message: Any) -> Dict[str, Any]:
    return {
        "dramatiq_actor": getattr(message, "actor_name", None),
        "dramatiq_message_id": getattr(message, "message_id", None),
        "dramatiq_queue": getattr(message, "queue_name", None),
        "dramatiq_retries": (getattr(message, "options", None) or {}).get("retries", 0),
    }


def _actor_options(broker: Any, message: Any) -> Dict[str, Any]:
    try:
        return dict(broker.get_actor(message.actor_name).options)
    except Exception:
        return {}


def is_expected(broker: Any, message: Any, exception: BaseException) -> bool:
    """Exceptions the actor declares with ``throws=`` are part of its contract, not failures."""
    throws = (message.options or {}).get("throws") or _actor_options(broker, message).get("throws")
    return bool(throws) and isinstance(exception, throws)


def will_retry(broker: Any, message: Any, exception: BaseException, retries_before: int) -> bool:
    """Mirror of ``dramatiq.middleware.Retries``: will this failed attempt be enqueued again?"""
    from dramatiq.middleware import Retries

    retries_mw = None
    for mw in getattr(broker, "middleware", []):
        if isinstance(mw, Retries):
            retries_mw = mw
    if retries_mw is None or is_expected(broker, message, exception):
        return False
    actor_options = _actor_options(broker, message)
    max_retries = (message.options or {}).get("max_retries") or actor_options.get("max_retries", retries_mw.max_retries)
    retry_when = actor_options.get("retry_when", retries_mw.retry_when)
    if retry_when is not None:
        return bool(retry_when(retries_before, exception))
    return max_retries is None or retries_before < max_retries


class InsightMiddleware(Middleware):
    """Add with ``broker.add_middleware(InsightMiddleware())``."""

    def __init__(self) -> None:
        self._active: Dict[str, Any] = {}
        self._retries_before: Dict[str, int] = {}
        self._version: Optional[str] = framework_version("dramatiq")

    def before_process_message(self, broker: Any, message: Any) -> None:
        try:
            tags = _message_tags(message)
            self._retries_before[message.message_id] = int((message.options or {}).get("retries", 0) or 0)
            self._active[message.message_id] = begin_job(
                "dramatiq", tags, "dramatiq actor %s" % message.actor_name, self._version, task=message.actor_name,
                traceparent=(message.options or {}).get("traceparent"),
            )
        except Exception as exc:
            logger.debug("insight_sdk: dramatiq before failed: %r", exc)

    def after_process_message(self, broker: Any, message: Any, *, result: Any = None, exception: Any = None) -> None:
        try:
            cm = self._active.pop(message.message_id, None)
            before = self._retries_before.pop(message.message_id, 0)
            if exception is not None:
                scope = getattr(cm, "scope", None)
                if is_expected(broker, message, exception):
                    pass
                elif will_retry(broker, message, exception, before):
                    from .. import add_breadcrumb

                    add_breadcrumb(
                        "dramatiq actor %s failed, will retry: %r" % (message.actor_name, exception), level="warning"
                    )
                else:
                    report(exception, scope=scope, tags=dict(_message_tags(message), framework="dramatiq"))
            end_request(cm)
        except Exception as exc:
            logger.debug("insight_sdk: dramatiq after failed: %r", exc)

    def after_skip_message(self, broker: Any, message: Any) -> None:
        self._retries_before.pop(message.message_id, None)
        end_request(self._active.pop(message.message_id, None))


def install(broker: Any = None) -> InsightMiddleware:
    """``install(broker)`` == ``broker.add_middleware(InsightMiddleware())`` (default broker when omitted)."""
    import dramatiq

    broker = broker or dramatiq.get_broker()
    middleware = InsightMiddleware()
    broker.add_middleware(middleware)
    return middleware
