import asyncio

import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, crumbs, sent

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from insight_sdk.integrations.aiohttp import insight_middleware, setup  # noqa: E402


def build(use_setup=True):
    routes = web.RouteTableDef()

    @routes.get("/items/{item_id}")
    async def item(request):
        insight_sdk.set_tag("item", request.match_info["item_id"])
        await asyncio.sleep(0.05)
        raise ValueError("aiohttp boom " + request.match_info["item_id"])

    @routes.get("/e503")
    async def e503(request):
        raise web.HTTPServiceUnavailable(text="down")

    @routes.get("/e404")
    async def e404(request):
        raise web.HTTPNotFound()

    @routes.get("/e302")
    async def e302(request):
        raise web.HTTPFound("/ok")

    @routes.get("/ok")
    async def ok(request):
        return web.Response(text="fine")

    app = web.Application() if use_setup else web.Application(middlewares=[insight_middleware()])
    app.add_routes(routes)
    if use_setup:
        setup(app)
        setup(app)  # idempotent
    return app


def with_client(app, fn):
    async def go():
        async with TestClient(TestServer(app)) as client:
            return await fn(client)

    return asyncio.run(go())


@pytest.mark.parametrize("use_setup", [True, False])
def test_aiohttp_route_template_and_normal_500(server, init, use_setup):
    init()

    async def fn(client):
        r = await client.get("/items/12?token=abc&k=v", headers=SECRET_HEADERS)
        return r.status, await r.text()

    status, body = with_client(build(use_setup), fn)
    assert status == 500 and "Internal Server Error" in body  # aiohttp's own 500
    (e,) = sent(server)
    assert e["exception"]["value"] == "aiohttp boom 12" and e["tags"]["item"] == "12"
    assert_tags(e, "aiohttp", "/items/{item_id}")
    assert_request(e, "/items/12")
    assert e["request"]["query"] == "token=[Filtered]&k=v"
    assert crumbs(e)[0]["message"] == "GET /items/12"


def test_aiohttp_http_exceptions(server, init):
    init()

    async def fn(client):
        out = []
        for p in ("/e503", "/e404", "/e302", "/ok", "/nowhere"):
            r = await client.get(p, allow_redirects=False)
            out.append(r.status)
        return out

    assert with_client(build(), fn) == [503, 404, 302, 200, 404]
    (e,) = sent(server)
    assert e["exception"]["type"] == "HTTPServiceUnavailable" and e["tags"]["route"] == "/e503"


def test_aiohttp_concurrent_requests_isolated(server, init):
    init()

    async def fn(client):
        rs = await asyncio.gather(client.get("/items/1"), client.get("/items/2"), client.get("/ok"))
        return [r.status for r in rs]

    assert with_client(build(), fn) == [500, 500, 200]
    events = {e["exception"]["value"]: e for e in sent(server)}
    assert events["aiohttp boom 1"]["tags"]["item"] == "1" and events["aiohttp boom 2"]["tags"]["item"] == "2"
    assert events["aiohttp boom 1"]["request"]["url"].endswith("/items/1")


def test_aiohttp_performance_transactions(server, init):
    from fwutil import assert_perf

    init()

    async def fn(client):
        return [(await client.get(p, allow_redirects=False)).status for p in ("/ok", "/ok", "/items/12", "/items/13", "/e404", "/e503", "/e302", "/nowhere/9")]

    assert with_client(build(), fn) == [200, 200, 500, 500, 404, 503, 302, 404]
    assert_perf(server, {
        "GET /ok": (2, 0), "GET /items/{item_id}": (2, 2), "GET /e404": (1, 0), "GET /e503": (1, 1), "GET /e302": (1, 0),
        "GET <unmatched>": (1, 0),
    })
