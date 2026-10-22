import threading
import time

import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, crumbs, sent

pyramid = pytest.importorskip("pyramid")
webtest = pytest.importorskip("webtest")
from pyramid import httpexceptions as exc  # noqa: E402
from pyramid.config import Configurator  # noqa: E402
from pyramid.response import Response  # noqa: E402


def build(exception_view=False, include=True):
    with Configurator() as config:
        if include:
            config.include("insight_sdk.integrations.pyramid")

        def item(request):
            insight_sdk.set_tag("item", request.matchdict["item_id"])
            raise ValueError("pyramid boom " + request.matchdict["item_id"])

        def slow(request):
            insight_sdk.set_tag("n", request.matchdict["n"])
            time.sleep(0.15)
            raise RuntimeError("slow boom " + request.matchdict["n"])

        def e404(request):
            raise exc.HTTPNotFound()

        def e403(request):
            raise exc.HTTPForbidden()

        def e302(request):
            raise exc.HTTPFound("/ok")

        def e500(request):
            raise exc.HTTPInternalServerError("explicit")

        def ok(request):
            return Response("fine")

        for name, pattern, view in (("item", "/items/{item_id}", item), ("slow", "/slow/{n}", slow), ("e404", "/e404", e404),
                                    ("e403", "/e403", e403), ("e302", "/e302", e302), ("e500", "/e500", e500), ("ok", "/ok", ok)):
            config.add_route(name, pattern)
            config.add_view(view, route_name=name)
        if exception_view:
            config.add_exception_view(lambda request: Response("custom error page", status=500), context=ValueError)
        return config.make_wsgi_app()


def test_tween_is_between_excview_and_main():
    from pyramid.config import Configurator
    from pyramid.interfaces import ITweens

    config = Configurator()
    config.include("insight_sdk.integrations.pyramid")
    config.commit()
    names = [n for n, _ in config.registry.queryUtility(ITweens).implicit()]
    assert names[-2:] == ["pyramid.tweens.excview_tween_factory", "insight_sdk.integrations.pyramid.insight_tween_factory"] or \
        names.index("insight_sdk.integrations.pyramid.insight_tween_factory") > names.index("pyramid.tweens.excview_tween_factory")


def test_pyramid_route_template_one_event_and_exception_propagates(server, init):
    init()
    app = webtest.TestApp(build())
    # no exception view: Pyramid lets the error propagate to the WSGI server, exactly as without the SDK
    with pytest.raises(ValueError, match="pyramid boom 3"):
        app.get("/items/3?token=abc&z=1", headers=SECRET_HEADERS)
    (e,) = sent(server)
    assert e["exception"]["value"] == "pyramid boom 3" and e["tags"]["item"] == "3"
    assert_tags(e, "pyramid", "/items/{item_id}")
    assert_request(e, "/items/3")
    assert e["request"]["query"] == "token=[Filtered]&z=1"
    assert crumbs(e)[0]["message"] == "GET /items/3"


def test_pyramid_exception_view_keeps_working_and_error_reported_once(server, init):
    init()
    app = webtest.TestApp(build(exception_view=True))
    r = app.get("/items/1", expect_errors=True)
    assert r.status_int == 500 and r.text == "custom error page"
    assert len(sent(server)) == 1


def test_pyramid_http_exceptions(server, init):
    init()
    app = webtest.TestApp(build())
    assert app.get("/e404", expect_errors=True).status_int == 404
    assert app.get("/e403", expect_errors=True).status_int == 403
    assert app.get("/e302").status_int == 302
    assert app.get("/ok").status_int == 200
    assert app.get("/missing", expect_errors=True).status_int == 404
    assert sent(server) == []
    assert app.get("/e500", expect_errors=True).status_int == 500
    (e,) = sent(server)
    assert e["exception"]["type"] == "HTTPInternalServerError" and e["tags"]["route"] == "/e500"


def test_pyramid_threads_isolated(server, init):
    init()
    app = webtest.TestApp(build(exception_view=False))
    errors = []

    def hit(n):
        try:
            app.get("/slow/%d" % n)
        except RuntimeError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=hit, args=(n,)) for n in (1, 2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(errors) == ["slow boom 1", "slow boom 2"]
    events = {e["exception"]["value"]: e for e in sent(server)}
    assert events["slow boom 1"]["tags"]["n"] == "1" and events["slow boom 2"]["tags"]["n"] == "2"


def test_pyramid_performance_transactions(server, init):
    from fwutil import assert_perf

    init()
    app = webtest.TestApp(build(exception_view=True))
    assert app.get("/ok").status_int == 200
    assert app.get("/items/3", expect_errors=True).status_int == 500  # exception view answers
    assert app.get("/items/4", expect_errors=True).status_int == 500
    assert app.get("/e404", expect_errors=True).status_int == 404
    assert app.get("/e403", expect_errors=True).status_int == 403
    assert app.get("/e500", expect_errors=True).status_int == 500
    assert app.get("/e302").status_int == 302
    assert app.get("/nowhere/55", expect_errors=True).status_int == 404
    assert_perf(server, {
        "GET /ok": (1, 0), "GET /items/{item_id}": (2, 2), "GET /e404": (1, 0), "GET /e403": (1, 0), "GET /e500": (1, 1),
        "GET /e302": (1, 0), "GET <unmatched>": (1, 0),
    })


def test_pyramid_unhandled_exception_propagating_is_an_error(server, init):
    from fwutil import assert_perf

    init()
    app = webtest.TestApp(build())
    with pytest.raises(ValueError):
        app.get("/items/3")
    assert_perf(server, {"GET /items/{item_id}": (1, 1)})
