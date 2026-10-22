"""Huey.

    import insight_sdk
    from insight_sdk.integrations.huey import install
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); install(huey)

Hooks Huey's ``pre_execute``/``post_execute`` and ``SIGNAL_ERROR``: every task runs in its own scope tagged
``framework=huey``, ``huey_task`` (task name), ``huey_task_id`` and ``huey_retries``; a task that raises is reported
once (arguments are never sent). Attempts that Huey will retry (``retries=N``) only leave a breadcrumb; the final
failure is the event. Works with the consumer and with ``immediate=True``.
"""
import logging
from typing import Any, Dict

from ._common import begin_job, end_request, framework_version, report

logger = logging.getLogger("insight_sdk")


def _task_tags(task: Any) -> Dict[str, Any]:
    return {
        "huey_task": getattr(task, "name", None) or type(task).__name__,
        "huey_task_id": getattr(task, "id", None),
        "huey_retries": getattr(task, "retries", None),
    }


def install(huey: Any) -> Any:
    """Attach to a ``Huey`` instance (idempotent). Returns ``huey``."""
    from huey.signals import SIGNAL_ERROR

    if getattr(huey, "_insight_sdk_installed", False):
        return huey
    huey._insight_sdk_installed = True
    version = framework_version("huey")
    active: Dict[str, Any] = {}

    @huey.pre_execute()
    def insight_pre_execute(task: Any) -> None:
        try:
            tags = _task_tags(task)
            active[str(task.id)] = begin_job("huey", tags, "huey task %s" % tags["huey_task"], version, task=tags["huey_task"])
        except Exception as exc:
            logger.debug("insight_sdk: huey pre_execute failed: %r", exc)

    @huey.post_execute()
    def insight_post_execute(task: Any, task_value: Any, exception: Any) -> None:
        end_request(active.pop(str(getattr(task, "id", "")), None))

    @huey.signal(SIGNAL_ERROR)
    def insight_error(signal: Any, task: Any, exc: Any = None, *args: Any, **kwargs: Any) -> None:
        try:
            if exc is None:
                return
            if getattr(task, "retries", 0):
                from .. import add_breadcrumb

                add_breadcrumb("huey task %s failed, will retry: %r" % (_task_tags(task)["huey_task"], exc), level="warning")
                return
            cm = active.get(str(getattr(task, "id", "")))
            report(exc, scope=getattr(cm, "scope", None), tags=dict(
                {k: v for k, v in _task_tags(task).items() if v is not None}, framework="huey"))
        except Exception as e:
            logger.debug("insight_sdk: huey error hook failed: %r", e)

    return huey
