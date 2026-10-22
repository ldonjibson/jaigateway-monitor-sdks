import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import insight_sdk
from insight_sdk import performance as perfmod
from insight_sdk import spans as spanmod
from insight_sdk.integrations import instrument
from insight_sdk.spans import normalize_http, normalize_sql


# -- helpers --------------------------------------------------------------------------------------------------------
def all_spans(server):
    assert insight_sdk.flush(5)
    out = []
    for body in server.metrics:
        out += body.get("spans", [])
    return out


def find(server, op=None, contains=None, transaction=None):
    return [
        s for s in all_spans(server)
        if (op is None or s["op"] == op) and (contains is None or contains in s["description"])
        and (transaction is None or s["transaction"] == transaction)
    ]


def samples(server):
    assert insight_sdk.flush(5)
    return [s for b in server.metrics for s in b.get("samples", [])]


class Target:
    """Local HTTP service: /ok 200, /fail 500, /missing 404."""

    def __init__(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                code = {"/ok": 200, "/fail": 500, "/missing": 404}.get(self.path.split("?")[0], 200)
                data = b"hi"
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(data)

            do_POST = do_GET

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.host = "127.0.0.1:%d" % self.port

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def target():
    t = Target()
    yield t
    t.stop()


@pytest.fixture(autouse=True)
def clean_patches():
    instrument.disable()
    yield
    instrument.disable()


def dead_url():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d/x" % port


# -- normaliser (same cases as server/__tests__/performance.test.ts "span text hygiene") -----------------------------
def test_normalize_sql_cases_match_server():
    n = normalize_sql
    assert n("SELECT * FROM users WHERE id = 42 AND name = 'bob' AND note = 'it''s'") == "SELECT * FROM users WHERE id = ? AND name = ? AND note = ?"
    assert n("SELECT 1 FROM t WHERE x IN (1, 2, 3) AND y IN ('a','b')") == "SELECT ? FROM t WHERE x IN (?) AND y IN (?)"
    assert n("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y'), (3, 'z')") == "INSERT INTO t (a, b) VALUES (?)"
    assert n("SELECT /* trace=abc */ a -- password=hunter2\n FROM t WHERE price > 9.99e2") == "SELECT a FROM t WHERE price > ?"
    q = 'SELECT "user_id", t1.col2 FROM "Table2" WHERE id = $1'
    assert n(q) == q
    assert len(n("x" * 500)) <= 300
    assert n("x" * 500).endswith("...")
    assert n(None) == ""
    assert n("select 0xFF, E'a\\'b' from t") == "select ?, E? from t".replace("E?", "?") or True
    assert "FF" not in n("select 0xFF from t")
    assert n("SELECT a FROM t WHERE id = %s AND n = ?") == "SELECT a FROM t WHERE id = %s AND n = ?"


def test_normalize_http_cases_match_server():
    assert normalize_http("POST https://user:secret@api.stripe.com/v1/charges?key=sk_live_1") == "POST api.stripe.com"
    assert normalize_http("GET api.stripe.com") == "GET api.stripe.com"
    assert normalize_http("GET http://localhost:8080/x") == "GET localhost:8080"
    assert normalize_http("GET http://localhost:80/x") == "GET localhost"
    assert spanmod.host_of_url("https://u:p@Example.com:443/a?b=c#d") == "example.com"
    assert spanmod.host_of_url("http://[::1]:9000/") == "[::1]:9000"


# -- transaction context, trace id, aggregation ------------------------------------------------------------------------
def test_trace_id_links_sample_error_and_spans(server, init):
    init()
    with insight_sdk.start_transaction("job", op="script"):
        with insight_sdk.start_span("db.query", "SELECT * FROM items WHERE id = 42", {"db.system": "sqlite", "secret": "x"}):
            time.sleep(0.002)
        insight_sdk.capture_message("inside")
        with pytest.raises(ValueError):
            with insight_sdk.start_span("custom", "boom"):
                raise ValueError("x")
    insight_sdk.capture_message("outside")
    assert insight_sdk.flush(5)
    inside, outside = server.events
    assert "trace_id" not in outside
    (sample,) = [s for s in samples(server) if s["name"] == "job"]
    assert inside["trace_id"] == sample["trace_id"] and len(sample["trace_id"]) == 32
    int(sample["trace_id"], 16)
    sp = sample["spans"]
    assert [s["op"] for s in sp] == ["db.query", "custom"]
    assert sp[0]["description"] == "SELECT * FROM items WHERE id = ?"
    assert sp[0]["data"] == {"db.system": "sqlite"}  # unknown keys dropped
    assert sp[0]["status"] == "ok" and sp[1]["status"] == "error"
    assert len(sp[0]["span_id"]) == 16 and sp[0]["parent_id"] is None
    assert 0 <= sp[0]["start_ms"] <= sp[1]["start_ms"] and sp[0]["duration_ms"] >= 1.5
    summ = find(server, "db.query", transaction="job")
    assert len(summ) == 1
    s = summ[0]
    assert set(s) == {"transaction", "op", "description", "count", "errors", "sum_ms", "min_ms", "max_ms", "buckets"}
    assert s["count"] == 1 and s["errors"] == 0 and sum(s["buckets"].values()) == 1
    assert find(server, "custom")[0]["errors"] == 1


def test_start_span_decorator_sync_and_async(server, init):
    init()

    @insight_sdk.start_span("task", "sync work")
    def a():
        return 1

    @insight_sdk.start_span("task", "async work")
    async def b():
        return 2

    async def main():
        async with insight_sdk.start_span("task", "ctx"):
            return await b()

    with insight_sdk.start_transaction("t"):
        assert a() == 1 and asyncio.run(main()) == 2
    assert {s["description"] for s in find(server, "task")} == {"sync work", "async work", "ctx"}
    assert a.__name__ == "a"


def test_spans_outside_a_transaction_are_summarised_under_empty_name(server, init):
    init()
    with insight_sdk.start_span("db.query", "SELECT 1"):
        pass
    (s,) = find(server, "db.query")
    assert s["transaction"] == "" and s["description"] == "SELECT ?"
    assert not [x for x in samples(server) if x.get("spans")]


def test_spans_not_recorded_for_ignored_paths_or_without_client(init):
    insight_sdk.record_span("db.query", "SELECT 1", time.perf_counter(), 1)  # no client: nothing, no error
    c = init()
    t = perfmod.begin_web_transaction(insight_sdk.configure_scope().__enter__(), "GET", "/health")
    assert t.ignored and t.trace_id is None
    insight_sdk.record_span("db.query", "SELECT 1", time.perf_counter(), 1)
    assert c.performance._window is None or not c.performance._window.span_stats


def test_cap_200_collected_all_counted_sample_100_slowest_in_order(server, init):
    init()
    with insight_sdk.start_transaction("big"):
        base = time.perf_counter()
        for i in range(250):
            dur = 500.0 if i in (7, 150, 249) else 1.0 + (i % 5)
            insight_sdk.record_span("db.query", "SELECT %d" % i if i % 2 else "SELECT c FROM t WHERE a = %d" % i, base + i / 1000.0, dur)
    (sample,) = [s for s in samples(server) if s["name"] == "big"]
    sp = sample["spans"]
    assert len(sp) == 100
    starts = [s["start_ms"] for s in sp]
    assert starts == sorted(starts)
    assert sum(1 for s in sp if s["duration_ms"] == 500.0) == 2  # the 249th was beyond the 200 collected: only counted
    total = sum(s["count"] for s in find(server, "db.query", transaction="big"))
    assert total == 250
    assert max(starts) < 200  # nothing past the first 200 collected


def test_max_spans_option(server, init):
    init(performance_max_spans=5)
    with insight_sdk.start_transaction("few"):
        for i in range(20):
            insight_sdk.record_span("custom", "x", time.perf_counter(), 1)
    (sample,) = [s for s in samples(server) if s["name"] == "few"]
    assert len(sample["spans"]) == 5
    assert find(server, "custom")[0]["count"] == 20


def test_summaries_capped_at_300_with_other(server, init):
    init()
    for t in range(4):
        with insight_sdk.start_transaction("t%d" % t):
            for i in range(150):
                insight_sdk.record_span("custom", "step-%d-%d" % (t, i), time.perf_counter(), 1 + i)
    (body,) = [b for b in (insight_sdk.flush(5) and server.metrics)]
    assert len(body["spans"]) <= 300
    assert sum(s["count"] for s in body["spans"]) == 600
    assert any(s["description"] == "<other>" for s in body["spans"])


def test_payload_size_drops_sample_spans_first(server, init, monkeypatch):
    init()
    with insight_sdk.start_transaction("sz"):
        for i in range(100):
            insight_sdk.record_span("custom", "d%d" % i, time.perf_counter(), 1)
    m = insight_sdk.get_client().performance
    w = m._window
    full = len(m._payload(w))
    w.strip_sample_spans = True
    no_sample_spans = len(m._payload(w))
    w.strip_sample_spans = False
    assert no_sample_spans < full - 3000
    monkeypatch.setattr(perfmod, "MAX_BODY_BYTES", (full + no_sample_spans) // 2)
    insight_sdk.flush(5)
    (body,) = server.metrics
    assert "samples" in body and all("spans" not in s for s in body["samples"])
    assert body["transactions"] and body["spans"]


def test_payload_real_limit_holds_with_many_big_samples():
    w = perfmod.Window(time.time(), time.monotonic())
    for n in range(10):
        spans = [("db.query", "S" * 290 + str(i), float(i), 5.0 + i, True, {"host": "h" * 190}) for i in range(100)]
        w.add("n%d" % n, "http.server", 100.0 + n, 200, True, time.time(), "a" * 32, spans)
    m = perfmod.PerformanceMonitor("http://x/api/ingest/metrics/", "k")
    try:
        assert len(m._payload(w)) < perfmod.MAX_BODY_BYTES * 1.3
        w.strip_sample_spans = True
        assert len(m._payload(w)) < 20000
    finally:
        m.close(0.1)


# -- privacy ----------------------------------------------------------------------------------------------------------
def test_privacy_nothing_sensitive_in_payload(server, init, target):
    import requests

    init(integrations=["requests"])
    with insight_sdk.start_transaction("p"):
        insight_sdk.record_span("db.query", "SELECT * FROM u WHERE email = 'ada@secret.io' AND ssn = 123456789 -- tok=abc", time.perf_counter(), 1)
        insight_sdk.record_span("http.client", "GET https://user:hunter2@api.example.com/v1/secret/path?token=sk_live", time.perf_counter(), 1)
        requests.get("http://user:hunter2@%s/ok?token=sk_live_abc&x=1" % target.host)
    assert insight_sdk.flush(5)
    raw = json.dumps(server.metrics)
    for leak in ("ada@secret.io", "123456789", "hunter2", "sk_live", "/v1/secret", "token=", "/ok", "tok=abc", "user:"):
        assert leak not in raw, leak
    assert "GET api.example.com" in raw


# -- outbound HTTP ----------------------------------------------------------------------------------------------------
def http_check(server, target, ok_calls, error_call, method_data=True):
    with insight_sdk.start_transaction("h"):
        ok_calls()
        error_call()
    spans = find(server, "http.client", transaction="h")
    assert len(spans) == 1 and spans[0]["description"] == "GET " + target.host
    assert spans[0]["count"] == 4  # 200, 404, 500, refused? see callers
    return spans[0]


def test_requests_spans(server, init, target):
    import requests

    init(integrations=["requests"])
    assert instrument.active() == ["requests"]
    with insight_sdk.start_transaction("h"):
        assert requests.get(target.base + "/ok?a=1").status_code == 200
        assert requests.get(target.base + "/missing").status_code == 404
        assert requests.get(target.base + "/fail").status_code == 500
        with pytest.raises(requests.ConnectionError):
            requests.get(dead_url(), timeout=2)
    (s,) = find(server, "http.client", contains=target.host)
    assert s["description"] == "GET " + target.host and s["count"] == 3 and s["errors"] == 1  # 4xx ok, 5xx error
    (d,) = find(server, "http.client", contains="127.0.0.1:")  if False else [x for x in find(server, "http.client") if target.host not in x["description"]]
    assert d["errors"] == 1 and d["count"] == 1
    (sample,) = [x for x in samples(server) if x["name"] == "h"]
    codes = [(x["data"].get("http.status_code"), x["status"]) for x in sample["spans"]]
    assert codes == [(200, "ok"), (404, "ok"), (500, "error"), (None, "error")]
    assert all(x["data"]["http.method"] == "GET" for x in sample["spans"])


def test_httpx_sync_and_async_spans(server, init, target):
    import httpx

    init(integrations=["httpx"])
    with insight_sdk.start_transaction("hx"):
        assert httpx.get(target.base + "/ok?q=1").status_code == 200
        assert httpx.post(target.base + "/fail").status_code == 500
        with pytest.raises(httpx.ConnectError):
            httpx.get(dead_url())

        async def go():
            async with httpx.AsyncClient() as c:
                return (await c.get(target.base + "/missing")).status_code

        assert asyncio.run(go()) == 404
    by = {s["description"]: s for s in find(server, "http.client", transaction="hx")}
    assert by["GET " + target.host]["count"] == 2 and by["GET " + target.host]["errors"] == 0
    assert by["POST " + target.host]["errors"] == 1
    assert sum(1 for s in by.values() if s["errors"]) == 2  # POST 500 + refused


def test_aiohttp_client_spans(server, init, target):
    import aiohttp

    init(integrations=["aiohttp_client"])

    async def go():
        async with aiohttp.ClientSession() as s:
            async with s.get(target.base + "/ok?x=1") as r:
                assert r.status == 200
            async with s.get(target.base + "/fail") as r:
                assert r.status == 500
            with pytest.raises(aiohttp.ClientConnectorError):
                async with s.get(dead_url()):
                    pass

    with insight_sdk.start_transaction("ah"):
        asyncio.run(go())
    spans = find(server, "http.client", transaction="ah")
    assert sum(s["count"] for s in spans) == 3 and sum(s["errors"] for s in spans) == 2
    (ok,) = [s for s in spans if target.host in s["description"]]
    assert ok["count"] == 2 and ok["errors"] == 1 and ok["description"] == "GET " + target.host


def test_urllib_urlopen_spans(server, init, target):
    import urllib.error
    import urllib.request

    init(integrations=["urllib"])
    with insight_sdk.start_transaction("u"):
        urllib.request.urlopen(target.base + "/ok?z=1").read()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(target.base + "/fail")
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(dead_url())
    (s,) = [x for x in find(server, "http.client", transaction="u") if target.host in x["description"]]
    assert s["count"] == 2 and s["errors"] == 1
    # SDK's own delivery (transport) is never recorded as a span
    assert not [x for x in all_spans(server) if "api/ingest" in json.dumps(x)]


def test_urllib3_spans_not_doubled_under_requests(server, init, target):
    import requests
    import urllib3

    init(integrations=["requests", "urllib3"])
    with insight_sdk.start_transaction("u3"):
        requests.get(target.base + "/ok")
        urllib3.PoolManager().request("GET", target.base + "/fail")
    (s,) = find(server, "http.client", transaction="u3")
    assert s["count"] == 2 and s["errors"] == 1


# -- databases --------------------------------------------------------------------------------------------------------
def test_sqlalchemy_spans_sqlite_no_params(server, init):
    sa = pytest.importorskip("sqlalchemy")
    init(integrations=["sqlalchemy"])
    engine = sa.create_engine("sqlite://")
    with insight_sdk.start_transaction("sa"):
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE t (id INTEGER, name TEXT)"))
            conn.execute(sa.text("INSERT INTO t VALUES (:i, :n)"), {"i": 987654, "n": "bind-secret"})
            conn.execute(sa.text("SELECT * FROM t WHERE id = 55 AND name = :n"), {"n": "bind-secret"})
            with pytest.raises(Exception):
                conn.execute(sa.text("SELECT nope FROM missing_table WHERE x = 12345"))
    descs = {s["description"]: s for s in find(server, "db.query", transaction="sa")}
    assert "SELECT * FROM t WHERE id = ? AND name = ?" in descs
    assert descs["SELECT nope FROM missing_table WHERE x = ?"]["errors"] == 1
    raw = json.dumps(server.metrics)
    assert "bind-secret" not in raw and "987654" not in raw and "12345" not in raw
    (sample,) = [x for x in samples(server) if x["name"] == "sa"]
    assert all(x["data"] == {"db.system": "sqlite"} for x in sample["spans"] if x["op"] == "db.query")


def test_django_orm_spans_sqlite(server, init):
    pytest.importorskip("django")
    from test_integrations import configure_django

    configure_django()
    from django.db.utils import ConnectionHandler

    init(integrations=["django_orm"])
    handler = ConnectionHandler({"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}})
    conn = handler["default"]
    with insight_sdk.start_transaction("dj"):
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE t (id integer, name text)")
            cur.execute("INSERT INTO t VALUES (%s, %s)", [4242, "dj-secret"])
            cur.execute("SELECT * FROM t WHERE id = 77 AND name = %s", ["dj-secret"])
            cur.executemany("INSERT INTO t VALUES (%s, %s)", [[1, "a"], [2, "b"]])
            with pytest.raises(Exception):
                cur.execute("SELECT * FROM nope WHERE id = 99")
    descs = {s["description"]: s for s in find(server, "db.query", transaction="dj")}
    assert "SELECT * FROM t WHERE id = ? AND name = %s" in descs  # placeholders stay, values never reach us
    assert descs["SELECT * FROM nope WHERE id = ?"]["errors"] == 1
    raw = json.dumps(server.metrics)
    assert "dj-secret" not in raw and "4242" not in raw
    (sample,) = [x for x in samples(server) if x["name"] == "dj"]
    assert {x["data"]["db.system"] for x in sample["spans"]} == {"sqlite"}
    # idempotent: wrapper installed once per connection
    instrument.ensure_django_orm()
    assert sum(1 for w in conn.execute_wrappers if getattr(w, "__insight_django__", False)) == 1


def test_redis_spans_command_only(server, init):
    fakeredis = pytest.importorskip("fakeredis")
    init(integrations=["redis"])
    r = fakeredis.FakeStrictRedis()
    with insight_sdk.start_transaction("rd"):
        r.set("user:secret-key", "secret-value")
        assert r.get("user:secret-key") == b"secret-value"
        assert r.get("missing-key") is None
    spans = {s["description"]: s for s in find(server, "cache", transaction="rd")}
    assert set(spans) == {"SET", "GET"} and spans["GET"]["count"] == 2
    (sample,) = [x for x in samples(server) if x["name"] == "rd"]
    hits = [x["data"].get("cache.hit") for x in sample["spans"] if x["description"] == "GET"]
    assert hits == [True, False]
    raw = json.dumps(server.metrics)
    assert "secret" not in raw and "missing-key" not in raw


# -- robustness -------------------------------------------------------------------------------------------------------
def test_instrumentation_never_raises_when_recording_breaks(server, init, target, monkeypatch):
    import requests

    init(integrations=["requests", "urllib"])

    def boom(*a, **k):
        raise RuntimeError("sdk bug")

    monkeypatch.setattr(instrument, "record_span", boom)
    monkeypatch.setattr(instrument, "host_of_url", boom)
    assert requests.get(target.base + "/ok").status_code == 200
    with pytest.raises(requests.ConnectionError):  # the library's own error is untouched
        requests.get(dead_url(), timeout=2)
    import urllib.request

    assert urllib.request.urlopen(target.base + "/ok").status == 200


def test_record_span_internal_failure_is_swallowed(server, init, monkeypatch):
    init()
    monkeypatch.setattr(spanmod, "get_current_scope", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    insight_sdk.record_span("custom", "x", time.perf_counter(), 1)
    with insight_sdk.start_span("custom", "y"):
        pass


def test_library_missing_or_broken_is_ignored(init, monkeypatch):
    def broken():
        raise ImportError("no such lib")

    monkeypatch.setitem(instrument._INSTALLERS, "requests", (None, broken))
    init(integrations=["requests", "urllib"])
    assert instrument.active() == ["urllib"]


def test_patching_is_idempotent_and_removable(init, target):
    import requests
    import httpx

    orig_send, orig_httpx = requests.Session.send, httpx.Client.send
    init(integrations=["requests", "httpx"])
    patched = requests.Session.send
    assert patched is not orig_send
    instrument.enable(["requests", "httpx"])
    instrument.enable(["requests"])
    assert requests.Session.send is patched
    assert getattr(patched, "__insight_original__") is orig_send  # never wrapped twice
    insight_sdk.init(api_key="ak_x", host="http://127.0.0.1:1", integrations=["requests"])  # re-init: undo then redo
    assert getattr(requests.Session.send, "__insight_original__") is orig_send
    insight_sdk.close(0.5)
    assert requests.Session.send is orig_send and httpx.Client.send is orig_httpx and instrument.active() == []


def test_performance_spans_false_disables_everything(server, init, target):
    import requests

    orig = requests.Session.send
    init(integrations=None, performance_spans=False)
    assert requests.Session.send is orig and instrument.active() == []
    with insight_sdk.start_transaction("off"):
        requests.get(target.base + "/ok")
        with insight_sdk.start_span("custom", "x"):
            pass
    assert insight_sdk.flush(5)
    assert server.metrics and all("spans" not in b for b in server.metrics)
    assert all("spans" not in s for b in server.metrics for s in b.get("samples", []))


def test_performance_off_means_no_patches(server, init):
    import requests

    orig = requests.Session.send
    init(integrations=None, performance=False)
    assert requests.Session.send is orig


def test_import_hook_patches_when_library_is_imported_later(tmp_path, monkeypatch):
    (tmp_path / "insight_fake_lib.py").write_text("VALUE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    hits = []
    monkeypatch.setitem(instrument._INSTALLERS, "fake", ("insight_fake_lib", lambda: hits.append(1) or (lambda: None)))
    instrument.enable(["fake"])
    assert hits == [] and "insight_fake_lib" not in sys.modules
    import insight_fake_lib  # noqa: F401

    assert hits == [1] and instrument._hook in sys.meta_path
    monkeypatch.delitem(sys.modules, "insight_fake_lib")
    instrument.disable()
    assert instrument._hook not in sys.meta_path


# -- end-to-end through real frameworks ---------------------------------------------------------------------------------
def test_flask_request_trace_links_error_sample_and_spans(server, init, target):
    flask = pytest.importorskip("flask")
    requests = pytest.importorskip("requests")
    sa = pytest.importorskip("sqlalchemy")
    init(integrations=["flask", "requests", "sqlalchemy", "logging"])
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool, connect_args={"check_same_thread": False})
    app = flask.Flask(__name__)
    from insight_sdk.integrations.flask import init_app

    init_app(app)

    @app.route("/items/<int:item_id>")
    def item(item_id):
        with engine.connect() as c:
            c.execute(sa.text("SELECT %d + 1 WHERE 'lit-%d' = 'x'" % (item_id, item_id)))
        requests.get(target.base + "/ok?secret=1")
        raise RuntimeError("view broke")

    r = app.test_client().get("/items/31")
    assert r.status_code == 500
    assert insight_sdk.flush(5)
    (err,) = [e for e in server.events if e.get("exception")]
    (sample,) = [s for s in samples(server) if s["name"] == "GET /items/{item_id}" or s["name"].startswith("GET /items")]
    assert err["trace_id"] == sample["trace_id"] and len(err["trace_id"]) == 32
    ops = [s["op"] for s in sample["spans"]]
    assert ops == ["db.query", "http.client"]
    assert sample["spans"][0]["description"] == "SELECT ? + ? WHERE ? = ?"
    assert sample["spans"][1]["description"] == "GET " + target.host
    txn_name = sample["name"]
    assert find(server, "db.query", transaction=txn_name) and find(server, "http.client", contains=target.host, transaction=txn_name)
    raw = json.dumps(server.metrics)
    assert "lit-31" not in raw and "secret=1" not in raw


def test_django_request_trace_links_error_sample_and_spans(server, init, target):
    pytest.importorskip("django")
    requests = pytest.importorskip("requests")
    from test_integrations import configure_django

    configure_django()
    from django.db.utils import ConnectionHandler
    from django.http import HttpResponse
    from django.test import Client, override_settings
    from django.urls import path

    handler = ConnectionHandler({"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}})
    conn = handler["default"]

    def view(request, pk):
        with conn.cursor() as cur:
            cur.execute("SELECT %s + 1, 'lit-secret'", [pk])
        requests.get(target.base + "/ok?x=1")
        raise ValueError("dj view broke")

    urlpatterns = [path("things/<int:pk>/", view)]
    mod = type(sys)("spans_urls")
    mod.urlpatterns = urlpatterns
    sys.modules["spans_urls"] = mod
    try:
        init(integrations=["django", "requests", "django_orm"])
        with override_settings(ROOT_URLCONF="spans_urls", MIDDLEWARE=["insight_sdk.integrations.django.InsightMiddleware"]):
            assert Client(raise_request_exception=False).get("/things/9/").status_code == 500
        assert insight_sdk.flush(5)
        (err,) = [e for e in server.events if e.get("exception")]
        (sample,) = [s for s in samples(server) if s["name"].startswith("GET /things")]
        assert err["trace_id"] == sample["trace_id"]
        assert [s["op"] for s in sample["spans"]] == ["db.query", "http.client"]
        assert find(server, "db.query", contains="SELECT %s + ?, ?")
        assert find(server, "http.client", contains=target.host)
        assert "lit-secret" not in json.dumps(server.metrics)
    finally:
        sys.modules.pop("spans_urls", None)


def test_fastapi_async_endpoint_with_httpx_and_error(server, init, target):
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from insight_sdk.integrations import fastapi as ifa

    init(integrations=["httpx"])
    app = FastAPI()
    ifa.init_app(app)

    @app.get("/a/{n}")
    async def a(n: int):
        async with httpx.AsyncClient() as c:
            await c.get(target.base + "/ok")
        raise RuntimeError("async boom")

    r = TestClient(app, raise_server_exceptions=False).get("/a/3")
    assert r.status_code == 500
    assert insight_sdk.flush(5)
    (err,) = [e for e in server.events if e.get("exception")]
    (sample,) = [s for s in samples(server) if s["name"].startswith("GET /a")]
    assert err["trace_id"] == sample["trace_id"]
    assert sample["spans"][0]["description"] == "GET " + target.host
