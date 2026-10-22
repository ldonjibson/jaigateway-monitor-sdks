import threading

import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, crumbs, sent

flask = pytest.importorskip("flask")
from werkzeug.exceptions import BadRequest, InternalServerError  # noqa: E402

from insight_sdk.integrations.flask import init_app  # noqa: E402


def build(handlers=False):
    app = flask.Flask("fw")

    @app.route("/items/<int:item_id>")
    def item(item_id):
        insight_sdk.set_tag("item", str(item_id))
        raise ValueError("flask item boom %d" % item_id)

    @app.route("/slow/<int:n>")
    def slow(n):
        import time

        insight_sdk.set_tag("n", str(n))
        time.sleep(0.15)
        raise RuntimeError("slow boom %d" % n)

    @app.route("/abort404")
    def abort404():
        flask.abort(404)

    @app.route("/abort400")
    def abort400():
        raise BadRequest("bad")

    @app.route("/abort500")
    def abort500():
        raise InternalServerError("explicit 500")

    @app.route("/redirect")
    def redirect():
        return flask.redirect("/ok")

    @app.route("/ok")
    def ok():
        return "ok"

    if handlers:
        @app.errorhandler(ValueError)
        def swallow(exc):
            return "handled by app", 500

    return app


def test_flask_one_event_route_template_and_normal_500(server, init):
    init(integrations=["flask", "logging"])
    app = build()
    init_app(app)
    init_app(app)  # idempotent
    r = app.test_client().get("/items/3?password=hunter2&a=1", headers=SECRET_HEADERS)
    assert r.status_code == 500 and b"Internal Server Error" in r.data  # Flask's own 500 page
    (e,) = sent(server)
    assert e["exception"]["value"] == "flask item boom 3" and e["tags"]["item"] == "3"
    assert_tags(e, "flask", "/items/<int:item_id>")
    assert_request(e, "/items/3")
    assert e["request"]["query"] == "password=[Filtered]&a=1"
    assert crumbs(e)[0]["message"] == "GET /items/3"


def test_flask_client_errors_and_redirects_not_reported(server, init):
    init(integrations=["flask"])
    app = build()
    init_app(app)
    c = app.test_client()
    assert c.get("/abort404").status_code == 404
    assert c.get("/abort400").status_code == 400
    assert c.get("/nowhere").status_code == 404
    assert c.get("/redirect").status_code == 302
    assert c.get("/ok").status_code == 200
    assert c.post("/ok").status_code == 405
    assert sent(server) == []
    assert c.get("/abort500").status_code == 500
    assert len(sent(server)) == 1  # an explicitly raised 5xx HTTPException is reported (Flask never signals it)


def test_flask_error_handler_that_swallows_the_exception_is_still_reported_once(server, init):
    init(integrations=["flask"])
    app = build(handlers=True)
    init_app(app)
    r = app.test_client().get("/items/1")
    assert (r.status_code, r.data) == (500, b"handled by app")  # the app's own response
    (e,) = sent(server)
    assert e["exception"]["value"] == "flask item boom 1" and e["tags"]["route"] == "/items/<int:item_id>"


def test_flask_threaded_requests_are_isolated(server, init):
    init(integrations=["flask"])
    app = build()
    init_app(app)
    codes = []

    def hit(n):
        codes.append(app.test_client().get("/slow/%d" % n).status_code)

    threads = [threading.Thread(target=hit, args=(n,)) for n in (1, 2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [500, 500]
    events = {e["exception"]["value"]: e for e in sent(server)}
    assert events["slow boom 1"]["tags"]["n"] == "1" and events["slow boom 2"]["tags"]["n"] == "2"
    assert events["slow boom 1"]["request"]["url"].endswith("/slow/1")


def test_flask_scope_discarded_after_request(server, init):
    init(integrations=["flask"])
    app = build()
    init_app(app)
    app.test_client().get("/items/9")
    insight_sdk.capture_message("later")
    last = sent(server)[-1]
    assert "item" not in last["tags"] and "route" not in last["tags"]


def test_flask_performance_transactions(server, init):
    from fwutil import assert_perf

    init(integrations=["flask"])
    app = build()
    init_app(app)
    c = app.test_client()
    for i in range(3):
        assert c.get("/ok").status_code == 200
    assert c.get("/items/17").status_code == 500  # unhandled exception -> Flask's 500
    assert c.get("/items/18").status_code == 500
    assert c.get("/abort404").status_code == 404  # matched route, 404 response: counted, not an error
    assert c.get("/nowhere/123").status_code == 404  # no rule: never the raw path
    assert c.get("/redirect").status_code == 302
    assert_perf(server, {
        "GET /ok": (3, 0), "GET /items/<int:item_id>": (2, 2), "GET /abort404": (1, 0),
        "GET <unmatched>": (1, 0), "GET /redirect": (1, 0),
    })


def test_flask_user_handler_response_status_decides_the_error(server, init):
    from fwutil import assert_perf

    init(integrations=["flask"])
    app = build(handlers=True)  # @errorhandler(ValueError) answers 500
    init_app(app)
    assert app.test_client().get("/items/3").status_code == 500
    assert_perf(server, {"GET /items/<int:item_id>": (1, 1)})


def test_flask_inside_a_wsgi_middleware_is_one_transaction(server, init):
    from fwutil import assert_perf
    from insight_sdk.integrations.wsgi import InsightWSGIMiddleware

    init(integrations=["flask"])
    app = build()
    init_app(app)
    app.wsgi_app = InsightWSGIMiddleware(app.wsgi_app)
    c = app.test_client()
    for path, code in (("/ok", 200), ("/items/1", 500), ("/nowhere", 404)):
        with c.get(path) as r:  # the WSGI layer times until the server closes the response, like a real server does
            assert r.status_code == code
    assert_perf(server, {"GET /ok": (1, 0), "GET /items/<int:item_id>": (1, 1), "GET <unmatched>": (1, 0)})


def test_flask_ignored_paths_are_not_timed(server, init):
    from fwutil import assert_perf

    init(integrations=["flask"])
    app = build()

    @app.route("/health")
    def health():
        return "ok"

    @app.route("/static/<path:p>")
    def stat(p):
        return "x"

    init_app(app)
    c = app.test_client()
    for p in ("/health", "/health/", "/static/a.js"):
        c.get(p)
    c.get("/ok")
    assert_perf(server, {"GET /ok": (1, 0)})


TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT = "00f067aa0ba902b7"


def _tp(flags):
    return "00-%s-%s-%s" % (TRACE, PARENT, flags)


def _samples(server):
    assert insight_sdk.flush(5)
    return [s for b in server.metrics for s in b.get("samples", [])]


def test_flask_adopts_incoming_traceparent_on_sample_and_error_event(server, init):
    import re

    init(integrations=["flask", "logging"])
    app = build()
    init_app(app)
    c = app.test_client()
    assert c.get("/ok", headers={"traceparent": _tp("01")}).status_code == 200
    assert c.get("/items/9", headers={"Traceparent": _tp("00").upper()}).status_code == 500
    assert c.get("/ok").status_code == 200  # no header: a new trace
    by_name = {}
    for s in _samples(server):
        by_name.setdefault(s["name"], []).append(s)
    ok_samples = by_name["GET /ok"]
    adopted = [s for s in ok_samples if s["trace_id"] == TRACE]
    fresh = [s for s in ok_samples if s["trace_id"] != TRACE]
    assert len(adopted) == 1 and len(fresh) == 1
    a = adopted[0]
    assert a["parent_span_id"] == PARENT and a["kind"] == "server" and a["op"] == "http.server" and re.match("^[0-9a-f]{16}$", a["span_id"])
    assert "parent_span_id" not in fresh[0] and fresh[0]["span_id"] != a["span_id"]
    (err,) = [e for e in server.events if e.get("exception")]
    assert err["trace_id"] == TRACE  # the error captured inside the request carries the ADOPTED id
    (failed,) = by_name["GET /items/<int:item_id>"]
    assert failed["trace_id"] == TRACE and failed["parent_span_id"] == PARENT and failed["ok"] is False


def test_flask_forced_sample_for_a_fast_200_only_when_flag_is_01(server, init, monkeypatch):
    from insight_sdk import performance as perfmod

    init(integrations=["flask"])
    monkeypatch.setattr(perfmod.random, "randrange", lambda n: 1)  # no random pick
    app = build()
    init_app(app)
    c = app.test_client()
    for i in range(4):
        assert c.get("/slow/%d" % i).status_code == 500  # slower, and failed: the usual samples
    assert c.get("/ok", headers={"traceparent": _tp("01")}).status_code == 200
    assert c.get("/redirect", headers={"traceparent": _tp("00")}).status_code == 302
    names = {s["name"] for s in _samples(server)}
    assert "GET /ok" in names
    assert "GET /redirect" not in names
