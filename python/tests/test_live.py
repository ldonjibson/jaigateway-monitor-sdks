"""Runs against a real Insight server. Skipped unless the sdk-test-server wrote its info file:

    cd insight-next && node scripts/sdk-test-server.mjs 3142 &
"""
import json
import logging
import os
import time
import urllib.request
import uuid

import pytest

import insight_sdk

INFO = "/Users/user/Documents/alljobs/jobs/insight/insight-next/test-results/sdk-server-3142.json"
pytestmark = pytest.mark.skipif(not os.path.exists(INFO), reason="no live sdk-test-server (%s)" % INFO)


def api(info, path):
    req = urllib.request.Request(info["base"] + path, headers={"Authorization": "Bearer " + info["token"]})
    return json.load(urllib.request.urlopen(req, timeout=10))


def issues_of_type(info, type_name):
    for _ in range(20):
        found = [r for r in api(info, "/analytics/errors/")["results"] if r["error_type"] == type_name]
        if found:
            return found
        time.sleep(0.25)
    return []


def test_live_roundtrip_grouping_and_context():
    info = json.load(open(INFO))
    run = uuid.uuid4().hex[:8]
    BugA = type("LiveBugA" + run, (Exception,), {})
    BugB = type("LiveBugB" + run, (Exception,), {})

    insight_sdk.init(api_key=info["key"], host=info["host"], release="py@" + run, environment="live-test",
                     integrations=["logging"], shutdown_timeout=10)
    insight_sdk.set_user({"id": "u-" + run, "email": "ada@example.com"})
    insight_sdk.set_tag("suite", "live")
    insight_sdk.add_breadcrumb("opened cart", category="ui.click")
    logging.getLogger("live").setLevel(logging.INFO)
    logging.getLogger("live").info("charging card")

    def checkout(flag):  # the same bug raised from two different lines
        if flag:
            raise BugA("gateway timeout")
        raise BugA("gateway timeout")

    def other():
        raise BugB("different problem")

    for flag in (True, False):
        try:
            checkout(flag)
        except BugA:
            insight_sdk.capture_exception()
    try:
        other()
    except BugB:
        insight_sdk.capture_exception()
    try:
        try:
            raise KeyError("root cause")
        except KeyError as e:
            raise BugB("wrapped") from e
    except BugB:
        logging.getLogger("live").exception("logged error")
    assert insight_sdk.flush(15)

    a = issues_of_type(info, "LiveBugA" + run)
    assert len(a) == 1 and a[0]["count"] == 2  # raised from two different lines: ONE issue
    issue = a[0]
    md = issue["metadata"]
    assert (md["release"], md["environment"], md["platform"]) == ("py@" + run, "live-test", "python")
    assert md["user"]["id"] == "u-" + run and md["tags"]["suite"] == "live"
    assert [b["message"] for b in md["breadcrumbs"]] == ["opened cart", "charging card"]
    assert md["exception"]["stacktrace"][0]["function"] == "checkout"  # most recent first
    assert md["exception"]["stacktrace"][0]["in_app"] is True
    assert md["sdk"]["name"] == "insight.python"

    b = issues_of_type(info, "LiveBugB" + run)
    assert b and all(x["id"] != issue["id"] for x in b)  # a different bug is a different issue
    titles = {x["title"] for x in b}
    assert "LiveBugB%s: different problem" % run in titles and "LiveBugB%s: wrapped" % run in titles
    wrapped = next(x for x in b if x["title"].endswith("wrapped"))
    chain = wrapped["metadata"]["extra"]["exception_chain"]
    assert [c["type"] for c in chain] == ["LiveBugB" + run, "KeyError"]
    insight_sdk.close()
