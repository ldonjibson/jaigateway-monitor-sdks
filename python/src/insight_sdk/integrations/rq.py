"""RQ (Redis Queue).

    import insight_sdk
    from insight_sdk.integrations.rq import install
    insight_sdk.init(api_key="ak_...", host="https://insight.example.com"); install()   # in the worker process

``install()`` patches the RQ worker class once (``handle_exception`` and ``perform_job``): every job runs in its own
scope tagged ``framework=rq``, ``rq_job_id``, ``rq_func`` (dotted function name) and ``rq_queue``; a job that fails
is reported once with those tags (arguments are never sent). Jobs that still have ``Retry`` attempts left are not
reported until the last attempt fails. Events from forked work horses are flushed before the horse exits.

Alternatives: ``install(worker)`` for one worker instance (uses ``push_exc_handler``), or
``rq worker --exception-handler insight_sdk.integrations.rq.exception_handler``. ``init()`` enables ``install()``
automatically when ``rq`` is already imported.
"""
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from ._common import begin_job, end_request, framework_version, report

logger = logging.getLogger("insight_sdk")
_MARK = "_insight_sdk_rq"


def job_tags(job: Any) -> Dict[str, Any]:
    tags: Dict[str, Any] = {"rq_job_id": getattr(job, "id", None), "rq_queue": getattr(job, "origin", None)}
    try:
        tags["rq_func"] = job.func_name
    except Exception:  # DeserializationError: the job payload is unreadable
        tags["rq_func"] = "<unreadable>"
    return tags


def _job_traceparent(job: Any) -> Any:
    """``job.meta["traceparent"]`` (set by the enqueuing code), or None."""
    try:
        value = (getattr(job, "meta", None) or {}).get("traceparent")
        return value if isinstance(value, (str, bytes)) else None
    except Exception:
        return None


def _will_retry(job: Any) -> bool:
    left = getattr(job, "retries_left", None)
    return isinstance(left, int) and left > 0


def _report_job(job: Any, exc_info: Tuple[Any, ...], worker: Any = None) -> None:
    try:
        exc = exc_info[1] if len(exc_info) > 1 else None
        if exc is None or _will_retry(job):
            return
        tags = {k: v for k, v in job_tags(job).items() if v is not None}
        tags["framework"] = "rq"
        report(exc, tags=tags)  # the job scope (if active) supplies breadcrumbs and user data
        if worker is not None and getattr(worker, "is_horse", False):  # the horse leaves with os._exit(): nothing flushes later
            from .. import flush

            flush(2)
    except Exception as e:
        logger.debug("insight_sdk: rq report failed: %r", e)


def exception_handler(job: Any, exc_type: Any, exc_value: Any, tb: Any) -> bool:
    """RQ exception handler (``push_exc_handler`` / ``--exception-handler``); lets the chain continue."""
    _report_job(job, (exc_type, exc_value, tb))
    return True


def _flush_metrics_in_horse(worker: Any) -> None:
    """A forked work horse leaves with ``os._exit`` and its aggregated timing would be lost: send it now (one small POST)."""
    try:
        if getattr(worker, "is_horse", False):
            from .. import get_client

            client = get_client()
            if client is not None and client.performance is not None:
                client.performance.flush(2)
    except Exception as e:
        logger.debug("insight_sdk: rq metrics flush failed: %r", e)


def _wrap_perform_job(original: Callable[..., Any]) -> Callable[..., Any]:
    version = framework_version("rq")

    def perform_job(self: Any, job: Any, queue: Any, *args: Any, **kwargs: Any) -> Any:
        tags = job_tags(job)
        cm = begin_job("rq", tags, "rq job %s" % tags.get("rq_func"), version, task=tags.get("rq_func"), traceparent=_job_traceparent(job))
        try:
            return original(self, job, queue, *args, **kwargs)
        finally:
            end_request(cm)
            _flush_metrics_in_horse(self)

    return perform_job


def _defining_class(cls: Any, name: str) -> Any:
    for klass in cls.__mro__:
        if name in klass.__dict__:
            return klass
    return None


def install(worker: Optional[Any] = None) -> Callable[[], None]:
    """Hook RQ. Returns an ``undo`` function. Safe to call more than once."""
    import rq.worker as rq_worker

    if worker is not None:
        if getattr(worker, _MARK, False):
            return lambda: None
        worker.push_exc_handler(exception_handler)
        worker.perform_job = _wrap_perform_job(worker.perform_job.__func__).__get__(worker)  # type: ignore[attr-defined]
        setattr(worker, _MARK, True)
        return lambda: None

    patched = []
    cls = rq_worker.Worker
    handle_cls = _defining_class(cls, "handle_exception")
    perform_cls = _defining_class(cls, "perform_job")
    if handle_cls is None or perform_cls is None or handle_cls.__dict__.get(_MARK):
        return lambda: None
    orig_handle = handle_cls.handle_exception
    orig_perform = perform_cls.perform_job

    def handle_exception(self: Any, job: Any, *exc_info: Any) -> Any:
        _report_job(job, exc_info, self)
        return orig_handle(self, job, *exc_info)

    handle_cls.handle_exception = handle_exception
    perform_cls.perform_job = _wrap_perform_job(orig_perform)
    setattr(handle_cls, _MARK, True)
    patched.append((handle_cls, "handle_exception", orig_handle))
    patched.append((perform_cls, "perform_job", orig_perform))

    def undo() -> None:
        for klass, attr, original in patched:
            setattr(klass, attr, original)
        if handle_cls.__dict__.get(_MARK):
            delattr(handle_cls, _MARK)

    return undo


def setup(client: Any) -> Callable[[], None]:
    """Entry point used by ``init()``'s automatic mode."""
    return install()
