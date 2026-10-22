import asyncio
import logging
import os
import re
import threading
import time

import pytest

import insight_sdk
from conftest import wait_for
from insight_sdk import performance as perfmod
from insight_sdk.client import build_metrics_endpoint
from insight_sdk.performance import BOUNDS_MS, DEFAULT_IGNORE, Window, bucket_index, compile_ignore

PROTOCOL_BOUNDS = [1, 2, 3, 5, 8, 12, 18, 25, 40, 60, 90, 130, 200, 300, 450, 700, 1000, 1500, 2200, 3300, 5000, 7500, 11000, 17000, 25000, 40000, 60000]


def rec(name="GET /x", ms=10.0, status=200, ok=True, op="http.server"):
    insight_sdk.get_client().performance.record(name, op, ms, status, ok)


def sent_metrics(server):
    assert insight_sdk.flush(5)
    return server.metrics


# -- buckets --------------------------------------------------------------------------------------------------------
def test_bounds_match_the_protocol_table():
    assert BOUNDS_MS == PROTOCOL_BOUNDS
    protocol = os.path.join(os.path.dirname(__file__), "..", "..", "PROTOCOL.md")
    if os.path.exists(protocol):
        text = open(protocol).read()
        m = re.search(r"BOUNDS_MS = \[([^\]]+)\]", text)
        assert [int(x) for x in m.group(1).split(",")] == BOUNDS_MS


def test_bucket_index_table_and_edges():
    for i, bound in enumerate(PROTOCOL_BOUNDS):
        assert bucket_index(bound) == i  # d <= BOUNDS[i]: the bound itself is inside
        assert bucket_index(bound + 0.01) == i + 1
    assert bucket_index(0) == 0 and bucket_index(0.4) == 0
    assert bucket_index(1) == 0 and bucket_index(1.01) == 1
    assert bucket_index(60000) == 26 and bucket_index(60001) == 27
    assert bucket_index(10 ** 9) == 27 == len(BOUNDS_MS)
    assert bucket_index(5) == 3 and bucket_index(6) == 4 and bucket_index(130) == 11


# -- aggregation / payload ------------------------------------------------------------------------------------------
def test_payload_shape_and_aggregation(server, init):
    init(release="api@1.4.2", environment="production", server_name="api-1")
    for ms in (3, 8, 8.5, 950):
        rec("GET /items/{id}", ms)
    rec("GET /items/{id}", 1000, 500, False)
    rec("POST /orders", 40, 201)
    (body,) = sent_metrics(server)
    assert server.metrics_requests[0]["path"] == "/api/ingest/metrics/"
    assert server.metrics_requests[0]["headers"]["x-api-key"] == "ak_test"
    assert server.metrics_requests[0]["headers"]["content-type"] == "application/json"
    assert body["sdk"] == {"name": "insight.python", "version": insight_sdk.__version__}
    assert body["platform"] == "python"
    assert (body["release"], body["environment"], body["server_name"]) == ("api@1.4.2", "production", "api-1")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", body["interval_start"])
    assert isinstance(body["interval_seconds"], int) and body["interval_seconds"] >= 1
    by = {t["name"]: t for t in body["transactions"]}
    items = by["GET /items/{id}"]
    assert items["op"] == "http.server" and items["count"] == 5 and items["errors"] == 1
    assert items["sum_ms"] == pytest.approx(3 + 8 + 8.5 + 950 + 1000) and items["min_ms"] == 3 and items["max_ms"] == 1000
    # 3 -> idx 2, 8 -> 4, 8.5 -> 5, 950 -> 16, 1000 -> 16
    assert items["buckets"] == {"2": 1, "4": 1, "5": 1, "16": 2}
    assert by["POST /orders"]["count"] == 1 and by["POST /orders"]["errors"] == 0
    assert "samples" in body and len(body["samples"]) <= 10


def test_window_unit_aggregation():
    w = Window(time.time(), time.monotonic())
    for ms in (1, 1.01, 60000, 60001, 5):
        w.add("n", "http.server", ms, 200, True, time.time())
    (t,) = w.transactions()
    assert t["buckets"] == {"0": 1, "1": 1, "26": 1, "27": 1, "3": 1}
    assert t["count"] == 5 and t["min_ms"] == 1 and t["max_ms"] == 60001 and t["errors"] == 0


def test_sample_selection_limits(server, init):
    init()
    for i in range(200):
        rec("GET /a", float(i), 500 if i % 10 == 0 else 200, ok=i % 10 != 0)
    (body,) = sent_metrics(server)
    samples = body["samples"]
    assert 1 <= len(samples) <= 10
    durations = {s["duration_ms"] for s in samples}
    assert {199.0, 198.0, 197.0} <= durations  # the slowest 3
    failed = [s for s in samples if not s["ok"]]
    assert 1 <= len(failed) <= 5 + 3  # up to 5 failed (the slowest 3 may overlap or be failed ones)
    assert len({s["trace_id"] for s in samples}) == len(samples)
    for s in samples:
        assert re.fullmatch(r"[0-9a-f]{32}", s["trace_id"])
        assert s["name"] == "GET /a" and "status_code" in s and isinstance(s["ok"], bool)
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", s["started_at"]), s["started_at"]


def test_sample_selection_exact_counts():
    w = Window(time.time(), time.monotonic())
    for i in range(1000):
        w.add("n", "o", float(i), 500 if i % 2 else 200, i % 2 == 0, time.time())
    samples = w.samples()
    assert len(samples) <= 10
    assert sum(1 for s in samples if not s["ok"]) >= 5  # five failed (+ maybe slow ones that failed)
    assert max(s["duration_ms"] for s in samples) == 999.0


def test_cardinality_guard_folds_into_other():
    w = Window(time.time(), time.monotonic())
    for i in range(700):
        w.add("GET /n%d" % i, "http.server", 1.0, 200, True, time.time())
    names = [t["name"] for t in w.transactions()]
    assert len(names) == 500 and "<other>" in names
    assert sum(t["count"] for t in w.transactions()) == 700


def test_names_are_truncated(server, init):
    init()
    rec("GET /" + "x" * 1000)
    (body,) = sent_metrics(server)
    assert len(body["transactions"][0]["name"]) == 300


# -- response table -------------------------------------------------------------------------------------------------
def test_201_is_done_and_nothing_is_resent(server, init):
    init()
    rec()
    assert insight_sdk.flush(5)
    assert insight_sdk.flush(5)
    assert len(server.metrics_requests) == 1
    assert insight_sdk.get_client().performance.sent_requests == 1


def test_400_drops_the_window(server, init):
    init()
    server.metrics_script = [(400, {}, {"error": "bad"})]
    rec()
    assert insight_sdk.flush(5)
    m = insight_sdk.get_client().performance
    assert len(server.metrics_requests) == 1 and m.buffered_windows() == 0 and m.active
    rec()
    assert insight_sdk.flush(5)
    assert len(server.metrics_requests) == 2  # still sending


@pytest.mark.parametrize("status", [401, 403])
def test_401_403_disable_only_metrics_and_log_once(server, init, caplog, status):
    init()
    server.metrics_script = [(status, {}, {"error": "no"})]
    rec()
    with caplog.at_level(logging.WARNING, logger="insight_sdk"):
        assert insight_sdk.flush(5)
        rec()  # no-op now
        rec()
        assert insight_sdk.flush(5)
    m = insight_sdk.get_client().performance
    assert m.active is False and len(server.metrics_requests) == 1
    assert len([r for r in caplog.records if "performance" in r.getMessage()]) == 1
    # error reporting is untouched
    assert insight_sdk.capture_message("still works")
    assert insight_sdk.flush(5)
    assert [e["message"] for e in server.events] == ["still works"]
    # and the transaction helpers are no-ops (no timing, no exception)
    with insight_sdk.start_transaction("x"):
        pass
    assert len(server.metrics_requests) == 1


def test_413_drops_samples_keeps_counts(server, init):
    init()
    server.metrics_script = [(413, {}, {"code": "payload_too_large"})]
    rec("GET /a", 5)
    rec("GET /a", 7)
    assert insight_sdk.flush(5)
    first, second = server.metrics
    assert "samples" in first and "samples" not in second
    assert second["transactions"] == first["transactions"] and second["transactions"][0]["count"] == 2
    assert insight_sdk.get_client().performance.buffered_windows() == 0


def test_413_twice_drops_the_window(server, init):
    init()
    server.metrics_script = [(413, {}, {}), (413, {}, {})]
    rec()
    assert insight_sdk.flush(5)
    assert len(server.metrics_requests) == 2
    assert insight_sdk.get_client().performance.buffered_windows() == 0


def test_429_rate_limited_waits_retry_after_then_retries_the_window(server, init):
    init()
    server.metrics_script = [(429, {"Retry-After": "1"}, {"code": "rate_limited", "error": "slow down"})]
    rec("GET /a", 5)
    t0 = time.monotonic()
    assert insight_sdk.flush(5)
    waited = time.monotonic() - t0
    assert 0.9 <= waited < 4
    first, second = server.metrics
    assert first["transactions"] == second["transactions"] and first["interval_start"] == second["interval_start"]
    assert insight_sdk.get_client().performance.active


def test_429_rate_limited_longer_than_the_flush_budget_keeps_the_window(server, init):
    init()
    server.metrics_script = [(429, {"Retry-After": "30"}, {"code": "rate_limited"})]
    rec()
    assert insight_sdk.flush(0.5) is False
    m = insight_sdk.get_client().performance
    assert m.buffered_windows() == 1 and len(server.metrics_requests) == 1


def quota_response(retry_after="3600"):
    return (429, {"Retry-After": retry_after}, {"code": "quota_exceeded", "quota": "performance", "error": "quota"})


def test_429_quota_pauses_only_metrics_not_errors(server, init, caplog):
    init()
    server.metrics_script = [quota_response("120")]
    rec("GET /a", 5)
    with caplog.at_level(logging.WARNING, logger="insight_sdk"):
        assert insight_sdk.flush(5)
        before = len(server.metrics_requests)
        for _ in range(5):  # dropped meanwhile: nothing buffered, nothing sent
            rec("GET /a", 5)
        assert insight_sdk.flush(5)
    m = insight_sdk.get_client().performance
    assert before == 1 and len(server.metrics_requests) == 1 and m.buffered_windows() == 0
    assert 100 < m.paused_until - time.monotonic() <= 120
    assert len([r for r in caplog.records if "quota" in r.getMessage()]) == 1
    # an error event still goes out, and the events transport was not paused or disabled
    assert insight_sdk.capture_exception(ValueError("boom"))
    assert insight_sdk.capture_message("also fine")
    assert insight_sdk.flush(5)
    assert len(server.events) == 2 and not insight_sdk.get_client().transport.disabled
    assert insight_sdk.get_client().transport.paused_until == 0.0
    # pause over -> recording resumes
    m.paused_until = time.monotonic() - 1
    rec("GET /b", 5)
    assert insight_sdk.flush(5)
    assert len(server.metrics_requests) == 2 and server.metrics[1]["transactions"][0]["name"] == "GET /b"


def test_429_quota_pause_is_capped_at_one_hour(server, init):
    init()
    server.metrics_script = [quota_response("999999")]
    rec()
    assert insight_sdk.flush(5)
    m = insight_sdk.get_client().performance
    assert 3500 < m.paused_until - time.monotonic() <= 3600


def test_event_quota_pause_does_not_pause_metrics(server, init):
    init()
    server.script = [(429, {"Retry-After": "60"}, {"code": "quota_exceeded", "error": "events"})]
    insight_sdk.capture_message("quota hit")
    assert insight_sdk.flush(5)
    assert insight_sdk.get_client().transport.paused_until > 0
    rec()
    assert insight_sdk.flush(5)
    assert len(server.metrics_requests) == 1  # metrics unaffected by the error-event quota


def test_5xx_keeps_the_window_and_sends_it_with_the_next_flush(server, init):
    init()
    server.metrics_script = [(503, {}, {"error": "down"})]
    rec("GET /a", 5)
    assert insight_sdk.flush(5) is False
    m = insight_sdk.get_client().performance
    assert m.buffered_windows() == 1
    rec("GET /b", 6)
    assert insight_sdk.flush(5)
    first, retry, second = server.metrics
    assert retry["interval_start"] == first["interval_start"] and retry["transactions"] == first["transactions"]
    assert [t["name"] for t in second["transactions"]] == ["GET /b"]
    assert m.buffered_windows() == 0 and m.sent_requests == 2


def test_network_error_keeps_the_window(server, init):
    init()
    server.metrics_script = [None]  # connection dropped
    rec()
    assert insight_sdk.flush(5) is False
    assert insight_sdk.get_client().performance.buffered_windows() == 1
    assert insight_sdk.flush(5)
    assert insight_sdk.get_client().performance.buffered_windows() == 0


def test_at_most_five_windows_are_buffered(server, init):
    init()
    server.metrics_responder = lambda req: (503, {}, {})
    m = insight_sdk.get_client().performance
    for i in range(8):
        rec("GET /w%d" % i, 5)
        assert insight_sdk.flush(5) is False
    assert m.buffered_windows() == 5
    assert m.dropped_requests == 3
    server.metrics_responder = None
    sent_names = []
    assert insight_sdk.flush(5)
    sent_names = [b["transactions"][0]["name"] for b in server.metrics[-5:]]
    assert sent_names == ["GET /w%d" % i for i in range(3, 8)]  # the oldest three were dropped


def test_unexpected_4xx_drops_without_retry(server, init):
    init()
    server.metrics_script = [(404, {}, {})]
    rec()
    assert insight_sdk.flush(5)
    assert len(server.metrics_requests) == 1 and insight_sdk.get_client().performance.buffered_windows() == 0


# -- flushing -------------------------------------------------------------------------------------------------------
def test_flush_interval_is_clamped_and_configurable(server, init):
    c = init(performance_flush_interval=1)
    assert c.performance.flush_interval == 5.0
    c = init(performance_flush_interval=30)
    assert c.performance.flush_interval == 30.0
    assert init().performance.flush_interval == 60.0


def test_background_thread_sends_every_interval(server, init):
    m = init().performance
    m.flush_interval = 0.4  # below the public minimum: only for this test
    rec("GET /a", 5)
    assert wait_for(lambda: len(server.metrics_requests) == 1, 5)
    body = server.metrics[0]
    assert body["transactions"][0]["count"] == 1 and body["interval_seconds"] >= 1
    rec("GET /a", 5)
    rec("GET /a", 6)
    assert wait_for(lambda: len(server.metrics_requests) == 2, 5)
    assert server.metrics[1]["transactions"][0]["count"] == 2
    assert server.metrics_requests[1]["time"] - server.metrics_requests[0]["time"] >= 0.3
    time.sleep(0.9)
    assert len(server.metrics_requests) == 2  # nothing recorded -> nothing sent
    assert any(t.name == "insight-sdk-metrics" and t.daemon for t in threading.enumerate())


def test_background_thread_waits_out_a_rate_limit(server, init):
    m = init().performance
    m.flush_interval = 0.3
    server.metrics_script = [(429, {"Retry-After": "1"}, {"code": "rate_limited"})]
    rec()
    assert wait_for(lambda: len(server.metrics_requests) == 2, 6)
    assert server.metrics_requests[1]["time"] - server.metrics_requests[0]["time"] >= 0.9


def test_background_thread_retries_after_5xx_at_the_next_flush(server, init):
    m = init().performance
    m.flush_interval = 0.4
    server.metrics_script = [(500, {}, {})]
    rec("GET /a", 5)
    assert wait_for(lambda: len(server.metrics_requests) >= 2, 6)
    assert server.metrics[0]["transactions"] == server.metrics[1]["transactions"]


def test_recording_never_blocks_on_a_slow_server(server, init):
    init()
    server.metrics_responder = lambda req: (time.sleep(1) or (201, {}, {}))
    m = insight_sdk.get_client().performance
    m.flush_interval = 0.2
    t0 = time.monotonic()
    for _ in range(2000):
        rec("GET /a", 5)
    assert time.monotonic() - t0 < 0.5


def test_atexit_flush_sends_metrics(server, init):
    init()
    rec("GET /a", 5)
    insight_sdk._flush_at_exit()
    assert len(server.metrics_requests) == 1


def test_close_flushes_metrics(server, init):
    init()
    rec("GET /a", 5)
    insight_sdk.close(5)
    assert len(server.metrics_requests) == 1


def test_endpoint_derivation(server, init):
    assert build_metrics_endpoint("insight.example.com") == "https://insight.example.com/api/ingest/metrics/"
    assert build_metrics_endpoint("https://h.example/api") == "https://h.example/api/ingest/metrics/"
    assert build_metrics_endpoint(endpoint="https://h.example/api/ingest/events/") == "https://h.example/api/ingest/metrics/"
    assert build_metrics_endpoint(endpoint="https://h.example/api/ingest/events") == "https://h.example/api/ingest/metrics/"
    assert build_metrics_endpoint(None, None) is None
    c = insight_sdk.init(api_key="k", endpoint=server.host + "/api/ingest/events/", integrations=[])
    c.performance.record("GET /a", "http.server", 3)
    assert insight_sdk.flush(5)
    insight_sdk.close(1)
    assert server.metrics_requests[0]["path"] == "/api/ingest/metrics/"


# -- on/off, ignore list ----------------------------------------------------------------------------------------------
def test_performance_false_is_a_noop(server, init):
    c = init(performance=False)
    assert c.performance is None
    with insight_sdk.start_transaction("x"):
        pass
    assert perfmod.current_monitor() is None
    assert insight_sdk.flush(5)
    assert server.metrics_requests == []
    assert insight_sdk.capture_message("errors still work") and insight_sdk.flush(5) and len(server.events) == 1


def test_disabled_client_is_a_noop():
    c = insight_sdk.init(api_key=None, host=None, integrations=[])
    try:
        assert not c.enabled and c.performance is None
        with insight_sdk.start_transaction("x"):
            pass
        assert insight_sdk.flush(1)
    finally:
        insight_sdk.close(1)


def test_no_client_at_all_is_a_noop():
    insight_sdk.close(1)
    with insight_sdk.start_transaction("x"):
        pass
    assert perfmod.current_monitor() is None


@pytest.mark.parametrize("path", [
    "/health", "/health/", "/healthz", "/ping", "/status", "/metrics", "/favicon.ico", "/robots.txt", "/ready", "/live",
    "/livez", "/readyz", "/static/app.js", "/static/a/b/c.css", "/assets/logo.png", "/health?x=1",
])
def test_default_ignore_list_matches(path):
    assert compile_ignore(None)(perfmod.path_of(path))


@pytest.mark.parametrize("path", ["/", "/items/1", "/healthy", "/api/health", "/staticfiles/x", "/pings", "/assetsx/1", "/api/status"])
def test_default_ignore_list_does_not_overmatch(path):
    assert not compile_ignore(None)(path)


def test_custom_ignore_list_replaces_defaults(server, init):
    ignored = compile_ignore(["/internal/*", "/skipme", re.compile(r"^/v\d+/debug")])
    assert ignored("/internal/x") and ignored("/skipme") and ignored("/skipme/") and ignored("/v2/debug/x")
    assert not ignored("/health") and not ignored("/internal")
    assert set(DEFAULT_IGNORE) >= {"/health", "/static/*", "/assets/*"}
    m = init(performance_ignore=["/nope"]).performance
    assert m.is_ignored("/nope") and not m.is_ignored("/health")


def wsgi_app(status="200 OK"):
    def app(environ, start_response):
        start_response(status, [("Content-Type", "text/plain")])
        return [b"ok"]

    return app


def call_wsgi(app, path, method="GET"):
    from wsgiref.util import setup_testing_defaults

    environ = {"PATH_INFO": path, "REQUEST_METHOD": method}
    setup_testing_defaults(environ)
    environ["PATH_INFO"] = path
    out = {}
    body = app(environ, lambda status, headers, exc_info=None: out.setdefault("status", status))
    data = b"".join(body)
    if hasattr(body, "close"):
        body.close()
    return out["status"], data


def test_ignored_paths_record_nothing_but_others_do(server, init):
    from insight_sdk.integrations.wsgi import InsightWSGIMiddleware
    from fwutil import assert_perf

    init()
    app = InsightWSGIMiddleware(wsgi_app(), route=lambda env: "/api/things/{id}" if env["PATH_INFO"].startswith("/api/things/") else None)
    for p in ("/health", "/healthz", "/static/x.js", "/assets/y.png", "/favicon.ico", "/robots.txt"):
        call_wsgi(app, p)
    call_wsgi(app, "/api/things/7")
    call_wsgi(app, "/api/things/8")
    assert_perf(server, {"GET /api/things/{id}": (2, 0)})


def test_unmatched_naming_never_uses_the_raw_path(server, init):
    from insight_sdk.integrations.wsgi import InsightWSGIMiddleware
    from fwutil import assert_perf

    init()
    app = InsightWSGIMiddleware(wsgi_app("404 Not Found"))
    for i in range(300):  # scanner traffic
        call_wsgi(app, "/wp-admin/%d.php" % i)
    call_wsgi(app, "/x", method="post")
    assert_perf(server, {"GET <unmatched>": (300, 0), "POST <unmatched>": (1, 0)})


def test_cardinality_safe_with_random_paths_in_one_window(server, init):
    from insight_sdk.integrations.wsgi import InsightWSGIMiddleware

    init()
    app = InsightWSGIMiddleware(wsgi_app("404 Not Found"))
    for i in range(1500):
        call_wsgi(app, "/random/%d" % i)
    (body,) = sent_metrics(server)
    assert [t["name"] for t in body["transactions"]] == ["GET <unmatched>"]
    assert body["transactions"][0]["count"] == 1500


def test_status_semantics_5xx_is_error_4xx_is_not(server, init):
    from insight_sdk.integrations.wsgi import InsightWSGIMiddleware
    from fwutil import assert_perf

    init()
    for status in ("200 OK", "301 Moved", "404 Not Found", "422 Unprocessable", "500 Internal Server Error", "503 Service Unavailable"):
        call_wsgi(InsightWSGIMiddleware(wsgi_app(status), route=lambda e, s=status: "/s/" + s[:3]), "/x")
    assert_perf(server, {"GET /s/200": (1, 0), "GET /s/301": (1, 0), "GET /s/404": (1, 0), "GET /s/422": (1, 0),
                         "GET /s/500": (1, 1), "GET /s/503": (1, 1)})
    samples = server.metrics[0]["samples"]
    assert {s["status_code"] for s in samples} <= {200, 301, 404, 422, 500, 503}
    assert all(s["ok"] == (s["status_code"] < 500) for s in samples)


def test_unhandled_exception_is_an_error_and_reraised(server, init):
    from insight_sdk.integrations.wsgi import InsightWSGIMiddleware
    from fwutil import assert_perf

    init()

    def boom(environ, start_response):
        raise RuntimeError("kaput")

    with pytest.raises(RuntimeError):
        call_wsgi(InsightWSGIMiddleware(boom, route=lambda e: "/boom"), "/boom")
    assert_perf(server, {"GET /boom": (1, 1)})
    assert server.metrics[0]["samples"][0]["status_code"] == 500


def test_one_transaction_per_request_when_layered(server, init):
    from insight_sdk.integrations.wsgi import InsightWSGIMiddleware

    init()
    inner = InsightWSGIMiddleware(wsgi_app(), route=lambda e: "/inner/{id}")
    outer = InsightWSGIMiddleware(inner)  # e.g. a user-added middleware around a framework that has its own
    call_wsgi(outer, "/inner/5")
    call_wsgi(InsightWSGIMiddleware(InsightWSGIMiddleware(inner, route=lambda e: "/mid"), route=lambda e: "/outer"), "/inner/6")
    got = {t["name"]: t["count"] for b in sent_metrics(server) for t in b["transactions"]}
    assert got == {"GET /inner/{id}": 2}, got  # two requests -> two transactions (not four), named by the innermost layer


def test_ignored_outer_layer_makes_inner_layers_skip_too(server, init):
    from insight_sdk.integrations.wsgi import InsightWSGIMiddleware

    init()
    app = InsightWSGIMiddleware(InsightWSGIMiddleware(wsgi_app(), route=lambda e: "/health"))
    call_wsgi(app, "/health")
    assert insight_sdk.flush(5) and server.metrics_requests == []


def test_duration_is_measured_until_the_body_is_consumed(server, init):
    from insight_sdk.integrations.wsgi import InsightWSGIMiddleware

    init()

    def slow_stream(environ, start_response):
        start_response("200 OK", [])

        def gen():
            yield b"a"
            time.sleep(0.15)
            yield b"b"

        return gen()

    call_wsgi(InsightWSGIMiddleware(slow_stream, route=lambda e: "/s"), "/s")
    (body,) = sent_metrics(server)
    assert body["transactions"][0]["min_ms"] >= 140


# -- start_transaction ----------------------------------------------------------------------------------------------
def test_start_transaction_context_manager_and_errors(server, init):
    from fwutil import assert_perf

    init()
    with insight_sdk.start_transaction("import_catalog", op="script"):
        time.sleep(0.02)
    with pytest.raises(ValueError):
        with insight_sdk.start_transaction("import_catalog", op="script"):
            raise ValueError("x")
    with pytest.raises(SystemExit):
        with insight_sdk.start_transaction("exit0", op="script"):
            raise SystemExit(0)
    with pytest.raises(SystemExit):
        with insight_sdk.start_transaction("exit1", op="script"):
            raise SystemExit(1)
    got = {t["name"]: t for b in sent_metrics(server) for t in b["transactions"]}
    assert (got["import_catalog"]["count"], got["import_catalog"]["errors"], got["import_catalog"]["op"]) == (2, 1, "script")
    assert got["import_catalog"]["max_ms"] >= 20
    assert got["exit0"]["errors"] == 0 and got["exit1"]["errors"] == 1


def test_start_transaction_decorator_sync_and_async(server, init):
    init()

    @insight_sdk.start_transaction("nightly", op="cron")
    def nightly(x):
        return x * 2

    @insight_sdk.start_transaction("asy", op="cron")
    async def asy():
        await asyncio.sleep(0.01)
        return "v"

    assert nightly(4) == 8 and nightly.__name__ == "nightly"
    assert asyncio.run(asy()) == "v"
    got = {t["name"]: t["count"] for b in sent_metrics(server) for t in b["transactions"]}
    assert got == {"nightly": 1, "asy": 1}


def test_start_transaction_manual_error_flag(server, init):
    init()
    with insight_sdk.start_transaction("job") as t:
        t.set_error()
    (body,) = sent_metrics(server)
    assert body["transactions"][0]["errors"] == 1


# -- fork safety ----------------------------------------------------------------------------------------------------
@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_fork_child_starts_clean_and_parent_keeps_its_data(server, init):
    m = init().performance
    for _ in range(3):
        rec("GET /parent", 5)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: must not resend the parent's window nor deadlock on a lock held by another thread
        try:
            ok = m._window is None and m.buffered_windows() == 0
            rec("GET /child", 7)
            ok = ok and m.flush(5)
            os.write(w, b"1" if ok else b"0")
        finally:
            os._exit(0)
    os.close(w)
    assert os.read(r, 1) == b"1"
    os.waitpid(pid, 0)
    assert insight_sdk.flush(5)
    by_name = {}
    for b in server.metrics:
        for t in b["transactions"]:
            by_name[t["name"]] = by_name.get(t["name"], 0) + t["count"]
    assert by_name == {"GET /child": 1, "GET /parent": 3}  # each exactly once


def test_pid_check_resets_state_without_fork_hooks(server, init):
    m = init().performance
    rec("GET /a", 5)
    m._pid = -1  # pretend this object was inherited from another process
    rec("GET /b", 5)
    assert insight_sdk.flush(5)
    names = [t["name"] for b in server.metrics for t in b["transactions"]]
    assert names == ["GET /b"]


# -- scope / thread safety ------------------------------------------------------------------------------------------
def test_concurrent_recording_is_exact(server, init):
    init()

    def work():
        for i in range(500):
            rec("GET /c", 1 + i % 50, 500 if i % 25 == 0 else 200, ok=i % 25 != 0)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    got = [t for b in sent_metrics(server) for t in b["transactions"]]
    assert sum(t["count"] for t in got) == 4000 and sum(t["errors"] for t in got) == 8 * 20
    assert sum(sum(t["buckets"].values()) for t in got) == 4000


def test_plain_asgi_middleware_times_http_but_not_websocket(server, init):
    from fwutil import assert_perf
    from insight_sdk.integrations.asgi import InsightASGIMiddleware

    init()

    async def app(scope, receive, send):
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 404 if scope["path"] == "/nope" else 200, "headers": []})
            await send({"type": "http.response.body", "body": b"x"})

    async def call(path, type_="http"):
        mw = InsightASGIMiddleware(app)
        scope = {"type": type_, "method": "GET", "path": path, "headers": [], "query_string": b"", "scheme": "http", "server": ("t", 80)}

        async def receive():
            return {"type": "http.request"}

        async def send(m):
            pass

        await mw(scope, receive, send)

    async def go():
        await call("/a")
        await call("/nope")
        await call("/health")
        await call("/ws", "websocket")

    asyncio.run(go())
    assert_perf(server, {"GET <unmatched>": (2, 0)})  # a bare ASGI app has no route template


def test_rq_horse_flushes_its_metrics_before_exiting(server, init):
    from insight_sdk.integrations.rq import _flush_metrics_in_horse

    init()
    rec("TASK x", 5, None, True, "queue.task")

    class Worker:
        is_horse = False

    _flush_metrics_in_horse(Worker())
    assert server.metrics_requests == []  # the main worker keeps aggregating
    Worker.is_horse = True
    _flush_metrics_in_horse(Worker())
    assert len(server.metrics_requests) == 1
