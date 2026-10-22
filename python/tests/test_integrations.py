import logging
import sys
import threading
import types
from wsgiref.util import setup_testing_defaults

import pytest

import insight_sdk
from insight_sdk import cli
from insight_sdk.integrations.asgi import InsightASGIMiddleware
from insight_sdk.integrations.wsgi import InsightWSGIMiddleware


def sent(server):
    assert insight_sdk.flush(5)
    return server.events


# --- logging ---------------------------------------------------------------------------
def test_logging_error_becomes_event_lower_levels_become_breadcrumbs(server, init):
    init(integrations=["logging"])
    log = logging.getLogger("myapp.payments")
    log.setLevel(logging.DEBUG)
    log.debug("ignored debug")
    log.info("starting charge")
    log.warning("slow gateway")
    log.error("charge failed for order %s", 7)
    try:
        1 / 0
    except ZeroDivisionError:
        log.exception("unexpected")
    ev = sent(server)
    assert len(ev) == 2
    msg, exc = ev
    assert msg["description"] == "charge failed for order 7" and msg["level"] == "error"
    assert msg["tags"]["logger"] == "myapp.payments"
    assert [(b["category"], b["level"], b["message"]) for b in msg["breadcrumbs"]] == [
        ("log", "info", "starting charge"), ("log", "warning", "slow gateway")]
    assert exc["exception"]["type"] == "ZeroDivisionError" and exc["extra"]["log_message"] == "unexpected"


def test_logging_ignores_sdk_own_logger_and_does_not_recurse(server, init):
    init(integrations=["logging"])
    logging.getLogger("insight_sdk").error("sdk internal problem")
    logging.getLogger("insight_sdk.transport").critical("another")
    assert sent(server) == []


def test_logging_handler_removed_on_close(server, init):
    init(integrations=["logging"])
    n = len(logging.getLogger().handlers)
    insight_sdk.close()
    assert len(logging.getLogger().handlers) == n - 1


def test_logging_plus_explicit_capture_is_deduped(server, init):
    init(integrations=["logging"])
    log = logging.getLogger("dup")
    try:
        raise ValueError("once")
    except ValueError as e:
        log.exception("logged")
        insight_sdk.capture_exception(e)  # e.g. a middleware seeing the same exception
    assert len(sent(server)) == 1


# --- excepthook ------------------------------------------------------------------------
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_excepthooks_capture_and_restore(server, init, capsys):
    before_sys, before_thread = sys.excepthook, threading.excepthook
    init(integrations=["excepthook"])
    assert sys.excepthook is not before_sys
    try:
        raise RuntimeError("main thread crash")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    def target():
        raise OSError("thread crash")

    t = threading.Thread(target=target, name="worker-x")
    t.start()
    t.join()
    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        sys.excepthook(*sys.exc_info())  # not reported
    ev = sent(server)
    assert [(e["exception"]["type"], e["level"]) for e in ev] == [("RuntimeError", "fatal"), ("OSError", "fatal")]
    assert ev[1]["extra"]["thread"] == "worker-x"
    insight_sdk.close()
    assert sys.excepthook is before_sys and threading.excepthook is before_thread


# --- WSGI --------------------------------------------------------------------------------
def run_wsgi(app, path="/boom", headers=None):
    environ = {}
    setup_testing_defaults(environ)
    environ.update({"PATH_INFO": path, "QUERY_STRING": "a=1&b=2", "HTTP_HOST": "shop.test",
                    "HTTP_AUTHORIZATION": "Bearer secret", "HTTP_COOKIE": "sid=abc",
                    "HTTP_X_API_KEY": "k", "HTTP_USER_AGENT": "pytest"})
    status = []
    result = app(environ, lambda s, h, e=None: status.append(s))
    try:
        return status, b"".join(result)
    finally:
        if hasattr(result, "close"):
            result.close()


def test_wsgi_middleware_captures_with_scrubbed_request(server, init):
    init()

    def app(environ, start_response):
        insight_sdk.add_breadcrumb("handling request")
        raise ValueError("wsgi boom")

    with pytest.raises(ValueError):
        run_wsgi(InsightWSGIMiddleware(app))
    (e,) = sent(server)
    assert e["exception"]["value"] == "wsgi boom"
    r = e["request"]
    assert r["url"] == "http://shop.test/boom" and r["method"] == "GET" and r["query"] == "a=1&b=2"
    assert r["headers"] == {"host": "shop.test", "user-agent": "pytest"}
    assert e["breadcrumbs"][-1]["message"] == "handling request"


def test_wsgi_error_while_streaming_and_scope_cleanup(server, init):
    init()

    def app(environ, start_response):
        start_response("200 OK", [])
        insight_sdk.set_tag("leak", "1")
        yield b"ok"
        raise RuntimeError("mid-stream")

    with pytest.raises(RuntimeError):
        run_wsgi(InsightWSGIMiddleware(app))
    (e,) = sent(server)
    assert e["exception"]["type"] == "RuntimeError" and e["tags"]["leak"] == "1"
    insight_sdk.capture_message("later")
    assert "leak" not in sent(server)[-1]["tags"]  # the request scope was discarded


# --- ASGI (Starlette) ----------------------------------------------------------------------
def test_asgi_middleware_with_starlette(server, init):
    pytest.importorskip("httpx")
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    init()

    async def boom(request):
        insight_sdk.add_breadcrumb("in handler", category="custom")
        raise ValueError("asgi boom")

    async def fine(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/boom", boom), Route("/fine", fine)])
    app.add_middleware(InsightASGIMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/fine").status_code == 200
    r = client.get("/boom?x=1", headers={"Authorization": "Bearer s", "Cookie": "a=b", "X-Trace": "t1"})
    assert r.status_code == 500
    (e,) = sent(server)
    assert e["exception"]["value"] == "asgi boom"
    assert e["request"]["url"].endswith("/boom") and e["request"]["query"] == "x=1"
    assert e["request"]["method"] == "GET"
    assert e["request"]["headers"]["x-trace"] == "t1"
    assert not {"authorization", "cookie"} & set(e["request"]["headers"])
    assert e["breadcrumbs"][-1]["message"] == "in handler"


# --- Flask ---------------------------------------------------------------------------------
def test_flask_integration(server, init):
    flask = pytest.importorskip("flask")
    from insight_sdk.integrations import flask as insight_flask

    init(integrations=["flask"], send_default_pii=False)
    app = flask.Flask("t")
    insight_flask.init_app(app)

    @app.route("/boom")
    def boom():
        insight_sdk.set_tag("handler", "boom")
        raise ValueError("flask boom")

    @app.route("/ok")
    def ok():
        insight_sdk.capture_message("from ok")
        return "ok"

    c = app.test_client()
    assert c.get("/boom?q=1", headers={"Authorization": "x", "X-Trace": "t"}).status_code == 500
    assert c.get("/ok").status_code == 200
    boom_ev, ok_ev = sent(server)
    assert boom_ev["exception"]["value"] == "flask boom" and boom_ev["tags"]["handler"] == "boom"
    assert boom_ev["tags"]["route"] == "/boom"  # the integration now tags the rule template itself
    assert boom_ev["request"]["url"].endswith("/boom") and boom_ev["request"]["query"] == "q=1"
    assert boom_ev["request"]["headers"]["x-trace"] == "t" and "authorization" not in boom_ev["request"]["headers"]
    assert "handler" not in ok_ev["tags"] and ok_ev["tags"]["route"] == "/ok"  # per-request scope isolation


# --- Django --------------------------------------------------------------------------------
def configure_django():
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=False, SECRET_KEY="x", ALLOWED_HOSTS=["*"], ROOT_URLCONF="test_integrations",
            INSTALLED_APPS=[],
            MIDDLEWARE=["test_integrations.FakeAuthMiddleware", "insight_sdk.integrations.django.InsightMiddleware"],
        )
        import django

        django.setup()


class _User:
    is_authenticated = True
    pk = 5
    email = "ada@example.com"

    def get_username(self):
        return "ada"


class FakeAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.user = _User()
        return self.get_response(request)


def dj_boom(request):
    insight_sdk.set_tag("view", "boom")
    raise ValueError("django boom")


def dj_ok(request):
    from django.http import HttpResponse

    insight_sdk.capture_message("in view")
    return HttpResponse("ok")


def dj_404(request):
    from django.http import Http404

    raise Http404()


try:
    from django.urls import path

    urlpatterns = [path("boom/", dj_boom), path("ok/", dj_ok), path("missing/", dj_404)]
except ImportError:  # pragma: no cover
    urlpatterns = []


@pytest.mark.parametrize("pii", [False, True])
def test_django_middleware(server, init, pii):
    pytest.importorskip("django")
    configure_django()
    from django.test import Client

    init(integrations=["django", "logging"], send_default_pii=pii)  # logging too: must still be ONE event
    c = Client(raise_request_exception=False)
    assert c.get("/boom/?z=1", HTTP_AUTHORIZATION="Bearer s", HTTP_COOKIE="a=b", HTTP_X_TRACE="t").status_code == 500
    assert c.get("/ok/").status_code == 200
    assert c.get("/missing/").status_code == 404
    boom_ev, ok_ev = sent(server)  # 404 is not reported; django's log + signal are deduped
    assert boom_ev["exception"]["value"] == "django boom" and boom_ev["tags"]["view"] == "boom"
    r = boom_ev["request"]
    assert r["url"].endswith("/boom/") and r["method"] == "GET" and r["query"] == "z=1"
    assert r["headers"]["x-trace"] == "t" and not {"authorization", "cookie"} & set(r["headers"])
    if pii:
        assert boom_ev["user"]["email"] == "ada@example.com" and boom_ev["user"]["id"] == "5"
    else:
        assert "user" not in boom_ev
    assert "view" not in ok_ev["tags"] and ok_ev["request"]["url"].endswith("/ok/")


def test_django_async_middleware(server, init):
    pytest.importorskip("django")
    configure_django()
    import asyncio
    from django.test import AsyncClient

    init(integrations=["django"])
    asyncio.run(AsyncClient(raise_request_exception=False).get("/ok/"))
    assert [e["description"] for e in sent(server)] == ["in view"]


# --- Celery (fake signals; the real package is not required) ------------------------------------
def test_celery_signals(server, init, monkeypatch):
    class Signal:
        def __init__(self):
            self.receivers = []

        def connect(self, fn, weak=False, dispatch_uid=None):
            self.receivers.append(fn)

    signals = types.SimpleNamespace(task_prerun=Signal(), task_postrun=Signal(), task_failure=Signal())
    celery = types.ModuleType("celery")
    celery.signals = signals
    monkeypatch.setitem(sys.modules, "celery", celery)
    monkeypatch.setitem(sys.modules, "celery.signals", signals)
    init(integrations=["celery"])
    task = types.SimpleNamespace(name="app.tasks.send_mail")
    signals.task_prerun.receivers[0](task_id="t1", task=task)
    try:
        raise ValueError("task failed")
    except ValueError as e:
        signals.task_failure.receivers[0](sender=task, task_id="t1", exception=e, traceback=e.__traceback__, args=("secret",))
    signals.task_postrun.receivers[0](task_id="t1")
    insight_sdk.capture_message("after task")
    failed, after = sent(server)
    assert failed["tags"]["celery_task"] == "app.tasks.send_mail" and failed["extra"]["celery_task_id"] == "t1"
    assert "secret" not in str(failed)
    assert "celery_task" not in after["tags"]


# --- auto-enable ------------------------------------------------------------------------------
def test_auto_integrations_only_for_imported_frameworks(server, init):
    sys.modules.pop("celery", None)
    sys.modules.pop("rq", None)
    c = init(integrations=None)
    assert len(c._teardowns) == 2  # logging + excepthook; flask/django/celery are imported lazily and only if used
    insight_sdk.close()


FRAMEWORKS = ("django", "rest_framework", "flask", "celery", "starlette", "fastapi", "litestar", "sanic", "aiohttp",
              "tornado", "quart", "pyramid", "bottle", "falcon", "rq", "dramatiq", "huey", "redis")


def test_base_package_does_not_import_frameworks():
    import subprocess

    code = ("import sys, insight_sdk; insight_sdk.init(api_key='k', host='http://x', integrations=None);"
            "import insight_sdk.integrations, insight_sdk.integrations.auto;"
            "bad=[m for m in %r if m in sys.modules]; print(bad)" % (FRAMEWORKS,))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "[]", out.stderr


# --- CLI ---------------------------------------------------------------------------------------
def test_cli_send_test_event_ok(server, capsys):
    rc = cli.main(["send-test-event", "--host", server.host, "--api-key", "ak_x", "--release", "r1"])
    insight_sdk.close()
    assert rc == 0
    assert "OK" in capsys.readouterr().out
    (e,) = server.events
    assert e["release"] == "r1" and e["exception"]["type"] == "RuntimeError"


def test_cli_send_test_event_reports_failure(server, capsys):
    server.default = (401, {}, {"success": False})
    rc = cli.main(["send-test-event", "--host", server.host, "--api-key", "bad"])
    insight_sdk.close()
    assert rc == 1
    assert "401" in capsys.readouterr().err
