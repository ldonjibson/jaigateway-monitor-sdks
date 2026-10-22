import threading
import time

import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, crumbs, sent

bottle = pytest.importorskip("bottle")
webtest = pytest.importorskip("webtest")

from insight_sdk.integrations.bottle import InsightPlugin  # noqa: E402


def build(catchall=True):
    app = bottle.Bottle()
    app.config["catchall"] = catchall

    @app.route("/items/<item_id:int>")
    def item(item_id):
        insight_sdk.set_tag("item", str(item_id))
        raise ValueError("bottle boom %d" % item_id)

    @app.route("/slow/<n:int>")
    def slow(n):
        insight_sdk.set_tag("n", str(n))
        time.sleep(0.15)
        raise RuntimeError("slow boom %d" % n)

    @app.route("/abort404")
    def abort404():
        bottle.abort(404, "nope")

    @app.route("/abort503")
    def abort503():
        bottle.abort(503, "down")

    @app.route("/redirect")
    def redirect():
        bottle.redirect("/ok")

    @app.route("/ok")
    def ok():
        return "fine"

    app.install(InsightPlugin())
    return app


def test_bottle_route_template_and_normal_500(server, init):
    init()
    app = webtest.TestApp(build())
    r = app.get("/items/4?api_key=zzz&x=1", headers=SECRET_HEADERS, expect_errors=True)
    assert r.status_int == 500 and "Internal Server Error" in r.text  # Bottle's own error page
    (e,) = sent(server)
    assert e["exception"]["value"] == "bottle boom 4" and e["tags"]["item"] == "4"
    assert_tags(e, "bottle", "/items/<item_id:int>")
    assert_request(e, "/items/4")
    assert e["request"]["query"] == "api_key=[Filtered]&x=1"
    assert crumbs(e)[0]["message"] == "GET /items/4"


def test_bottle_http_errors_and_redirects(server, init):
    init()
    app = webtest.TestApp(build())
    assert app.get("/abort404", expect_errors=True).status_int == 404
    assert app.get("/redirect").status_int in (302, 303)
    assert app.get("/ok").status_int == 200
    assert app.get("/nowhere", expect_errors=True).status_int == 404
    assert sent(server) == []
    r = app.get("/abort503", expect_errors=True)
    assert r.status_int == 503
    (e,) = sent(server)
    assert e["exception"]["type"] == "HTTPError" and e["tags"]["route"] == "/abort503"


def test_bottle_catchall_false_reraises(server, init):
    init()
    app = webtest.TestApp(build(catchall=False))
    with pytest.raises(ValueError):
        app.get("/items/2")
    assert len(sent(server)) == 1


def test_bottle_threads_isolated(server, init):
    init()
    app = webtest.TestApp(build())
    codes = []

    def hit(n):
        codes.append(app.get("/slow/%d" % n, expect_errors=True).status_int)

    threads = [threading.Thread(target=hit, args=(n,)) for n in (1, 2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [500, 500]
    events = {e["exception"]["value"]: e for e in sent(server)}
    assert events["slow boom 1"]["tags"]["n"] == "1" and events["slow boom 2"]["tags"]["n"] == "2"


def test_bottle_performance_transactions(server, init):
    from fwutil import assert_perf

    init()
    app = webtest.TestApp(build())
    assert app.get("/ok").status_int == 200
    assert app.get("/items/4", expect_errors=True).status_int == 500
    assert app.get("/items/5", expect_errors=True).status_int == 500
    assert app.get("/abort404", expect_errors=True).status_int == 404
    assert app.get("/abort503", expect_errors=True).status_int == 503
    assert app.get("/redirect").status_int in (302, 303)
    assert app.get("/nowhere/77", expect_errors=True).status_int == 404  # Bottle never calls a route callback here
    assert_perf(server, {
        "GET /ok": (1, 0), "GET /items/<item_id:int>": (2, 2), "GET /abort404": (1, 0), "GET /abort503": (1, 1),
        "GET /redirect": (1, 0), "GET <unmatched>": (1, 0),
    })


def test_bottle_catchall_false_exception_is_an_error(server, init):
    from fwutil import assert_perf

    init()
    app = webtest.TestApp(build(catchall=False))
    with pytest.raises(ValueError):
        app.get("/items/4")
    assert_perf(server, {"GET /items/<item_id:int>": (1, 1)})
