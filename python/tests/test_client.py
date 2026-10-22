import asyncio
import json
import threading

import pytest

import insight_sdk
from insight_sdk.client import build_endpoint
from insight_sdk.event import scrub_headers
from insight_sdk.scope import get_current_scope


def inner(secret_local="SECRET-LOCAL-VALUE"):
    password = "hunter2-LOCAL"  # noqa: F841  (must never be sent)
    raise ValueError("boom")


def outer():
    inner()


def captured(server, **ctx):
    try:
        outer()
    except ValueError:
        insight_sdk.capture_exception(**ctx)
    assert insight_sdk.flush(5)
    return server.events[-1]


def test_event_shape_frames_most_recent_first_and_in_app(server, init):
    init(release="web@1.4.2", environment="staging", server_name="api-1")
    e = captured(server)
    assert e["event_type"] == "error" and e["action"] == "exception" and e["status"] == "error"
    assert e["description"] == "ValueError: boom"
    assert e["level"] == "error" and e["platform"] == "python"
    assert e["sdk"] == {"name": "insight.python", "version": insight_sdk.__version__}
    assert (e["release"], e["environment"], e["server_name"]) == ("web@1.4.2", "staging", "api-1")
    assert e["timestamp"].endswith("Z") and "T" in e["timestamp"]
    assert e["tags"]["runtime"].startswith("CPython") and e["tags"]["os"]
    assert e["extra"]["python_version"]
    exc = e["exception"]
    assert exc["type"] == "ValueError" and exc["value"] == "boom"
    funcs = [f["function"] for f in exc["stacktrace"]]
    assert funcs[:3] == ["inner", "outer", "captured"]  # most recent first
    top = exc["stacktrace"][0]
    assert top["filename"].endswith("test_client.py") and isinstance(top["lineno"], int)
    assert top["in_app"] is True and top["module"] == "test_client"
    assert "Traceback (most recent call last)" in e["metadata"]["traceback"]
    assert "ValueError: boom" in e["metadata"]["traceback"]


def test_stdlib_frames_are_not_in_app(server, init):
    init()
    try:
        json.loads("{nope")
    except ValueError:
        insight_sdk.capture_exception()
    insight_sdk.flush(5)
    frames = server.events[0]["exception"]["stacktrace"]
    assert frames[0]["in_app"] is False  # json/decoder.py
    assert frames[-1]["in_app"] is True  # this test


def test_in_app_include_exclude(server, init):
    init(in_app_include=["json"], in_app_exclude=["test_client"])
    try:
        json.loads("{nope")
    except ValueError:
        insight_sdk.capture_exception()
    insight_sdk.flush(5)
    frames = server.events[0]["exception"]["stacktrace"]
    assert frames[0]["in_app"] is True and frames[-1]["in_app"] is False


def test_never_sends_local_variables(server, init):
    init()
    e = captured(server)
    blob = json.dumps(e)
    assert "SECRET-LOCAL-VALUE" not in blob and "hunter2-LOCAL" not in blob


def test_chained_exceptions(server, init):
    init()

    def chain():
        try:
            try:
                raise KeyError("root")
            except KeyError as a:
                raise RuntimeError("middle") from a
        except RuntimeError:
            raise ConnectionError("outermost")  # implicit __context__

    try:
        chain()
    except ConnectionError:
        insight_sdk.capture_exception()
    insight_sdk.flush(5)
    e = server.events[0]
    assert e["exception"]["type"] == "ConnectionError"
    assert e["description"] == "ConnectionError: outermost"
    ch = e["extra"]["exception_chain"]
    assert [c["type"] for c in ch] == ["ConnectionError", "RuntimeError", "KeyError"]
    assert [c["relation"] for c in ch] == [None, "context", "cause"]
    assert ch[2]["stacktrace"][0]["function"] == "chain"
    assert "KeyError" in e["metadata"]["traceback"]


def test_no_chain_key_for_simple_exception(server, init):
    init()
    assert "exception_chain" not in captured(server)["extra"]


def test_exception_argument_forms(server, init):
    init()
    err = ValueError("direct")
    insight_sdk.capture_exception(err)  # exception without traceback
    try:
        raise TypeError("tb")
    except TypeError:
        import sys
        insight_sdk.capture_exception(sys.exc_info())
    insight_sdk.flush(5)
    assert [e["exception"]["type"] for e in server.events] == ["ValueError", "TypeError"]
    assert insight_sdk.capture_exception() is None  # nothing being handled


def test_dedupe_same_exception_object(server, init):
    init()
    try:
        outer()
    except ValueError as e:
        assert insight_sdk.capture_exception(e)
        assert insight_sdk.capture_exception() is None
        assert insight_sdk.capture_exception(e, tags={"x": "y"}) is None
    insight_sdk.flush(5)
    assert len(server.events) == 1
    captured(server)  # a different exception object is a different event
    assert len(server.events) == 2


def test_sample_rate(server, init):
    init(sample_rate=0.0)
    captured_none = insight_sdk.capture_message("x")
    assert captured_none is None
    init(sample_rate=1.0)
    assert insight_sdk.capture_message("y")
    insight_sdk.flush(5)
    assert [e["description"] for e in server.events] == ["y"]


def test_before_send_can_modify_and_drop(server, init):
    seen = []

    def hook(event, hint):
        seen.append(hint)
        if "drop" in event["description"]:
            return None
        event["tags"]["touched"] = "yes"
        return event

    init(before_send=hook)
    insight_sdk.capture_message("drop me")
    insight_sdk.capture_message("keep me")
    insight_sdk.flush(5)
    assert [e["description"] for e in server.events] == ["keep me"]
    assert server.events[0]["tags"]["touched"] == "yes"
    assert seen[0]["message"] == "drop me"


def test_before_send_raising_drops_event(server, init):
    init(before_send=lambda e, h: 1 / 0)
    assert insight_sdk.capture_message("x") is None
    insight_sdk.flush(5)
    assert server.events == []


def test_rate_guard_limits_crash_loops(server, init):
    init(max_events_per_minute=5)
    ids = [insight_sdk.capture_message("m%d" % i) for i in range(50)]
    assert len([i for i in ids if i]) == 5
    insight_sdk.flush(5)
    assert len(server.events) == 5


def test_capture_message(server, init):
    init()
    insight_sdk.capture_message("deploy finished", level="warning", tags={"k": "v"}, order=7)
    insight_sdk.flush(5)
    e = server.events[0]
    assert e["description"] == "deploy finished" and e["level"] == "warning"
    assert e["tags"]["k"] == "v" and e["extra"]["order"] == 7 and "exception" not in e


def test_description_truncated_to_255(server, init):
    init()
    try:
        raise ValueError("x" * 1000)
    except ValueError:
        insight_sdk.capture_exception()
    insight_sdk.flush(5)
    assert len(server.events[0]["description"]) == 255


def test_scope_data_and_breadcrumbs_in_event(server, init):
    init(max_breadcrumbs=3)
    insight_sdk.set_user({"id": "42", "email": "a@b.com"})
    insight_sdk.set_tag("feature", "checkout")
    insight_sdk.set_tags({"a": 1})
    insight_sdk.set_extra("orderId", 991)
    for i in range(5):
        insight_sdk.add_breadcrumb("step %d" % i, category="ui.click", data={"i": i})
    e = captured(server, fingerprint=["checkout", "timeout"], tags={"x": "1"})
    assert e["user"] == {"id": "42", "email": "a@b.com"}
    assert e["tags"]["feature"] == "checkout" and e["tags"]["a"] == "1" and e["tags"]["x"] == "1"
    assert e["extra"]["orderId"] == 991
    assert [b["message"] for b in e["breadcrumbs"]] == ["step 2", "step 3", "step 4"]  # newest last, bounded
    assert e["breadcrumbs"][0]["category"] == "ui.click" and e["breadcrumbs"][0]["timestamp"].endswith("Z")
    assert e["fingerprint"] == ["checkout", "timeout"]


def test_push_scope_discards_changes(server, init):
    init()
    insight_sdk.set_tag("outer", "1")
    with insight_sdk.push_scope() as s:
        s.set_tag("inner", "1")
        insight_sdk.set_user({"id": "9"})
        insight_sdk.capture_message("in")
    insight_sdk.capture_message("out")
    with insight_sdk.configure_scope() as s:
        s.set_tag("configured", "1")
    insight_sdk.capture_message("after")
    insight_sdk.flush(5)
    inside, outside, after = server.events
    assert inside["tags"]["inner"] == "1" and inside["tags"]["outer"] == "1" and inside["user"] == {"id": "9"}
    assert "inner" not in outside["tags"] and "user" not in outside and outside["tags"]["outer"] == "1"
    assert after["tags"]["configured"] == "1"


def test_scope_isolated_between_threads(server, init):
    init()
    insight_sdk.set_tag("who", "main")
    seen = {}

    def worker(name):
        assert "who" not in get_current_scope().tags  # a new thread starts clean
        insight_sdk.set_tag("who", name)
        insight_sdk.set_user({"id": name})
        barrier.wait(5)  # all threads have written before any reads
        seen[name] = (get_current_scope().tags["who"], get_current_scope().user["id"])

    barrier = threading.Barrier(4)
    threads = [threading.Thread(target=worker, args=("t%d" % i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert seen == {"t%d" % i: ("t%d" % i, "t%d" % i) for i in range(4)}
    assert get_current_scope().tags["who"] == "main" and get_current_scope().user is None


def test_scope_isolated_between_asyncio_tasks(server, init):
    init()

    async def task(name, ready):
        insight_sdk.set_tag("who", name)
        insight_sdk.add_breadcrumb("crumb " + name)
        await ready.wait()
        await asyncio.sleep(0)
        s = get_current_scope()
        return s.tags["who"], [b["message"] for b in s.breadcrumbs], s.tags.get("parent")

    async def main():
        insight_sdk.set_tag("parent", "yes")
        ready = asyncio.Event()
        futs = [asyncio.ensure_future(task("a%d" % i, ready)) for i in range(3)]
        await asyncio.sleep(0.01)
        ready.set()
        res = await asyncio.gather(*futs)
        return res, dict(get_current_scope().tags), len(get_current_scope().breadcrumbs)

    res, parent_tags, parent_crumbs = asyncio.run(main())
    for i, (who, crumbs, parent) in enumerate(res):
        assert (who, crumbs, parent) == ("a%d" % i, ["crumb a%d" % i], "yes")  # inherits, but private
    assert parent_tags == {"parent": "yes"} and parent_crumbs == 0


@pytest.mark.parametrize(
    "host,endpoint,expected",
    [
        ("https://app.example.com", None, "https://app.example.com/api/ingest/events/"),
        ("https://app.example.com/", None, "https://app.example.com/api/ingest/events/"),
        ("https://app.example.com///", None, "https://app.example.com/api/ingest/events/"),
        ("https://app.example.com/api", None, "https://app.example.com/api/ingest/events/"),
        ("https://app.example.com/api/", None, "https://app.example.com/api/ingest/events/"),
        ("https://app.example.com/api/ingest/events", None, "https://app.example.com/api/ingest/events/"),
        ("app.example.com", None, "https://app.example.com/api/ingest/events/"),
        ("http://localhost:3000", None, "http://localhost:3000/api/ingest/events/"),
        (None, "https://x.io/api/ingest/events", "https://x.io/api/ingest/events/"),
        ("ignored", "https://x.io/custom/", "https://x.io/custom/"),
        (None, None, None),
    ],
)
def test_build_endpoint(host, endpoint, expected):
    assert build_endpoint(host, endpoint) == expected


def test_full_endpoint_option(server, init):
    init(host=None, endpoint=server.host + "/api/ingest/events")
    insight_sdk.capture_message("x")
    assert insight_sdk.flush(5)
    assert len(server.events) == 1


def test_misconfigured_init_never_raises():
    c = insight_sdk.init(api_key=None, host=None, integrations=[])
    assert not c.enabled
    assert insight_sdk.capture_message("x") is None
    assert insight_sdk.flush(0.1)
    insight_sdk.close()


def test_calls_before_init_are_noops():
    insight_sdk.close()
    assert insight_sdk.capture_exception(ValueError("x")) is None
    assert insight_sdk.capture_message("x") is None
    insight_sdk.add_breadcrumb("x")
    assert insight_sdk.flush() is True


def test_extra_is_sanitised_and_bounded(server, init):
    init()
    insight_sdk.set_extra("obj", object())
    insight_sdk.set_extra("huge", "y" * 100000)
    insight_sdk.set_extra("nested", {"a": {"b": [1, 2, {3, 4}]}})
    insight_sdk.capture_message("x")
    insight_sdk.flush(5)
    extra = server.events[0]["extra"]
    assert len(json.dumps(extra)) <= 16000
    assert isinstance(extra["obj"], str)


def test_scrub_headers_removes_credentials():
    out = scrub_headers(
        [("Authorization", "Bearer x"), ("Cookie", "a=b"), ("Set-Cookie", "a=b"), ("X-API-Key", "k"),
         ("User-Agent", "ua"), ("X-Request-Id", "1")]
    )
    assert out == {"user-agent": "ua", "x-request-id": "1"}
