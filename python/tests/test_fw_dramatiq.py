import pytest

import insight_sdk
from fwutil import sent

dramatiq = pytest.importorskip("dramatiq")
from dramatiq import Worker  # noqa: E402
from dramatiq.brokers.stub import StubBroker  # noqa: E402

from insight_sdk.integrations.dramatiq import InsightMiddleware  # noqa: E402


@pytest.fixture
def broker():
    b = StubBroker()
    b.emit_after("process_boot")
    b.add_middleware(InsightMiddleware())
    dramatiq.set_broker(b)
    yield b
    b.flush_all()
    b.close()


def run(broker, *actors):
    worker = Worker(broker, worker_timeout=50)
    worker.start()
    try:
        try:
            broker.join("default", fail_fast=False)  # newer Dramatiq re-raises the first failure from join() by default
        except TypeError:
            broker.join("default")  # older versions have no fail_fast
        worker.join()
    finally:
        worker.stop()


def test_failure_reported_once_with_actor_tags(server, init, broker):
    init()

    @dramatiq.actor(max_retries=0)
    def explode(secret):
        insight_sdk.set_user({"id": "d-1"})
        raise ValueError("dramatiq boom")

    msg = explode.send("top-secret-arg")
    run(broker)
    (e,) = sent(server)
    assert e["exception"]["value"] == "dramatiq boom"
    t = e["tags"]
    assert t["framework"] == "dramatiq" and t["dramatiq_actor"] == "explode" and t["dramatiq_message_id"] == msg.message_id
    assert t["dramatiq_queue"] == "default" and t["dramatiq_retries"] == "0"
    assert e["user"] == {"id": "d-1"} and "top-secret-arg" not in str(e)


def test_retries_report_only_the_final_failure(server, init, broker):
    init()
    calls = []

    @dramatiq.actor(max_retries=2, min_backoff=1, max_backoff=1)
    def flaky():
        calls.append(1)
        raise RuntimeError("attempt %d" % len(calls))

    flaky.send()
    run(broker)
    assert len(calls) == 3
    (e,) = sent(server)
    assert e["exception"]["value"] == "attempt 3" and e["tags"]["dramatiq_retries"] == "2"
    assert [b["message"] for b in e["breadcrumbs"]].count("dramatiq actor flaky") == 1


def test_success_throws_and_scope_isolation(server, init, broker):
    init()

    class Expected(Exception):
        pass

    @dramatiq.actor(max_retries=0)
    def ok():
        insight_sdk.set_tag("leak", "1")

    @dramatiq.actor(max_retries=0, throws=(Expected,))
    def expected():
        raise Expected("fine")

    @dramatiq.actor(max_retries=0)
    def bad():
        raise KeyError("bad")

    ok.send()
    expected.send()
    bad.send()
    run(broker)
    (e,) = sent(server)
    assert e["exception"]["type"] == "KeyError" and "leak" not in e["tags"]


def test_dramatiq_performance_one_transaction_per_message(server, init, broker):
    from fwutil import assert_perf

    init()

    @dramatiq.actor(max_retries=0)
    def good():
        return 1

    @dramatiq.actor(max_retries=0)
    def explode():
        raise ValueError("boom")

    @dramatiq.actor(max_retries=1, min_backoff=1, max_backoff=1)
    def flaky():
        raise RuntimeError("again")

    good.send()
    good.send()
    explode.send()
    flaky.send()
    run(broker)
    assert_perf(server, {"TASK good": (2, 0), "TASK explode": (1, 1), "TASK flaky": (2, 1)}, op="queue.task")
