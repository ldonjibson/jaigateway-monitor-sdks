import pytest

import insight_sdk
from fwutil import sent

huey_mod = pytest.importorskip("huey")
from huey import MemoryHuey  # noqa: E402

from insight_sdk.integrations.huey import install  # noqa: E402


@pytest.fixture
def huey():
    h = MemoryHuey("insight-test", immediate=True)
    install(h)
    install(h)  # idempotent
    return h


def test_immediate_mode_failure_reported_once_with_task_tags(server, init, huey):
    init()

    @huey.task()
    def explode(secret):
        insight_sdk.set_user({"id": "h-1"})
        raise ValueError("huey boom")

    res = explode("top-secret-arg")
    with pytest.raises(Exception):
        res()  # huey surfaces the failure when the result is read
    (e,) = sent(server)
    assert e["exception"]["value"] == "huey boom"
    t = e["tags"]
    assert t["framework"] == "huey" and t["huey_task"] == "explode" and t["huey_task_id"] == res.id
    assert e["user"] == {"id": "h-1"} and "top-secret-arg" not in str(e)


def test_success_no_event_and_no_scope_leak(server, init, huey):
    init()

    @huey.task()
    def fine():
        insight_sdk.set_tag("leak", "1")
        return 1

    @huey.task()
    def bad():
        raise KeyError("bad")

    assert fine()() == 1
    bad()
    (e,) = sent(server)
    assert e["exception"]["type"] == "KeyError" and "leak" not in e["tags"]


def test_retrying_task_only_final_failure_reported(server, init, huey):
    init()
    calls = []

    @huey.task(retries=2)
    def flaky():
        calls.append(1)
        raise RuntimeError("attempt %d" % len(calls))

    flaky()
    assert len(calls) == 3
    (e,) = sent(server)
    assert e["exception"]["value"] == "attempt 3"


def test_huey_performance_one_transaction_per_execution(server, init, huey):
    from fwutil import assert_perf

    init()

    @huey.task()
    def fine():
        return 1

    @huey.task()
    def bad():
        raise KeyError("bad")

    @huey.task(retries=2)
    def flaky():
        raise RuntimeError("again")

    fine()
    fine()
    bad()
    flaky()
    assert_perf(server, {"TASK fine": (2, 0), "TASK bad": (1, 1), "TASK flaky": (3, 1)}, op="queue.task")
