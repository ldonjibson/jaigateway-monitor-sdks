import pytest

import insight_sdk
from fwutil import sent

celery = pytest.importorskip("celery")
from celery import Celery  # noqa: E402
from celery import signals as csignals  # noqa: E402


@pytest.fixture
def app(server, init):
    init(integrations=["celery"])
    app = Celery("fwtests", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = False
    app.conf.task_store_eager_result = True
    yield app
    for sig in (csignals.task_prerun, csignals.task_postrun, csignals.task_failure, csignals.task_retry):
        for uid in ("insight_sdk.prerun", "insight_sdk.postrun", "insight_sdk.failure", "insight_sdk.retry"):
            sig.disconnect(dispatch_uid=uid)


def test_real_celery_task_failure_tags_and_no_arguments(server, init, app):
    @app.task(name="tests.send_mail")
    def send_mail(to, token):
        insight_sdk.set_user({"id": "c-1"})
        raise ValueError("smtp down")

    res = send_mail.apply_async(("ada@example.com", "tok-secret-123"), queue="mail")
    assert res.failed()
    (e,) = sent(server)
    assert e["exception"]["value"] == "smtp down"
    t = e["tags"]
    assert t["framework"] == "celery" and t["celery_task"] == "tests.send_mail" and t["celery_task_id"] == res.id
    assert t["celery_retries"] == "0" and t["framework_version"][0].isdigit()
    assert e["user"] == {"id": "c-1"}
    assert "tok-secret-123" not in str(e) and "ada@example.com" not in str(e)
    assert e["extra"]["celery_task_id"] == res.id


def test_retries_are_not_reported_only_the_final_failure(server, init, app):
    attempts = []

    @app.task(bind=True, name="tests.flaky", max_retries=2, default_retry_delay=0)
    def flaky(self):
        attempts.append(self.request.retries)
        try:
            raise ConnectionError("attempt %d" % self.request.retries)
        except ConnectionError as exc:
            raise self.retry(exc=exc)

    res = flaky.apply()
    assert attempts == [0, 1, 2] and res.failed()
    events = sent(server)
    assert len(events) == 1, [e["description"] for e in events]  # one event for three attempts
    assert events[0]["exception"]["type"] == "ConnectionError" and events[0]["tags"]["celery_retries"] == "2"


def test_task_that_succeeds_after_retry_reports_nothing(server, init, app):
    @app.task(bind=True, name="tests.recovers", max_retries=3, default_retry_delay=0)
    def recovers(self):
        if self.request.retries < 1:
            raise self.retry(exc=OSError("transient"))
        return "ok"

    assert recovers.apply().get() == "ok"
    assert sent(server) == []


def test_scope_does_not_leak_between_tasks(server, init, app):
    @app.task(name="tests.tagger")
    def tagger():
        insight_sdk.set_tag("leak", "1")

    @app.task(name="tests.boom")
    def boom():
        raise KeyError("b")

    tagger.apply()
    boom.apply()
    (e,) = sent(server)
    assert "leak" not in e["tags"] and e["tags"]["celery_task"] == "tests.boom"


def test_celery_performance_one_transaction_per_execution(server, init, app):
    from fwutil import assert_perf

    @app.task(name="tests.ok_task")
    def ok_task():
        return 1

    @app.task(name="tests.bad_task")
    def bad_task():
        raise ValueError("nope")

    @app.task(bind=True, name="tests.flaky_task", max_retries=2, default_retry_delay=0)
    def flaky_task(self):
        raise self.retry(exc=ConnectionError("again"))

    ok_task.apply()
    ok_task.apply()
    bad_task.apply()
    flaky_task.apply()  # 3 executions: two retried attempts (timing samples, not errors) + the final failure
    assert_perf(server, {"TASK tests.ok_task": (2, 0), "TASK tests.bad_task": (1, 1), "TASK tests.flaky_task": (3, 1)}, op="queue.task")
