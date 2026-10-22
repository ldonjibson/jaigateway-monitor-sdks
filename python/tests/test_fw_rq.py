import pytest

import insight_sdk
from fwutil import sent

rq = pytest.importorskip("rq")
fakeredis = pytest.importorskip("fakeredis")
from rq import Queue, Retry, SimpleWorker  # noqa: E402

import fw_jobs  # noqa: E402
from insight_sdk.integrations import rq as insight_rq  # noqa: E402


@pytest.fixture
def redis():
    return fakeredis.FakeStrictRedis()


@pytest.fixture
def patched():
    undo = insight_rq.install()
    insight_rq.install()  # twice is harmless
    yield
    undo()


def run_jobs(redis, queue_name="default"):
    q = Queue(queue_name, connection=redis)
    return q


def test_failed_job_reported_once_with_job_tags(server, init, redis, patched):
    init()
    q = Queue("emails", connection=redis)
    job = q.enqueue(fw_jobs.boom, "super-secret-value")
    SimpleWorker([q], connection=redis).work(burst=True)
    assert job.get_status() == "failed"  # RQ's own failure handling still happened
    (e,) = sent(server)
    assert e["exception"]["value"] == "job boom"
    t = e["tags"]
    assert t["framework"] == "rq" and t["rq_job_id"] == job.id and t["rq_func"] == "fw_jobs.boom" and t["rq_queue"] == "emails"
    assert t["framework_version"][0].isdigit()
    assert e["user"] == {"id": "job-user"}  # job scope active while the job ran
    assert "super-secret-value" not in str(e) and "s3cret-argument" not in str(e)  # arguments are never sent
    assert any(b["message"].startswith("rq job") for b in e["breadcrumbs"])


def test_successful_job_not_reported_and_scope_isolated(server, init, redis, patched):
    init()
    q = Queue(connection=redis)
    q.enqueue(fw_jobs.fine)
    q.enqueue(fw_jobs.boom)
    SimpleWorker([q], connection=redis).work(burst=True)
    (e,) = sent(server)
    assert "leak" not in e["tags"]  # the previous job's scope did not leak into the next one


def test_retries_reported_only_when_exhausted(server, init, redis, patched):
    init()
    q = Queue(connection=redis)
    job = q.enqueue(fw_jobs.flaky_always, retry=Retry(max=2, interval=0))
    SimpleWorker([q], connection=redis).work(burst=True)
    assert job.get_status() == "failed"
    events = sent(server)
    assert len(events) == 1 and events[0]["exception"]["value"] == "always fails"


def test_install_for_one_worker_instance_and_cli_handler(server, init, redis):
    init()
    q = Queue(connection=redis)
    q.enqueue(fw_jobs.boom)
    worker = SimpleWorker([q], connection=redis)
    insight_rq.install(worker)
    worker.work(burst=True)
    (e,) = sent(server)
    assert e["tags"]["rq_func"] == "fw_jobs.boom"

    q.enqueue(fw_jobs.boom)  # `rq worker --exception-handler insight_sdk.integrations.rq.exception_handler`
    w2 = SimpleWorker([q], connection=redis, exception_handlers=[insight_rq.exception_handler])
    w2.work(burst=True)
    assert len(sent(server)) == 2


def test_handler_called_with_unreadable_job_does_not_raise(server, init):
    init()

    class BadJob:
        id = "j1"
        origin = "q"
        retries_left = None

        @property
        def func_name(self):
            raise RuntimeError("cannot deserialize")

    try:
        raise OSError("x")
    except OSError:
        import sys

        assert insight_rq.exception_handler(BadJob(), *sys.exc_info()) is True
    (e,) = sent(server)
    assert e["tags"]["rq_func"] == "<unreadable>"


def test_rq_performance_one_transaction_per_job(server, init, redis, patched):
    from fwutil import assert_perf

    init()
    q = Queue(connection=redis)
    q.enqueue(fw_jobs.fine)
    q.enqueue(fw_jobs.fine)
    q.enqueue(fw_jobs.boom)
    q.enqueue(fw_jobs.flaky_always, retry=Retry(max=2, interval=0))  # 3 executions, only the last one is an error
    SimpleWorker([q], connection=redis).work(burst=True)
    assert_perf(server, {"TASK fw_jobs.fine": (2, 0), "TASK fw_jobs.boom": (1, 1), "TASK fw_jobs.flaky_always": (3, 1)}, op="queue.task")
