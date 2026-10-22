import asyncio

import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, crumbs, sent

quart = pytest.importorskip("quart")
from quart import Quart, abort  # noqa: E402

from insight_sdk.integrations.quart import init_app  # noqa: E402


def build(handler=False):
    app = Quart("fwq")

    @app.route("/items/<int:item_id>")
    async def item(item_id):
        insight_sdk.set_tag("item", str(item_id))
        await asyncio.sleep(0.05)
        raise ValueError("quart boom %d" % item_id)

    @app.route("/sync/<int:item_id>")
    def sync_item(item_id):
        raise KeyError("quart sync boom")

    @app.route("/a404")
    async def a404():
        abort(404)

    @app.route("/a400")
    async def a400():
        abort(400)

    @app.route("/ok")
    async def ok():
        return "ok"

    if handler:
        @app.errorhandler(ValueError)
        async def swallow(exc):
            return "handled by app", 500

    return app


async def get(app, path, **kw):
    return await app.test_client().get(path, **kw)


def test_quart_route_template_one_event_and_normal_500(server, init):
    init(integrations=[])
    app = build()
    init_app(app)
    init_app(app)
    r = asyncio.run(get(app, "/items/4?token=abc", headers=SECRET_HEADERS))
    assert r.status_code == 500
    assert b"Internal Server Error" in asyncio.run(r.get_data())
    (e,) = sent(server)
    assert e["exception"]["value"] == "quart boom 4"
    assert_tags(e, "quart", "/items/<int:item_id>")
    assert_request(e, "/items/4")
    assert e["request"]["query"] == "token=[Filtered]"
    assert crumbs(e)[0]["message"] == "GET /items/4"


def test_quart_4xx_not_reported_and_sync_view(server, init):
    init(integrations=[])
    app = build()
    init_app(app)

    async def go():
        c = app.test_client()
        return [(await c.get(p)).status_code for p in ("/a404", "/a400", "/missing", "/ok", "/sync/1")]

    assert asyncio.run(go()) == [404, 400, 404, 200, 500]
    (e,) = sent(server)
    assert e["exception"]["type"] == "KeyError" and e["tags"]["route"] == "/sync/<int:item_id>"


def test_quart_swallowing_error_handler_still_reported_once(server, init):
    init(integrations=[])
    app = build(handler=True)
    init_app(app)
    r = asyncio.run(get(app, "/items/2"))
    assert r.status_code == 500 and asyncio.run(r.get_data()) == b"handled by app"
    (e,) = sent(server)
    assert e["exception"]["value"] == "quart boom 2"


def test_quart_concurrent_requests_isolated(server, init):
    init(integrations=[])
    app = build()
    init_app(app)

    async def go():
        c1, c2 = app.test_client(), app.test_client()
        return await asyncio.gather(c1.get("/items/1"), c2.get("/items/2"), c1.get("/ok"))

    res = asyncio.run(go())
    assert [r.status_code for r in res] == [500, 500, 200]
    events = {e["exception"]["value"]: e for e in sent(server)}
    assert events["quart boom 1"]["tags"]["item"] == "1" and events["quart boom 2"]["tags"]["item"] == "2"
    assert events["quart boom 1"]["request"]["url"].endswith("/items/1")


def test_quart_performance_transactions(server, init):
    from fwutil import assert_perf

    init(integrations=[])
    app = build()
    init_app(app)
    assert asyncio.run(get(app, "/ok")).status_code == 200
    assert asyncio.run(get(app, "/items/9")).status_code == 500
    assert asyncio.run(get(app, "/a404")).status_code == 404
    assert asyncio.run(get(app, "/nowhere/55")).status_code == 404
    assert_perf(server, {"GET /ok": (1, 0), "GET /items/<int:item_id>": (1, 1), "GET /a404": (1, 0), "GET <unmatched>": (1, 0)})
