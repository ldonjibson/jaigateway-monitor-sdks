"""Shared assertions for the framework integration tests."""
import asyncio
from typing import Any, Dict, List, Tuple

import insight_sdk

SECRET_HEADERS = {"Authorization": "Bearer s3cret", "Cookie": "sid=abc", "X-API-Key": "k-123", "X-Trace": "t1"}


def sent(server) -> List[Dict[str, Any]]:
    assert insight_sdk.flush(5)
    return server.events


def assert_request(event: Dict[str, Any], path: str, method: str = "GET") -> None:
    """url without query/secrets, scrubbed headers, filtered query."""
    r = event["request"]
    assert r["url"].split("://", 1)[1].split("/", 1)[1] == path.lstrip("/"), r["url"]
    assert "?" not in r["url"] and r["method"] == method
    assert not {"authorization", "cookie", "x-api-key"} & set(r["headers"]), r["headers"]
    assert r["headers"].get("x-trace") == "t1"
    assert "s3cret" not in str(event) and "k-123" not in str(event) and "sid=abc" not in str(event)


def assert_tags(event: Dict[str, Any], framework: str, route: str) -> None:
    assert event["tags"]["framework"] == framework
    assert event["tags"]["framework_version"][0].isdigit()
    assert event["tags"]["route"] == route, event["tags"]


def crumbs(event: Dict[str, Any], category: str = "http") -> List[Dict[str, Any]]:
    return [b for b in event["breadcrumbs"] if b["category"] == category]


def run(coro):
    return asyncio.run(coro)


def perf(server) -> Dict[str, Dict[str, Any]]:
    """Flush and merge every metrics window the fake server received: name -> {count, errors, op, ...}."""
    assert insight_sdk.flush(5)
    out: Dict[str, Dict[str, Any]] = {}
    for body in server.metrics:
        assert body["platform"] == "python" and body["sdk"]["name"] == "insight.python"
        for t in body["transactions"]:
            o = out.setdefault(t["name"], {"count": 0, "errors": 0, "op": t["op"], "sum_ms": 0.0, "buckets": 0, "min_ms": t["min_ms"], "max_ms": 0})
            o["count"] += t["count"]
            o["errors"] += t["errors"]
            o["sum_ms"] += t["sum_ms"]
            o["buckets"] += sum(t["buckets"].values())
            o["min_ms"] = min(o["min_ms"], t["min_ms"])
            o["max_ms"] = max(o["max_ms"], t["max_ms"])
    for name, o in out.items():  # histogram and durations are consistent
        assert o["buckets"] == o["count"] and o["min_ms"] <= o["max_ms"], (name, o)
    return out


def assert_perf(server, expected: Dict[str, Tuple[int, int]], op: str = "http.server") -> None:
    """``expected``: transaction name -> (count, errors). Names are route templates, never raw paths."""
    got = perf(server)
    assert {n: (v["count"], v["errors"]) for n, v in got.items()} == expected, got
    assert all(v["op"] == op for v in got.values()), got
