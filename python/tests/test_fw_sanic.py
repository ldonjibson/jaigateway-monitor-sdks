import asyncio
import itertools

import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, crumbs, sent

sanic = pytest.importorskip("sanic")
pytest.importorskip("sanic_testing")
from sanic import Sanic  # noqa: E402
from sanic.exceptions import NotFound, SanicException  # noqa: E402
from sanic.response import text  # noqa: E402

from insight_sdk.integrations.sanic import init_app  # noqa: E402

Sanic.test_mode = True
_names = itertools.count()


def build(install=True):
    app = Sanic("fw_sanic_%d" % next(_names))

    @app.get("/items/<item_id:int>")
    async def item(request, item_id):
        insight_sdk.set_tag("item", str(item_id))
        await asyncio.sleep(0.05)
        raise ValueError("sanic boom %d" % item_id)

    @app.get("/e503")
    async def e503(request):
        raise SanicException("down", status_code=503)

    @app.get("/e404")
    async def e404(request):
        raise NotFound("nope")

    @app.get("/e400")
    async def e400(request):
        raise SanicException("bad", status_code=400)

    @app.get("/ok")
    async def ok(request):
        return text("fine")

    if install:
        init_app(app)
    return app


def call(app, *paths, **kw):
    async def go():
        out = []
        for p in paths:
            _, resp = await app.asgi_client.get(p, **kw)
            out.append(resp)
        return out

    return asyncio.run(go())


def test_sanic_route_template_and_normal_500(server, init):
    init()
    app = build()
    (r,) = call(app, "/items/6?secret=abc&x=1", headers=SECRET_HEADERS)
    base = build(install=False)
    (plain,) = call(base, "/items/6")
    assert r.status_code == plain.status_code == 500
    assert r.content_type == plain.content_type and r.text == plain.text  # Sanic's default error response
    (e,) = sent(server)
    assert e["exception"]["value"] == "sanic boom 6" and e["tags"]["item"] == "6"
    assert_tags(e, "sanic", "/items/<item_id:int>")
    assert_request(e, "/items/6")
    assert e["request"]["query"] == "secret=[Filtered]&x=1"
    assert crumbs(e)[0]["message"] == "GET /items/6"


def test_sanic_5xx_sanic_exception_reported_4xx_not(server, init):
    init()
    app = build()
    r503, r404, r400, ok, missing = call(app, "/e503", "/e404", "/e400", "/ok", "/nowhere")
    assert [r.status_code for r in (r503, r404, r400, ok, missing)] == [503, 404, 400, 200, 404]
    (e,) = sent(server)
    assert e["exception"]["type"] == "SanicException" and e["tags"]["route"] == "/e503"


def test_sanic_concurrent_requests_isolated(server, init):
    init()
    app = build()

    async def go():
        return await asyncio.gather(app.asgi_client.get("/items/1"), app.asgi_client.get("/items/2"), app.asgi_client.get("/ok"))

    res = asyncio.run(go())
    assert [r[1].status_code for r in res] == [500, 500, 200]
    events = {e["exception"]["value"]: e for e in sent(server)}
    assert events["sanic boom 1"]["tags"]["item"] == "1" and events["sanic boom 2"]["tags"]["item"] == "2"
    assert events["sanic boom 1"]["request"]["url"].endswith("/items/1")


def test_sanic_real_server_test_client(server, init):
    init()
    app = build()
    _, resp = app.test_client.get("/items/8", headers=SECRET_HEADERS)
    assert resp.status_code == 500
    (e,) = sent(server)
    assert e["tags"]["route"] == "/items/<item_id:int>"


def test_sanic_performance_transactions(server, init):
    from fwutil import assert_perf

    init()
    app = build()
    call(app, "/ok", "/ok", "/items/6", "/items/7", "/e404", "/e503", "/nowhere/42")
    assert_perf(server, {
        "GET /ok": (2, 0), "GET /items/<item_id:int>": (2, 2), "GET /e404": (1, 0), "GET /e503": (1, 1), "GET <unmatched>": (1, 0),
    })
