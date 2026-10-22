"""Celery: report failed tasks (arguments are never sent). Auto-enabled when Celery is imported.

    import insight_sdk
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com")   # in the worker process; nothing else

Every task runs in its own scope tagged ``framework=celery``, ``celery_task`` (name), ``celery_task_id``,
``celery_queue`` and ``celery_retries``. Only the *final* failure is reported (``task_failure``): attempts that are
retried (``self.retry()``/``autoretry_for``) only leave a breadcrumb, so a task retried 3 times and then failing is
one event, and a task that eventually succeeds is none. ``Retry``/``Ignore``/``Reject`` are never reported.
"""
from typing import Any, Dict, Optional

from ._common import begin_job, end_request, framework_version, report

_scopes: Dict[str, Any] = {}


def _task_tags(task: Any) -> Dict[str, Any]:
    request = getattr(task, "request", None)
    delivery = getattr(request, "delivery_info", None) or {}
    queue = delivery.get("routing_key") or delivery.get("queue") if isinstance(delivery, dict) else None
    return {
        "celery_task": getattr(task, "name", "?"),
        "celery_task_id": getattr(request, "id", None),
        "celery_queue": queue,
        "celery_retries": getattr(request, "retries", None),
    }


def _traceparent(task: Any) -> Any:
    """A ``traceparent`` sent as a custom message header (``apply_async(headers={"traceparent": ...})``) or kwarg-free
    ``request.headers``; None when absent."""
    try:
        request = getattr(task, "request", None)
        value = getattr(request, "traceparent", None)
        if value is None:
            headers = getattr(request, "headers", None)
            value = headers.get("traceparent") if isinstance(headers, dict) else None
        return value if isinstance(value, (str, bytes)) else None
    except Exception:
        return None


def setup(client: Any) -> None:
    from celery import signals

    version = framework_version("celery")

    def prerun(task_id: Any = None, task: Any = None, **kw: Any) -> None:
        try:
            tags = _task_tags(task)
            tags["celery_task_id"] = tags.get("celery_task_id") or task_id
            _scopes[str(task_id)] = begin_job("celery", tags, "celery task %s" % tags["celery_task"], version, task=tags["celery_task"], traceparent=_traceparent(task))
        except Exception:
            pass

    def postrun(task_id: Any = None, **kw: Any) -> None:
        end_request(_scopes.pop(str(task_id), None))

    def failure(sender: Any = None, task_id: Any = None, exception: Any = None, traceback: Any = None, **kw: Any) -> None:
        if exception is None:
            return
        cm = _scopes.get(str(task_id))
        tags = {k: v for k, v in _task_tags(sender).items() if v is not None}
        report(
            exception,
            scope=getattr(cm, "scope", None),
            tags=dict(tags, framework="celery", celery_task_id=str(task_id)),
            extra={"celery_task_id": str(task_id)},
        )

    def retry(sender: Any = None, request: Any = None, reason: Any = None, **kw: Any) -> None:
        try:  # retried attempts are not failures; keep a breadcrumb for the final event
            from .. import add_breadcrumb

            add_breadcrumb("celery task %s will retry: %s" % (getattr(sender, "name", "?"), str(reason)[:200]), category="custom", level="warning")
        except Exception:
            pass

    signals.task_prerun.connect(prerun, weak=False, dispatch_uid="insight_sdk.prerun")
    signals.task_postrun.connect(postrun, weak=False, dispatch_uid="insight_sdk.postrun")
    signals.task_failure.connect(failure, weak=False, dispatch_uid="insight_sdk.failure")
    task_retry: Optional[Any] = getattr(signals, "task_retry", None)
    if task_retry is not None:
        task_retry.connect(retry, weak=False, dispatch_uid="insight_sdk.retry")
