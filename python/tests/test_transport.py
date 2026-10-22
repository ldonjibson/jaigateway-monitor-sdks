import logging
import os
import sys
import threading
import time

import pytest

import insight_sdk
from conftest import wait_for
from insight_sdk.transport import Transport


def boom(msg="boom"):
    try:
        raise ValueError(msg)
    except ValueError:
        return insight_sdk.capture_exception()


def test_201_delivers_with_auth_header_and_path(server, init):
    init()
    assert boom()
    assert insight_sdk.flush(5)
    req = server.requests[0]
    assert req["path"] == "/api/ingest/events/"
    assert req["headers"]["x-api-key"] == "ak_test"
    assert req["headers"]["content-type"] == "application/json"
    assert req["headers"]["user-agent"].startswith("insight.python/")
    assert len(server.events) == 1


def test_400_drops_without_retry(server, init):
    c = init()
    server.script = [(400, {}, {"success": False})]
    boom()
    insight_sdk.flush(5)
    assert len(server.requests) == 1
    assert c.transport.dropped == 1 and not c.transport.disabled


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_disables_and_logs_once(server, init, caplog, status):
    c = init()
    server.default = (status, {}, {"success": False})
    with caplog.at_level(logging.WARNING, logger="insight_sdk"):
        boom("a")
        insight_sdk.flush(5)
        boom("b")
        boom("c")
        insight_sdk.flush(5)
    assert len(server.requests) == 1  # nothing sent after the first rejection, no retry
    assert c.transport.disabled
    assert len([r for r in caplog.records if r.name == "insight_sdk" and r.levelno == logging.WARNING]) == 1


def test_413_splits_batches_then_delivers_everything(server, init):
    c = init()
    gate = threading.Event()

    def responder(req):
        if not gate.is_set():
            gate.wait(5)
        return (413, {}, {"code": "batch_too_large"}) if len(req["events"]) > 2 else (201, {}, {})

    server.responder = responder
    boom("first")
    wait_for(lambda: len(server.requests) == 1)
    for i in range(8):
        boom("e%d" % i)
    gate.set()
    assert insight_sdk.flush(5)
    assert max(len(r["events"]) for r in server.requests[1:]) <= 8
    # every event ends up accepted in a request of <= 2 events
    ok = [e for r in server.requests if len(r["events"]) <= 2 for e in r["events"]]
    assert {e["description"] for e in ok} == {"ValueError: first"} | {"ValueError: e%d" % i for i in range(8)}


def test_413_single_event_dropped(server, init):
    c = init()
    server.default = (413, {}, {"code": "payload_too_large"})
    boom()
    insight_sdk.flush(5)
    assert len(server.requests) == 1 and c.transport.dropped == 1


def test_429_rate_limited_honours_retry_after(server, init):
    init()
    server.script = [(429, {"Retry-After": "0.3"}, {"code": "rate_limited"})]
    boom()
    assert insight_sdk.flush(5)
    assert len(server.requests) == 2
    assert server.requests[1]["time"] - server.requests[0]["time"] >= 0.29


def test_429_quota_exceeded_pauses_drops_and_logs_once(server, init, caplog):
    c = init()
    server.script = [(429, {"Retry-After": "99999"}, {"code": "quota_exceeded"})]
    with caplog.at_level(logging.WARNING, logger="insight_sdk"):
        boom("a")
        insight_sdk.flush(5)
        for i in range(5):
            boom("b%d" % i)
        insight_sdk.flush(5)
    assert len(server.requests) == 1
    remaining = c.transport.paused_until - time.monotonic()
    assert 3590 < remaining <= 3600  # capped at one hour
    assert len([r for r in caplog.records if r.name == "insight_sdk" and r.levelno == logging.WARNING]) == 1
    assert c.transport.dropped >= 6
    # once the pause is over, sending resumes
    c.transport.paused_until = time.monotonic() - 1
    boom("after")
    insight_sdk.flush(5)
    assert len(server.requests) == 2


def test_500_retried_then_success(server, init):
    c = init()
    server.script = [(500, {}, {}), (503, {}, {})]
    boom()
    assert insight_sdk.flush(5)
    assert len(server.requests) == 3 and c.transport.sent == 1


def test_500_gives_up_after_five_attempts(server, init):
    c = init()
    server.default = (500, {}, {})
    boom()
    assert insight_sdk.flush(10)
    assert len(server.requests) == 5 and c.transport.dropped == 1


def test_network_error_retried_and_never_raises(server, init):
    c = init()
    server.script = [None, None]  # connection dropped without a response
    boom()
    assert insight_sdk.flush(5)
    assert len(server.requests) == 3 and c.transport.sent == 1


def test_unreachable_host_does_not_raise_or_block(init):
    c = init(host="http://127.0.0.1:1", max_attempts=2)
    t0 = time.monotonic()
    assert boom()  # returns an event id immediately
    assert time.monotonic() - t0 < 0.5
    assert insight_sdk.flush(5)
    assert c.transport.last_status is None and c.transport.dropped == 1


def test_backoff_grows_exponentially_with_jitter():
    t = Transport("http://x/", "k", backoff_base=1.0)
    for attempt, top in [(1, 1), (2, 2), (3, 4), (4, 8), (10, 60)]:
        samples = [t._backoff(attempt) for _ in range(50)]
        assert max(samples) <= top and min(samples) >= top / 2 - 1e-9
        assert len(set(samples)) > 1


def blocked_server(server):
    gate = threading.Event()
    server.responder = lambda req: (gate.wait(10), (201, {}, {}))[1]
    return gate


def test_queue_is_bounded_and_drops_oldest(server, init):
    c = init(max_queue_size=3)
    gate = blocked_server(server)
    boom("first")
    wait_for(lambda: len(server.requests) == 1)
    for i in range(10):
        boom("e%d" % i)
    assert c.transport.dropped == 7
    gate.set()
    assert insight_sdk.flush(5)
    descs = [e["description"] for e in server.events]
    assert descs == ["ValueError: first", "ValueError: e7", "ValueError: e8", "ValueError: e9"]


def test_batches_of_up_to_20(server, init):
    init()
    gate = blocked_server(server)
    boom("first")
    wait_for(lambda: len(server.requests) == 1)
    for i in range(25):
        boom("e%d" % i)
    gate.set()
    assert insight_sdk.flush(10)
    assert [len(r["events"]) for r in server.requests] == [1, 20, 5]


def test_flush_times_out_and_capture_never_blocks(server, init):
    init(max_events_per_minute=0)
    gate = blocked_server(server)
    t0 = time.monotonic()
    for _ in range(30):
        boom()
    assert time.monotonic() - t0 < 1.0
    t0 = time.monotonic()
    assert insight_sdk.flush(0.3) is False
    assert time.monotonic() - t0 < 1.0
    gate.set()
    assert insight_sdk.flush(10) is True


def test_close_flushes(server, init):
    init()
    boom()
    insight_sdk.close(5)
    assert len(server.events) == 1
    assert boom() is None  # closed: no-op


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_fork_safety_child_gets_its_own_worker(server, init):
    init()
    boom("parent-before")
    assert insight_sdk.flush(5)  # worker thread is running in the parent
    pid = os.fork()
    if pid == 0:  # child: the parent's worker thread does not exist here
        code = 1
        try:
            boom("child")
            code = 0 if insight_sdk.flush(5) else 2
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0
    boom("parent-after")
    assert insight_sdk.flush(5)
    descs = [e["description"] for e in server.events]
    assert sorted(descs) == sorted(["ValueError: parent-before", "ValueError: child", "ValueError: parent-after"])
