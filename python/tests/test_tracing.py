"""Distributed tracing: traceparent parsing, adoption, forced samples, outbound propagation, helpers."""
import asyncio
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import insight_sdk
from insight_sdk import performance as perfmod
from insight_sdk import tracing
from insight_sdk.integrations import instrument

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"
TP1 = "00-%s-%s-01" % (TRACE, PARENT)
TP0 = "00-%s-%s-00" % (TRACE, PARENT)
HEX16 = re.compile(r"^[0-9a-f]{16}$")


def all_samples(server):
    assert insight_sdk.flush(5)
    return [s for b in server.metrics for s in b.get("samples", [])]


def sample_named(server, name):
    found = [s for s in all_samples(server) if s["name"] == name]
    assert found, [s["name"] for s in all_samples(server)]
    return found[-1]


@pytest.fixture(autouse=True)
def clean_patches():
    instrument.disable()
    yield
    instrument.disable()


# -- parsing ----------------------------------------------------------------------------------------------------------
def test_parse_valid_and_flags():
    c = tracing.parse_traceparent(TP1)
    assert (c.trace_id, c.parent_span_id, c.sampled) == (TRACE, PARENT, True)
    assert tracing.parse_traceparent(TP0).sampled is False
    assert tracing.parse_traceparent("00-%s-%s-03" % (TRACE, PARENT)).sampled is True  # other flag bits are ignored
    assert tracing.parse_traceparent("00-%s-%s-02" % (TRACE, PARENT)).sampled is False
    assert tracing.parse_traceparent(TP1.encode()).trace_id == TRACE


def test_parse_uppercase_and_whitespace_are_accepted_and_normalised():
    c = tracing.parse_traceparent("  " + TP1.upper() + " \t")
    assert (c.trace_id, c.parent_span_id) == (TRACE, PARENT)


@pytest.mark.parametrize("value", [
    None, "", "   ", 5, [], "garbage", "00-%s-%s" % (TRACE, PARENT),
    "01-%s-%s-01" % (TRACE, PARENT), "ff-%s-%s-01" % (TRACE, PARENT),  # not version 00
    "00-%s-%s-01" % ("0" * 32, PARENT), "00-%s-%s-01" % (TRACE, "0" * 16),  # all-zero ids
    "00-%s-%s-01" % (TRACE[:-1], PARENT), "00-%s-%s-01" % (TRACE + "a", PARENT),  # wrong length
    "00-%s-%s-01" % (TRACE, PARENT[:-1]), "00-%s-%s-1" % (TRACE, PARENT), "00-%s-%s-0g" % (TRACE, PARENT),
    "00-%s-%s-01" % ("z" * 32, PARENT), "00-%s-%s-01-extra" % (TRACE, PARENT), "00_%s_%s_01" % (TRACE, PARENT),
    "00-%s-%s-01" % (TRACE, PARENT) + "x" * 500, "\x00\xff", "00-é-é-01",
])
def test_parse_rejects_malformed_without_raising(value):
    assert tracing.parse_traceparent(value) is None


# -- adoption (transactions) --------------------------------------------------------------------------------------------
def test_continue_trace_adopts_trace_and_samples_carry_ids(server, init):
    init()
    with insight_sdk.continue_trace(TP1, name="job", op="task"):
        tp = insight_sdk.get_traceparent()
        err = insight_sdk.capture_message("inside")
    assert insight_sdk.get_traceparent() is None
    s = sample_named(server, "job")
    assert s["trace_id"] == TRACE and s["parent_span_id"] == PARENT and HEX16.match(s["span_id"])
    assert s["kind"] == "server" and s["op"] == "task"
    assert tp == "00-%s-%s-01" % (TRACE, s["span_id"])
    assert err and [e for e in server.events if e["trace_id"] == TRACE]


def test_new_trace_has_span_id_but_no_parent(server, init):
    init()
    with insight_sdk.start_transaction("plain"):
        tp = insight_sdk.get_traceparent()
    s = sample_named(server, "plain")
    assert "parent_span_id" not in s and HEX16.match(s["span_id"]) and len(s["trace_id"]) == 32
    assert tp == "00-%s-%s-00" % (s["trace_id"], s["span_id"])


def test_continue_trace_with_invalid_header_is_a_new_trace(server, init):
    init()
    with insight_sdk.continue_trace("nonsense", name="j2"):
        assert insight_sdk.get_traceparent().startswith("00-") and TRACE not in insight_sdk.get_traceparent()
    assert "parent_span_id" not in sample_named(server, "j2")


def test_continue_trace_without_name_joins_transactions_and_errors(server, init):
    init()
    with insight_sdk.continue_trace(TP0):
        assert insight_sdk.get_traceparent() == "00-%s-%s-00" % (TRACE, PARENT)  # no transaction: parent is the header's span
        insight_sdk.capture_message("bare")
        with insight_sdk.start_transaction("nested"):
            pass
    assert sample_named(server, "nested")["trace_id"] == TRACE
    assert any(e.get("trace_id") == TRACE for e in server.events)


def test_continue_trace_async_and_never_raises(server, init):
    init()

    async def go():
        async with insight_sdk.continue_trace(TP1, name="aj"):
            return insight_sdk.get_traceparent()

    assert asyncio.run(go()).startswith("00-" + TRACE)
    for bad in (None, 5, "", object()):
        with insight_sdk.continue_trace(bad, name="x"):
            pass


def test_get_traceparent_inside_start_span_uses_the_span_id(server, init):
    init()
    with insight_sdk.start_transaction("sp"):
        with insight_sdk.start_span("http.client", "POST api.example.com") as span:
            tp = insight_sdk.get_traceparent()
            assert span.traceparent == tp
            sid = span.span_id
    assert tp.split("-")[2] == sid and HEX16.match(sid)
    spans = sample_named(server, "sp")["spans"]
    assert [x["span_id"] for x in spans] == [sid]


def test_all_recorded_span_ids_are_16_hex(server, init):
    init()
    with insight_sdk.start_transaction("ids"):
        for i in range(5):
            insight_sdk.record_span("custom", "d%d" % i, time.perf_counter(), 1)
        with insight_sdk.start_span("custom", "own"):
            pass
    ids = [s["span_id"] for s in sample_named(server, "ids")["spans"]]
    assert len(ids) == 6 and len(set(ids)) == 6 and all(HEX16.match(i) for i in ids)


# -- forced samples ---------------------------------------------------------------------------------------------------
def _fill(w, n, sampled, ms, prefix):
    for i in range(n):
        w.add("%s%d" % (prefix, i), "http.server", ms, 200, True, time.time(), "%032x" % (len(prefix) * 1000 + i + (1 if sampled else 2) * 10 ** 6),
              None, "%016x" % (i + 1), PARENT if sampled else None, sampled)


def test_forced_samples_cap_20_in_addition_to_the_usual_ones():
    w = perfmod.Window(time.time(), time.monotonic())
    _fill(w, 30, True, 1.0, "fast")  # fast, successful, sampled flag set
    _fill(w, 100, False, 200.0, "slow")  # slower, not sampled
    assert len(w.forced) == 20
    out = w.samples()
    forced_names = [s for s in out if s["name"].startswith("fast")]
    assert 20 <= len(forced_names) <= 21  # 20 forced (+ possibly the one random pick)
    assert len([s for s in out if s["name"].startswith("slow")]) >= 3  # the slowest three are still there
    assert len(out) <= 20 + perfmod.MAX_SAMPLES
    assert all(s["kind"] == "server" and s["op"] == "http.server" and HEX16.match(s["span_id"]) for s in out)
    assert all(s.get("parent_span_id") == PARENT for s in forced_names)


def test_nothing_forced_without_the_sampled_flag():
    w = perfmod.Window(time.time(), time.monotonic())
    _fill(w, 60, False, 1.0, "fast")
    assert w.forced == []
    assert len(w.samples()) <= 4  # slowest 3 + 1 random


def test_fast_ok_request_is_kept_only_because_of_the_flag(server, init, monkeypatch):
    init()
    monkeypatch.setattr(perfmod.random, "randrange", lambda n: 1)  # the 1 random sample never fires
    for i in range(4):
        with insight_sdk.start_transaction("slow%d" % i):
            time.sleep(0.03)
    with insight_sdk.continue_trace(TP1, name="fast_sampled"):
        pass
    with insight_sdk.continue_trace(TP0, name="fast_unsampled"):
        pass
    names = {s["name"] for s in all_samples(server)}
    assert "fast_sampled" in names and "fast_unsampled" not in names
    assert len({n for n in names if n.startswith("slow")}) == 3  # the usual slowest three


def test_forced_samples_are_the_last_to_go_under_payload_pressure(server, init, monkeypatch):
    init()
    monkeypatch.setattr(perfmod.random, "randrange", lambda n: 1)
    for i in range(4):
        with insight_sdk.start_transaction("reg%d" % i):
            for j in range(100):
                insight_sdk.record_span("custom", "r%d-%d" % (i, j), time.perf_counter(), 1)
    with insight_sdk.continue_trace(TP1, name="forced"):
        for j in range(100):
            insight_sdk.record_span("custom", "f%d" % j, time.perf_counter(), 1)
    m = insight_sdk.get_client().performance
    w = m._window
    w.strip_sample_spans = w.strip_forced_spans = True
    c = len(m._payload(w))
    w.strip_regular_samples = True
    d = len(m._payload(w))
    w.strip_sample_spans = w.strip_forced_spans = w.strip_regular_samples = False
    assert d < c
    monkeypatch.setattr(perfmod, "MAX_BODY_BYTES", (c + d) // 2)
    insight_sdk.flush(5)
    (body,) = server.metrics
    assert [s["name"] for s in body["samples"]] == ["forced"] and "spans" not in body["samples"][0]
    assert body["transactions"]


# -- targets ------------------------------------------------------------------------------------------------------------
def test_target_matching():
    import re as _re

    m = tracing.compile_targets(["api.example.com", "*.internal.io", "localhost:8000", _re.compile(r"^svc-\d+\.local$")])
    for host in ("api.example.com", "API.example.com", "api.example.com:8443", "a.internal.io", "a.b.internal.io:9", "localhost:8000", "svc-12.local", "svc-3.local:81"):
        assert m(host), host
    for host in ("evil-api.example.com", "example.com", "localhost", "localhost:8001", "internal.io", "svc-x.local", "", "api.example.com.evil.io"):
        assert not m(host), host
    assert tracing.compile_targets(()) is None and tracing.compile_targets(None) is None and tracing.compile_targets([""]) is None
    assert tracing.compile_targets("api.example.com")("api.example.com")  # a bare string is one target


# -- outbound propagation ---------------------------------------------------------------------------------------------
class Echo:
    """Local HTTP service that answers with the request headers as JSON."""

    def __init__(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                data = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_POST = do_GET

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.host = "127.0.0.1:%d" % self.port
        self.base = "http://" + self.host
        self.localhost = "http://localhost:%d" % self.port

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def echo():
    e = Echo()
    yield e
    e.stop()


def _sync_requests(url, headers=None):
    import requests

    return requests.get(url, headers=headers).json()


def _sync_httpx(url, headers=None):
    import httpx

    return httpx.get(url, headers=headers).json()


def _sync_urllib3(url, headers=None):
    import urllib3

    return json.loads(urllib3.PoolManager().request("GET", url, headers=headers).data)


def _sync_urllib3_pool(url, headers=None):
    import urllib3

    host, port = url.split("//")[1].split("/")[0].split(":")
    return json.loads(urllib3.HTTPConnectionPool(host, int(port)).request("GET", "/", headers=headers).data)


def _sync_urllib(url, headers=None):
    import urllib.request

    if headers:
        return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=headers)).read())
    return json.loads(urllib.request.urlopen(url).read())


def _sync_urllib_request_object(url, headers=None):
    import urllib.request

    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})).read())


def _async_httpx(url, headers=None):
    import httpx

    async def go():
        async with httpx.AsyncClient() as c:
            return (await c.get(url, headers=headers)).json()

    return asyncio.run(go())


def _async_aiohttp(url, headers=None):
    import aiohttp

    async def go():
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers) as r:
                return await r.json()

    return asyncio.run(go())


CLIENTS = [
    ("requests", "requests", _sync_requests), ("httpx", "httpx", _sync_httpx), ("urllib3", "urllib3", _sync_urllib3),
    ("urllib3", "urllib3", _sync_urllib3_pool), ("urllib", None, _sync_urllib), ("urllib", None, _sync_urllib_request_object),
    ("httpx", "httpx", _async_httpx), ("aiohttp_client", "aiohttp", _async_aiohttp),
]
IDS = ["requests", "httpx", "urllib3-poolmanager", "urllib3-pool", "urllib-str", "urllib-request", "httpx-async", "aiohttp"]


def _run_in_txn(fn, url, headers=None, tp=TP1):
    """Run the call inside a transaction (the async clients inside their own loop context)."""
    box = {}

    def body():
        box["tp"] = insight_sdk.get_traceparent()
        box["echoed"] = fn(url, headers)

    with insight_sdk.continue_trace(tp, name="outbound"):
        body()
    return box


@pytest.mark.parametrize("integration,module,fn", CLIENTS, ids=IDS)
def test_outbound_header_to_matching_host_ties_to_the_http_client_span(server, init, echo, integration, module, fn):
    if module:
        pytest.importorskip(module)
    init(integrations=[integration], trace_propagation_targets=["127.0.0.1:%d" % echo.port])
    box = _run_in_txn(fn, echo.base + "/ok")
    header = box["echoed"].get("traceparent")
    assert header, box["echoed"]
    m = re.match(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-(0[01])$", header)
    assert m and m.group(1) == TRACE and m.group(3) == "01"  # the sampled bit is repeated
    s = sample_named(server, "outbound")
    (client_span,) = [x for x in s["spans"] if x["op"] == "http.client"]
    assert client_span["span_id"] == m.group(2) and client_span["description"] == "GET " + echo.host
    assert m.group(2) != s["span_id"]


@pytest.mark.parametrize("integration,module,fn", CLIENTS, ids=IDS)
def test_outbound_flags_00_when_the_trace_is_not_sampled(server, init, echo, integration, module, fn):
    if module:
        pytest.importorskip(module)
    init(integrations=[integration], trace_propagation_targets=["127.0.0.1"])  # host without port matches any port
    box = _run_in_txn(fn, echo.base + "/ok", tp=TP0)
    assert box["echoed"]["traceparent"].startswith("00-%s-" % TRACE) and box["echoed"]["traceparent"].endswith("-00")


@pytest.mark.parametrize("integration,module,fn", CLIENTS, ids=IDS)
def test_outbound_never_to_non_matching_hosts(server, init, echo, integration, module, fn):
    if module:
        pytest.importorskip(module)
    init(integrations=[integration], trace_propagation_targets=["127.0.0.1:%d" % echo.port, "*.example.com"])
    assert "traceparent" not in _run_in_txn(fn, echo.localhost + "/ok")["echoed"]  # other host name, same server
    init(integrations=[integration], trace_propagation_targets=["127.0.0.1:1"])
    assert "traceparent" not in _run_in_txn(fn, echo.base + "/ok")["echoed"]  # other port


@pytest.mark.parametrize("integration,module,fn", CLIENTS, ids=IDS)
def test_outbound_default_is_no_propagation(server, init, echo, integration, module, fn):
    if module:
        pytest.importorskip(module)
    init(integrations=[integration])
    assert "traceparent" not in _run_in_txn(fn, echo.base + "/ok")["echoed"]


@pytest.mark.parametrize("integration,module,fn", CLIENTS, ids=IDS)
def test_outbound_existing_traceparent_is_left_alone(server, init, echo, integration, module, fn):
    if module:
        pytest.importorskip(module)
    init(integrations=[integration], trace_propagation_targets=["127.0.0.1"])
    mine = "00-%s-%s-00" % ("a" * 32, "b" * 16)
    box = _run_in_txn(fn, echo.base + "/ok", headers={"Traceparent": mine})
    assert box["echoed"]["traceparent"] == mine


@pytest.mark.parametrize("integration,module,fn", CLIENTS, ids=IDS)
def test_outbound_no_header_without_a_transaction(server, init, echo, integration, module, fn):
    if module:
        pytest.importorskip(module)
    init(integrations=[integration], trace_propagation_targets=["127.0.0.1"])
    assert "traceparent" not in fn(echo.base + "/ok", None)
    with insight_sdk.start_transaction("done"):
        pass
    assert "traceparent" not in fn(echo.base + "/ok", None)  # the transaction has ended


def test_outbound_started_trace_propagates_a_new_trace_id(server, init, echo):
    pytest.importorskip("requests")
    init(integrations=["requests"], trace_propagation_targets=["127.0.0.1"])
    with insight_sdk.start_transaction("origin"):
        echoed = _sync_requests(echo.base + "/ok")
    s = sample_named(server, "origin")
    assert echoed["traceparent"].split("-")[1] == s["trace_id"] and echoed["traceparent"].endswith("-00")


def test_outbound_never_to_the_insight_endpoint_or_the_sdk_own_calls(server, init, echo):
    requests = pytest.importorskip("requests")
    init(integrations=["requests", "urllib"], trace_propagation_targets=["127.0.0.1", "localhost"])  # matches the fake Insight server too
    with insight_sdk.start_transaction("t"):
        requests.post(server.host + "/api/ingest/events/", json={"message": "x"})
        from insight_sdk.spans import suppressed

        with suppressed():
            echoed = _sync_requests(echo.base + "/ok")
        ok = _sync_requests(echo.base + "/ok")
    assert all("traceparent" not in r["headers"] for r in server.requests)
    assert "traceparent" not in echoed and "traceparent" in ok


def test_outbound_url_userinfo_and_query_do_not_confuse_matching(server, init, echo):
    requests = pytest.importorskip("requests")
    init(integrations=["requests"], trace_propagation_targets=["127.0.0.1:%d" % echo.port])
    with insight_sdk.start_transaction("t"):
        assert "traceparent" in requests.get("http://user:pw@%s/a?x=1#f" % echo.host).json()
        assert "traceparent" not in requests.get("http://localhost:%d/a?next=%s" % (echo.port, echo.host)).json()


def test_outbound_regex_targets(server, init, echo):
    requests = pytest.importorskip("requests")
    init(integrations=["requests"], trace_propagation_targets=[re.compile(r"^127\.0\.0\.1:\d+$")])
    with insight_sdk.start_transaction("t"):
        assert "traceparent" in requests.get(echo.base + "/").json()


def test_outbound_never_raises_when_injection_breaks(server, init, echo, monkeypatch):
    requests = pytest.importorskip("requests")
    init(integrations=["requests", "urllib"], trace_propagation_targets=["127.0.0.1"])

    def boom(*a, **k):
        raise RuntimeError("tracing broke")

    monkeypatch.setattr(instrument, "outbound_context", boom)
    with insight_sdk.start_transaction("t"):
        assert requests.get(echo.base + "/").status_code == 200
        assert _sync_urllib(echo.base + "/")


def test_outbound_needs_the_http_instrumentation(server, init, echo):
    requests = pytest.importorskip("requests")
    init(integrations=["requests"], performance_spans=False, trace_propagation_targets=["127.0.0.1"])  # no span patches at all
    with insight_sdk.start_transaction("t"):
        assert "traceparent" not in requests.get(echo.base + "/").json()
    init(integrations=[], trace_propagation_targets=["127.0.0.1"])  # integrations=[] disables the patches too
    with insight_sdk.start_transaction("t"):
        assert "traceparent" not in requests.get(echo.base + "/").json()


def test_urllib3_under_requests_gets_one_header_and_one_span(server, init, echo):
    requests = pytest.importorskip("requests")
    init(integrations=["requests", "urllib3"], trace_propagation_targets=["127.0.0.1"])
    with insight_sdk.start_transaction("t"):
        header = requests.get(echo.base + "/").json()["traceparent"]
    spans = [s for s in sample_named(server, "t")["spans"] if s["op"] == "http.client"]
    assert len(spans) == 1 and spans[0]["span_id"] == header.split("-")[2]


# -- inbound: garbage never raises -------------------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["", "zz", "x" * 5000, "00-" + "g" * 32 + "-" + "1" * 16 + "-01", "éè", "00-%s-%s-01" % ("0" * 32, PARENT), TP1 + ", " + TP0])
def test_garbage_traceparent_never_raises_and_starts_a_new_trace(server, init, value):
    flask = pytest.importorskip("flask")
    from insight_sdk.integrations.flask import init_app

    init(integrations=["flask"])
    app = flask.Flask("g")

    @app.route("/ok")
    def ok():
        return "ok"

    init_app(app)
    assert app.test_client().get("/ok", headers={"traceparent": value}).status_code == 200
    s = sample_named(server, "GET /ok")
    assert s["trace_id"] != TRACE and "parent_span_id" not in s and HEX16.match(s["span_id"])


def test_wsgi_environ_header_and_job_helper(server, init):
    from insight_sdk.integrations._common import begin_job, end_request
    from insight_sdk.integrations.wsgi import InsightWSGIMiddleware
    init()

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    from wsgiref.util import setup_testing_defaults

    wrapped = InsightWSGIMiddleware(app)
    environ = {"PATH_INFO": "/w", "REQUEST_METHOD": "GET", "HTTP_TRACEPARENT": TP1}
    setup_testing_defaults(environ)
    environ["PATH_INFO"] = "/w"
    body = wrapped(environ, lambda status, headers, exc_info=None: None)
    b"".join(body)
    body.close()
    s = [x for x in all_samples(server) if x["name"].endswith(" /w") or "<unmatched>" in x["name"] or x["name"].startswith("GET")][-1]
    assert s["trace_id"] == TRACE and s["parent_span_id"] == PARENT and s["op"] == "http.server"
    cm = begin_job("celery", {}, None, None, task="t.task", traceparent=TP1)
    assert cm.txn.trace_id == TRACE and cm.txn.parent_span_id == PARENT and cm.txn.sampled and cm.txn.op == "queue.task"
    end_request(cm)
    s = sample_named(server, "TASK t.task")
    assert s["op"] == "queue.task" and s["parent_span_id"] == PARENT and s["kind"] == "server"


def test_celery_traceparent_reader():
    from types import SimpleNamespace

    pytest.importorskip("celery")
    from insight_sdk.integrations.celery import _traceparent

    assert _traceparent(SimpleNamespace(request=SimpleNamespace(traceparent=TP1))) == TP1
    assert _traceparent(SimpleNamespace(request=SimpleNamespace(headers={"traceparent": TP1}))) == TP1
    assert _traceparent(SimpleNamespace(request=None)) is None and _traceparent(None) is None
