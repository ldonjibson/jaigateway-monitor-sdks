import asyncio

import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, crumbs, sent

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from insight_sdk.integrations.fastapi import init_app  # noqa: E402


def build(custom_handlers=False):
    app = FastAPI()

    def auth_dep():  # sync dependency runs in the threadpool: its scope writes must reach the event
        insight_sdk.set_user({"id": "u-7"})
        insight_sdk.set_tag("from_dep", "yes")

    @app.get("/items/{item_id}")
    def item(item_id: int, _=Depends(auth_dep)):
        insight_sdk.add_breadcrumb("loading item", category="custom")
        raise ValueError("item boom %d" % item_id)

    @app.get("/async/{item_id}")
    async def async_item(item_id: int):
        insight_sdk.set_tag("item", str(item_id))
        await asyncio.sleep(0.05)
        raise RuntimeError("async boom %d" % item_id)

    @app.get("/e503")
    def e503():
        raise HTTPException(status_code=503, detail="down")

    @app.get("/e404")
    def e404():
        raise HTTPException(status_code=404, detail="nope")

    @app.get("/e401")
    def e401():
        raise HTTPException(status_code=401, detail="who")

    @app.get("/ok")
    def ok():
        return {"ok": True}

    @app.get("/bg")
    def bg(tasks: BackgroundTasks):
        def later():
            insight_sdk.set_tag("in_bg", "1")
            raise KeyError("bg boom")

        tasks.add_task(later)
        return {"queued": True}

    if custom_handlers:
        @app.exception_handler(Exception)
        async def any_exc(request, exc):
            return PlainTextResponse("custom 500", status_code=500)

    return app


def setup(init, **kw):
    init(**kw)
    app = build()
    init_app(app)
    return app


def test_unhandled_exception_reported_once_with_route_template(server, init):
    app = setup(init)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/items/5?token=abc&page=2", headers=SECRET_HEADERS)
    assert r.status_code == 500 and r.text == "Internal Server Error"  # starlette's own 500 is unchanged
    (e,) = sent(server)
    assert e["exception"]["type"] == "ValueError" and e["exception"]["value"] == "item boom 5"
    assert_tags(e, "fastapi", "/items/{item_id}")  # template, not "/items/5"
    assert_request(e, "/items/5")
    assert e["request"]["query"] == "token=[Filtered]&page=2"
    assert e["user"] == {"id": "u-7"} and e["tags"]["from_dep"] == "yes"  # dependency scope carries through
    assert crumbs(e)[0]["message"] == "GET /items/5"
    assert [b["message"] for b in e["breadcrumbs"]][-1] == "loading item"
    assert any(f["function"] == "item" and f["in_app"] for f in e["exception"]["stacktrace"])


def test_http_exception_5xx_reported_4xx_not(server, init):
    c = TestClient(setup(init), raise_server_exceptions=False)
    r503 = c.get("/e503")
    assert (r503.status_code, r503.json()) == (503, {"detail": "down"})  # FastAPI's own response
    assert c.get("/e404").status_code == 404
    assert c.get("/e401").status_code == 401
    assert c.get("/ok").status_code == 200
    assert c.get("/items/notanint").status_code == 422
    assert c.get("/does-not-exist").status_code == 404
    (e,) = sent(server)
    assert e["exception"]["type"] == "HTTPException" and "503" in e["description"]
    assert e["tags"]["route"] == "/e503"


def test_background_task_exception_reported_once_with_scope(server, init):
    c = TestClient(setup(init), raise_server_exceptions=False)
    assert c.get("/bg", headers=SECRET_HEADERS).status_code == 200
    (e,) = sent(server)
    assert e["exception"]["type"] == "KeyError" and e["tags"]["in_bg"] == "1"
    assert e["tags"]["route"] == "/bg" and e["request"]["url"].endswith("/bg")


def test_init_app_twice_and_custom_exception_handler_still_one_event(server, init):
    init()
    app = build(custom_handlers=True)
    init_app(app)
    init_app(app)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/items/1")
    assert r.status_code == 500 and r.text == "custom 500"  # the app's own handler still decides the response
    assert len(sent(server)) == 1


def test_scope_isolation_between_concurrent_requests(server, init):
    import httpx

    app = setup(init)

    async def go():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await asyncio.gather(client.get("/async/1"), client.get("/async/2"), client.get("/ok"))

    r1, r2, r3 = asyncio.run(go())
    assert (r1.status_code, r2.status_code, r3.status_code) == (500, 500, 200)
    events = {e["exception"]["value"]: e for e in sent(server)}
    assert set(events) == {"async boom 1", "async boom 2"}
    assert events["async boom 1"]["tags"]["item"] == "1" and events["async boom 2"]["tags"]["item"] == "2"
    assert events["async boom 1"]["request"]["url"].endswith("/async/1")
    assert events["async boom 2"]["request"]["url"].endswith("/async/2")
    insight_sdk.capture_message("outside")
    assert "item" not in sent(server)[-1]["tags"]  # request scopes were discarded


def test_mounted_subapp_route_template(server, init):
    init()
    sub = FastAPI()

    @sub.get("/things/{tid}")
    def thing(tid: str):
        raise ValueError("sub boom")

    app = FastAPI()
    app.mount("/api", sub)
    init_app(app)
    TestClient(app, raise_server_exceptions=False).get("/api/things/9")
    (e,) = sent(server)
    assert e["tags"]["route"] == "/api/things/{tid}", e["tags"]


def test_sdk_not_initialised_is_harmless():
    app = FastAPI()

    @app.get("/x")
    def x():
        raise ValueError("no client")

    init_app(app)
    assert TestClient(app, raise_server_exceptions=False).get("/x").status_code == 500


def test_fastapi_performance_transactions(server, init):
    from fwutil import assert_perf

    init()
    app = build()
    init_app(app)
    c = TestClient(app, raise_server_exceptions=False)
    for _ in range(4):
        assert c.get("/ok").status_code == 200
    assert c.get("/items/17").status_code == 500 and c.get("/items/18").status_code == 500
    assert c.get("/async/3").status_code == 500
    assert c.get("/e404").status_code == 404
    assert c.get("/e401").status_code == 401
    assert c.get("/e503").status_code == 503
    assert c.get("/items/notanint").status_code == 422  # validation error: matched route, 4xx
    assert c.get("/definitely/not/here").status_code == 404
    assert_perf(server, {
        "GET /ok": (4, 0), "GET /items/{item_id}": (3, 2), "GET /async/{item_id}": (1, 1), "GET /e404": (1, 0),
        "GET /e401": (1, 0), "GET /e503": (1, 1), "GET <unmatched>": (1, 0),
    })
    body = server.metrics[-1] if server.metrics else {}
    assert all("17" not in t["name"] for b in server.metrics for t in b["transactions"])


def test_fastapi_adopts_incoming_traceparent(server, init):
    import re

    init(integrations=["logging"])
    app = build()
    init_app(app)
    c = TestClient(app, raise_server_exceptions=False)
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert c.get("/ok", headers={"traceparent": tp}).status_code == 200
    assert c.get("/async/4", headers={"traceparent": tp}).status_code == 500
    assert insight_sdk.flush(5)
    samples = {s["name"]: s for b in server.metrics for s in b.get("samples", [])}
    for name in ("GET /ok", "GET /async/{item_id}"):
        s = samples[name]
        assert s["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736" and s["parent_span_id"] == "00f067aa0ba902b7"
        assert s["kind"] == "server" and s["op"] == "http.server" and re.match("^[0-9a-f]{16}$", s["span_id"])
    (err,) = [e for e in server.events if e.get("exception")]
    assert err["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
