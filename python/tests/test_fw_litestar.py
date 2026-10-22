import asyncio

import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, sent

pytest.importorskip("litestar")
from litestar import Litestar, get  # noqa: E402
from litestar.exceptions import HTTPException, NotFoundException, ValidationException  # noqa: E402
from litestar.testing import TestClient  # noqa: E402

from insight_sdk.integrations.litestar import InsightPlugin  # noqa: E402


@get("/items/{item_id:int}")
async def item(item_id: int) -> str:
    insight_sdk.set_user({"id": "u-1"})
    raise ValueError("litestar boom %d" % item_id)


@get("/slow/{n:int}")
async def slow(n: int) -> str:
    insight_sdk.set_tag("n", str(n))
    await asyncio.sleep(0.05)
    raise RuntimeError("slow boom %d" % n)


@get("/e503")
async def e503() -> str:
    raise HTTPException(status_code=503, detail="down")


@get("/e404")
async def e404() -> str:
    raise NotFoundException("nope")


@get("/e400")
async def e400() -> str:
    raise ValidationException("bad")


@get("/ok")
async def ok() -> str:
    return "fine"


def app():
    return Litestar([item, slow, e503, e404, e400, ok], plugins=[InsightPlugin()])


def test_litestar_unhandled_route_template_and_response(server, init):
    init()
    with TestClient(app()) as c:
        r = c.get("/items/5?token=abc", headers=SECRET_HEADERS)
        assert r.status_code == 500 and r.json() == {"status_code": 500, "detail": "Internal Server Error"}
    (e,) = sent(server)
    assert e["exception"]["value"] == "litestar boom 5"
    assert_tags(e, "litestar", "/items/{item_id}")
    assert_request(e, "/items/5")
    assert e["request"]["query"] == "token=[Filtered]" and e["user"] == {"id": "u-1"}
    assert e["breadcrumbs"][0]["category"] == "http"


def test_litestar_http_exceptions(server, init):
    init()
    with TestClient(app()) as c:
        r = c.get("/e503")
        assert r.status_code == 503 and r.json()["detail"] == "down"
        assert c.get("/e404").status_code == 404
        assert c.get("/e400").status_code == 400
        assert c.get("/ok").status_code == 200
        assert c.get("/nowhere").status_code == 404
    (e,) = sent(server)
    assert e["tags"]["route"] == "/e503"


def test_litestar_concurrent_scope_isolation(server, init):
    import httpx

    init()
    a = app()

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://t") as client:
            return await asyncio.gather(client.get("/slow/1"), client.get("/slow/2"))

    asyncio.run(go())
    events = {e["exception"]["value"]: e for e in sent(server)}
    assert events["slow boom 1"]["tags"]["n"] == "1" and events["slow boom 2"]["tags"]["n"] == "2"


def test_litestar_performance_transactions(server, init):
    from fwutil import assert_perf

    init()
    with TestClient(app()) as c:
        assert c.get("/ok").status_code == 200
        assert c.get("/items/5").status_code == 500 and c.get("/items/6").status_code == 500
        assert c.get("/e404").status_code == 404
        assert c.get("/e503").status_code == 503
        assert c.get("/nowhere/77").status_code == 404
    assert_perf(server, {
        "GET /ok": (1, 0), "GET /items/{item_id}": (2, 2), "GET /e404": (1, 0), "GET /e503": (1, 1), "GET <unmatched>": (1, 0),
    })
