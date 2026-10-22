import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, sent

pytest.importorskip("starlette")
pytest.importorskip("httpx")
from starlette.applications import Starlette  # noqa: E402
from starlette.exceptions import HTTPException  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from insight_sdk.integrations.starlette import init_app  # noqa: E402


def build():
    async def boom(request):
        raise ValueError("starlette boom " + request.path_params["n"])

    async def unavailable(request):
        raise HTTPException(503, "maintenance")

    async def gone(request):
        raise HTTPException(410, "gone")

    async def ok(request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/boom/{n}", boom), Route("/u", unavailable), Route("/gone", gone), Route("/ok", ok)])


def test_starlette_init_app(server, init):
    init()
    app = init_app(build())
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/boom/7?api_key=zzz", headers=SECRET_HEADERS)
    assert r.status_code == 500 and r.text == "Internal Server Error"
    u = c.get("/u")
    assert (u.status_code, u.text) == (503, "maintenance")  # Starlette's default HTTPException response
    assert c.get("/gone").status_code == 410
    assert c.get("/ok").status_code == 200
    assert c.get("/missing").status_code == 404
    boom, unavailable = sent(server)
    assert_tags(boom, "starlette", "/boom/{n}")
    assert_request(boom, "/boom/7")
    assert boom["request"]["query"] == "api_key=[Filtered]"
    assert unavailable["tags"]["route"] == "/u" and unavailable["exception"]["type"] == "HTTPException"


def test_starlette_user_exception_handler_wrapped(server, init):
    init()
    app = build()

    async def mine(request, exc):
        return PlainTextResponse("mine:%d" % exc.status_code, status_code=exc.status_code)

    app.add_exception_handler(HTTPException, mine)
    init_app(app)
    r = TestClient(app, raise_server_exceptions=False).get("/u")
    assert (r.status_code, r.text) == (503, "mine:503")
    assert len(sent(server)) == 1


def test_starlette_performance_transactions(server, init):
    from fwutil import assert_perf

    init()
    c = TestClient(init_app(build()), raise_server_exceptions=False)
    assert c.get("/ok").status_code == 200
    assert c.get("/boom/7").status_code == 500 and c.get("/boom/8").status_code == 500
    assert c.get("/gone").status_code == 410  # matched, 4xx: not an error
    assert c.get("/u").status_code == 503  # HTTPException 5xx: an error
    assert c.get("/missing/xyz").status_code == 404
    assert_perf(server, {
        "GET /ok": (1, 0), "GET /boom/{n}": (2, 2), "GET /gone": (1, 0), "GET /u": (1, 1), "GET <unmatched>": (1, 0),
    })


def test_starlette_behind_an_asgi_middleware_is_one_transaction(server, init):
    from fwutil import assert_perf
    from insight_sdk.integrations.asgi import InsightASGIMiddleware

    init()
    outer = InsightASGIMiddleware(init_app(build()))  # two layers: the outer one owns the transaction
    c = TestClient(outer, raise_server_exceptions=False)
    assert c.get("/ok").status_code == 200
    assert c.get("/boom/1").status_code == 500
    assert c.get("/nope").status_code == 404
    assert_perf(server, {"GET /ok": (1, 0), "GET /boom/{n}": (1, 1), "GET <unmatched>": (1, 0)})
