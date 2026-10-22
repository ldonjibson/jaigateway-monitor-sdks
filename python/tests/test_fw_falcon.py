import asyncio
import threading
import time

import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, crumbs, sent

falcon = pytest.importorskip("falcon")
import falcon.asgi  # noqa: E402
import falcon.testing as ft  # noqa: E402

from insight_sdk.integrations.falcon import InsightMiddleware, init_app  # noqa: E402


class Item:
    def on_get(self, req, resp, item_id):
        insight_sdk.set_tag("item", item_id)
        raise ValueError("falcon boom " + item_id)


class Slow:
    def on_get(self, req, resp, n):
        insight_sdk.set_tag("n", n)
        time.sleep(0.15)
        raise RuntimeError("slow boom " + n)


class Errors:
    def on_get(self, req, resp, code):
        raise {"404": falcon.HTTPNotFound, "400": falcon.HTTPBadRequest, "503": falcon.HTTPServiceUnavailable,
               "500": falcon.HTTPInternalServerError}[code]()


class Ok:
    def on_get(self, req, resp):
        resp.text = "fine"


class AItem:
    async def on_get(self, req, resp, item_id):
        insight_sdk.set_tag("item", item_id)
        await asyncio.sleep(0.05)
        raise ValueError("falcon async boom " + item_id)


class AErrors:
    async def on_get(self, req, resp, code):
        raise {"404": falcon.HTTPNotFound, "503": falcon.HTTPServiceUnavailable}[code]()


class AOk:
    async def on_get(self, req, resp):
        resp.text = "fine"


@pytest.fixture(autouse=True)
def event_loop_for_falcon_testing():  # falcon.testing's ASGI client uses get_event_loop(); earlier asyncio.run() unset it
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


def build_wsgi():
    app = falcon.App()
    app.add_route("/items/{item_id}", Item())
    app.add_route("/slow/{n}", Slow())
    app.add_route("/errors/{code}", Errors())
    app.add_route("/ok", Ok())
    return init_app(app)


def build_asgi():
    app = falcon.asgi.App()
    app.add_route("/items/{item_id}", AItem())
    app.add_route("/errors/{code}", AErrors())
    app.add_route("/ok", AOk())
    return init_app(app)


def test_falcon_wsgi_route_template_and_normal_500(server, init):
    init()
    c = ft.TestClient(build_wsgi())
    r = c.simulate_get("/items/5", query_string="token=abc&p=1", headers=SECRET_HEADERS)
    plain = ft.TestClient(falcon.App())  # reference: a Falcon app without the SDK
    plain.app.add_route("/items/{item_id}", Item())
    ref = plain.simulate_get("/items/5")
    assert r.status_code == ref.status_code == 500
    assert r.json == ref.json  # Falcon's HTTPInternalServerError body
    (e,) = sent(server)
    assert e["exception"]["value"] == "falcon boom 5" and e["tags"]["item"] == "5"
    assert_tags(e, "falcon", "/items/{item_id}")
    assert_request(e, "/items/5")
    assert e["request"]["query"] == "token=[Filtered]&p=1"
    assert crumbs(e)[0]["message"] == "GET /items/5"


def test_falcon_wsgi_http_errors(server, init):
    init()
    c = ft.TestClient(build_wsgi())
    assert [c.simulate_get(p).status_code for p in ("/errors/404", "/errors/400", "/ok", "/nowhere")] == [404, 400, 200, 404]
    assert sent(server) == []
    assert c.simulate_get("/errors/503").status_code == 503
    assert c.simulate_get("/errors/500").status_code == 500
    events = sent(server)
    assert [e["exception"]["type"] for e in events] == ["HTTPServiceUnavailable", "HTTPInternalServerError"]
    assert events[0]["tags"]["route"] == "/errors/{code}"


def test_falcon_asgi(server, init):
    init()
    c = ft.TestClient(build_asgi())
    r = c.simulate_get("/items/8", query_string="secret=x", headers=SECRET_HEADERS)
    assert r.status_code == 500 and r.json["title"] == "500 Internal Server Error"
    assert c.simulate_get("/errors/404").status_code == 404
    assert c.simulate_get("/ok").status_code == 200
    assert c.simulate_get("/errors/503").status_code == 503
    boom, down = sent(server)
    assert boom["exception"]["value"] == "falcon async boom 8" and boom["tags"]["item"] == "8"
    assert_tags(boom, "falcon", "/items/{item_id}")
    assert_request(boom, "/items/8")
    assert boom["request"]["query"] == "secret=[Filtered]"
    assert down["exception"]["type"] == "HTTPServiceUnavailable"


def test_falcon_asgi_concurrent_isolation(server, init):
    init()
    c = ft.TestClient(build_asgi())

    async def go():
        async with ft.ASGIConductor(c.app) as conductor:
            return await asyncio.gather(conductor.simulate_get("/items/1"), conductor.simulate_get("/items/2"),
                                        conductor.simulate_get("/ok"))

    res = asyncio.run(go())
    assert [r.status_code for r in res] == [500, 500, 200]
    events = {e["exception"]["value"]: e for e in sent(server)}
    assert events["falcon async boom 1"]["tags"]["item"] == "1" and events["falcon async boom 2"]["tags"]["item"] == "2"


def test_falcon_wsgi_threads_isolated(server, init):
    init()
    app = build_wsgi()
    codes = []

    def hit(n):
        codes.append(ft.TestClient(app).simulate_get("/slow/%d" % n).status_code)

    threads = [threading.Thread(target=hit, args=(n,)) for n in (1, 2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [500, 500]
    events = {e["exception"]["value"]: e for e in sent(server)}
    assert events["slow boom 1"]["tags"]["n"] == "1" and events["slow boom 2"]["tags"]["n"] == "2"


def test_falcon_user_error_handler_for_specific_exception_wins(server, init):
    init()
    app = falcon.App()
    app.add_route("/items/{item_id}", Item())
    init_app(app)

    def mine(req, resp, ex, params):
        resp.status = falcon.HTTP_418
        resp.text = "teapot"

    app.add_error_handler(ValueError, mine)
    r = ft.TestClient(app).simulate_get("/items/1")
    assert (r.status_code, r.text) == (418, "teapot")  # the app's own handler decides the response


def test_falcon_wsgi_performance_transactions(server, init):
    from fwutil import assert_perf

    init()
    c = ft.TestClient(build_wsgi())
    assert c.simulate_get("/ok").status_code == 200
    assert c.simulate_get("/items/5").status_code == 500 and c.simulate_get("/items/6").status_code == 500
    assert c.simulate_get("/errors/404").status_code == 404
    assert c.simulate_get("/errors/503").status_code == 503
    assert c.simulate_get("/nowhere/1").status_code == 404
    assert_perf(server, {
        "GET /ok": (1, 0), "GET /items/{item_id}": (2, 2), "GET /errors/{code}": (2, 1), "GET <unmatched>": (1, 0),
    })


def test_falcon_asgi_performance_transactions(server, init):
    from fwutil import assert_perf

    init()
    c = ft.TestClient(build_asgi())
    assert c.simulate_get("/ok").status_code == 200
    assert c.simulate_get("/items/8").status_code == 500
    assert c.simulate_get("/errors/404").status_code == 404
    assert c.simulate_get("/errors/503").status_code == 503
    assert c.simulate_get("/nowhere/1").status_code == 404
    assert_perf(server, {
        "GET /ok": (1, 0), "GET /items/{item_id}": (1, 1), "GET /errors/{code}": (2, 1), "GET <unmatched>": (1, 0),
    })
