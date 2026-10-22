import asyncio
import threading

import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, crumbs, sent

django = pytest.importorskip("django")
pytest.importorskip("rest_framework")

from django.test import AsyncClient, Client, override_settings  # noqa: E402

MW = "insight_sdk.integrations.django.InsightMiddleware"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "OPTIONS": {"loaders": [("django.template.loaders.locmem.Loader", {
        "explode.html": "value={{ obj.value }}",
        "syntax.html": "{% if %}",
    })]},
}]
DRF = {"EXCEPTION_HANDLER": "insight_sdk.integrations.django.drf_exception_handler",
       "DEFAULT_AUTHENTICATION_CLASSES": [], "DEFAULT_PERMISSION_CLASSES": [], "UNAUTHENTICATED_USER": None}


def ensure_configured():
    from test_integrations import configure_django  # the settings the other Django tests expect

    configure_django()


@pytest.fixture(autouse=True)
def dj():
    ensure_configured()
    with override_settings(ROOT_URLCONF="dj_fw_urls", MIDDLEWARE=[MW], TEMPLATES=TEMPLATES, REST_FRAMEWORK=DRF, ALLOWED_HOSTS=["*"]):
        from rest_framework import settings as drf_settings

        drf_settings.api_settings.reload()
        yield
    drf_settings.api_settings.reload()


def client():
    return Client(raise_request_exception=False)


def test_view_error_once_with_route_template_and_django_500(server, init):
    init(integrations=["django", "logging"])  # signal + logging + process_exception all see it: ONE event
    r = client().get("/items/5/?token=abc&z=1", HTTP_AUTHORIZATION="Bearer s3cret", HTTP_COOKIE="sid=abc",
                     HTTP_X_API_KEY="k-123", HTTP_X_TRACE="t1")
    assert r.status_code == 500 and b"Server Error" in r.content  # Django's own 500 page
    (e,) = sent(server)
    assert e["exception"]["value"] == "django item boom 5" and e["tags"]["view"] == "item"
    assert_tags(e, "django", "/items/<int:pk>/")
    assert_request(e, "/items/5/")
    assert e["request"]["query"] == "token=[Filtered]&z=1"
    assert crumbs(e)[0]["message"] == "GET /items/5/"


def test_regex_route_and_expected_errors_not_reported(server, init):
    init(integrations=["django"])
    c = client()
    assert c.get("/legacy/12/").status_code == 500
    assert c.get("/missing/").status_code == 404
    assert c.get("/nowhere/").status_code == 404
    assert c.get("/ok/").status_code == 200
    (e,) = sent(server)
    assert e["tags"]["route"] == "/legacy/(?P<pk>\\d+)/$"


def test_template_render_errors(server, init):
    init(integrations=["django"])
    c = client()
    for url in ("/tmpl-response/", "/tmpl-render/", "/tmpl-syntax/"):
        assert c.get(url).status_code == 500
    a, b, s = sent(server)
    assert a["exception"]["type"] == "ZeroDivisionError" and a["tags"]["route"] == "/tmpl-response/"
    assert b["exception"]["type"] == "ZeroDivisionError" and b["tags"]["route"] == "/tmpl-render/"
    assert s["exception"]["type"] == "TemplateSyntaxError"
    assert any(f["function"] == "value" for f in a["exception"]["stacktrace"])


def test_drf_unexpected_exception_reported_once_and_response_unchanged(server, init):
    from rest_framework.test import APIClient

    init(integrations=["django", "logging"])
    r = APIClient(raise_request_exception=False).get("/api/boom/3/", HTTP_AUTHORIZATION="Bearer s3cret",
                                                    HTTP_COOKIE="sid=abc", HTTP_X_API_KEY="k-123", HTTP_X_TRACE="t1")
    assert r.status_code == 500
    (e,) = sent(server)
    assert e["exception"]["value"] == "drf boom 3"
    assert_tags(e, "django", "/api/boom/<int:pk>/")
    assert_request(e, "/api/boom/3/")
    assert e["tags"]["drf_view"] == "DrfBoom"


def test_drf_converted_5xx_reported_4xx_ignored(server, init):
    from rest_framework.test import APIClient

    init(integrations=["django"])
    c = APIClient(raise_request_exception=False)
    down = c.get("/api/down/")
    assert down.status_code == 503 and down.json() == {"detail": "backend down"}  # DRF's own response
    assert c.get("/api/notfound/").status_code == 404
    assert c.get("/api/invalid/").status_code == 400
    assert c.get("/api/forbidden/").status_code in (401, 403)
    assert c.get("/api/dj404/").status_code == 404
    assert c.get("/api/ok/").status_code == 200
    (e,) = sent(server)
    assert e["exception"]["type"] == "Down" and e["tags"]["route"] == "/api/down/"


def test_drf_handler_without_middleware_still_reports(server, init):
    from rest_framework.test import APIClient

    init(integrations=["django"])
    with override_settings(MIDDLEWARE=[]):
        c = APIClient(raise_request_exception=False)
        assert c.get("/api/boom/1/", HTTP_X_TRACE="t1").status_code == 500
        assert c.get("/api/down/").status_code == 503
        assert c.get("/api/notfound/").status_code == 404
    boom, down = sent(server)
    assert boom["exception"]["value"] == "drf boom 1" and boom["request"]["url"].endswith("/api/boom/1/")
    assert boom["tags"]["framework"] == "django" and boom["tags"]["route"] == "/api/boom/<int:pk>/"
    assert down["exception"]["type"] == "Down"


def test_drf_handler_wrapping_a_custom_handler(server, init):
    from rest_framework.response import Response
    from rest_framework.views import exception_handler

    from insight_sdk.integrations.django import wrap_drf_exception_handler

    init()

    def mine(exc, context):
        if isinstance(exc, ValueError):
            return Response({"custom": str(exc)}, status=500)
        return exception_handler(exc, context)

    wrapped = wrap_drf_exception_handler(mine)
    resp = wrapped(ValueError("x"), {})
    assert resp.status_code == 500 and resp.data == {"custom": "x"}
    assert [e["exception"]["value"] for e in sent(server)] == ["x"]


def test_async_view_and_async_middleware(server, init):
    init(integrations=["django"])
    r = asyncio.run(AsyncClient(raise_request_exception=False).get("/aitems/4/"))
    assert r.status_code == 500
    (e,) = sent(server)
    assert e["exception"]["value"] == "django async boom 4" and e["tags"]["async_view"] == "1"
    assert e["tags"]["route"] == "/aitems/<int:pk>/"


def test_concurrent_requests_do_not_share_scope(server, init):
    init(integrations=["django"])
    results = []

    def hit(n):
        results.append(client().get("/slow/%d/" % n).status_code)

    threads = [threading.Thread(target=hit, args=(n,)) for n in (1, 2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == [500, 500]
    events = {e["exception"]["value"]: e for e in sent(server)}
    assert events["slow boom 1"]["tags"]["n"] == "1" and events["slow boom 2"]["tags"]["n"] == "2"
    assert events["slow boom 1"]["request"]["url"].endswith("/slow/1/")


def test_scope_is_discarded_after_request(server, init):
    init(integrations=["django"])
    client().get("/items/1/")
    insight_sdk.capture_message("after")
    last = sent(server)[-1]
    assert "view" not in last["tags"] and "route" not in last["tags"]


def test_django_performance_transactions(server, init):
    from fwutil import assert_perf

    init(integrations=["django"])
    c = client()
    assert c.get("/ok/").status_code == 200
    assert c.get("/items/5/").status_code == 500 and c.get("/items/6/").status_code == 500
    assert c.get("/missing/").status_code == 404  # Http404 inside a matched view: counted, not an error
    assert c.get("/nowhere/123/").status_code == 404  # unresolved URL
    assert c.get("/legacy/12/").status_code == 500
    assert_perf(server, {
        "GET /ok/": (1, 0), "GET /items/<int:pk>/": (2, 2), "GET /missing/": (1, 0), "GET <unmatched>": (1, 0),
        "GET /legacy/(?P<pk>\\d+)/$": (1, 1),
    })


def test_django_rest_framework_performance_transactions(server, init):
    from fwutil import assert_perf

    init(integrations=["django"])
    c = client()
    assert c.get("/api/ok/").status_code == 200
    assert c.get("/api/boom/3/").status_code == 500
    assert c.get("/api/notfound/").status_code == 404
    assert c.get("/api/invalid/").status_code == 400
    assert c.get("/api/down/").status_code == 503
    assert_perf(server, {
        "GET /api/ok/": (1, 0), "GET /api/boom/<int:pk>/": (1, 1), "GET /api/notfound/": (1, 0), "GET /api/invalid/": (1, 0),
        "GET /api/down/": (1, 1),
    })


def test_django_async_view_performance_transaction(server, init):
    from fwutil import assert_perf

    init(integrations=["django"])
    r = asyncio.run(AsyncClient(raise_request_exception=False).get("/aitems/4/"))
    assert r.status_code == 500
    assert_perf(server, {"GET /aitems/<int:pk>/": (1, 1)})


def test_django_inside_a_wsgi_middleware_is_one_transaction(server, init):
    from django.core.handlers.wsgi import WSGIHandler
    from fwutil import assert_perf
    from insight_sdk.integrations.wsgi import InsightWSGIMiddleware
    from test_performance import call_wsgi

    init(integrations=["django"])
    app = InsightWSGIMiddleware(WSGIHandler())  # Django middleware inside a WSGI middleware
    assert call_wsgi(app, "/ok/")[0].startswith("200")
    assert call_wsgi(app, "/items/9/")[0].startswith("500")
    assert call_wsgi(app, "/nowhere/")[0].startswith("404")
    assert_perf(server, {"GET /ok/": (1, 0), "GET /items/<int:pk>/": (1, 1), "GET <unmatched>": (1, 0)})


def test_django_adopts_incoming_traceparent(server, init):
    import re

    init(integrations=["django", "logging"])
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    c = client()
    assert c.get("/ok/", HTTP_TRACEPARENT=tp).status_code == 200
    assert c.get("/items/5/", HTTP_TRACEPARENT=tp).status_code == 500
    assert c.get("/ok/", HTTP_TRACEPARENT="not-a-header").status_code == 200
    assert insight_sdk.flush(5)
    samples = [s for b in server.metrics for s in b.get("samples", [])]
    adopted = [s for s in samples if s["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"]
    assert {s["name"] for s in adopted} == {"GET /ok/", "GET /items/<int:pk>/"}
    for s in adopted:
        assert s["parent_span_id"] == "00f067aa0ba902b7" and s["kind"] == "server" and s["op"] == "http.server"
        assert re.match("^[0-9a-f]{16}$", s["span_id"])
    other = [s for s in samples if s["trace_id"] != "4bf92f3577b34da6a3ce929d0e0e4736"]
    assert len(other) == 1 and "parent_span_id" not in other[0]
    (err,) = [e for e in server.events if e.get("exception")]
    assert err["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
