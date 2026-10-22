import asyncio

import pytest

import insight_sdk
from fwutil import SECRET_HEADERS, assert_request, assert_tags, crumbs, sent

tornado = pytest.importorskip("tornado")
from tornado import web  # noqa: E402
from tornado.testing import AsyncHTTPTestCase, gen_test  # noqa: E402

from insight_sdk.integrations import tornado as insight_tornado  # noqa: E402
from insight_sdk.integrations.tornado import InsightMixin, regex_to_template  # noqa: E402


class ItemHandler(web.RequestHandler):
    async def get(self, item_id):
        insight_sdk.set_tag("item", item_id)
        await asyncio.sleep(0.05)
        raise ValueError("tornado boom " + item_id)


class NamedHandler(web.RequestHandler):
    def get(self, name):
        raise KeyError("named boom")


class ErrorHandler(web.RequestHandler):
    def get(self, code):
        raise web.HTTPError(int(code), "explicit %s" % code)


class OkHandler(web.RequestHandler):
    def get(self):
        self.write("fine")


class MixinHandler(InsightMixin, web.RequestHandler):
    def get(self, item_id):
        insight_sdk.set_tag("mixin", "1")
        raise RuntimeError("mixin boom")


def make_app():
    return web.Application([
        (r"/items/(\d+)", ItemHandler),
        (r"/named/(?P<name>\w+)", NamedHandler),
        (r"/error/(\d+)", ErrorHandler),
        (r"/ok", OkHandler),
        (r"/mixin/(\d+)", MixinHandler),
    ])


def test_regex_to_template():
    assert regex_to_template(r"/items/(\d+)$") == "/items/{0}"
    assert regex_to_template(r"/a/(?P<name>[^/]+)/b/(\d+)(?:/x)?") == "/a/{name}/b/{0}"
    assert regex_to_template(r"/static\.js") == "/static.js"


class SetupMode(AsyncHTTPTestCase):
    @pytest.fixture(autouse=True)
    def _fixtures(self, server, init):
        self.server_fx, self.init_fx = server, init

    def get_app(self):
        return make_app()

    def setUp(self):
        super().setUp()
        self.init_fx()
        insight_tornado.setup()
        insight_tornado.setup()  # wraps only once

    def tearDown(self):
        insight_tornado.uninstall()
        super().tearDown()

    def test_unhandled_error_route_template_and_normal_500(self):
        r = self.fetch("/items/7?token=abc&q=1", headers=SECRET_HEADERS)
        assert r.code == 500 and b"500: Internal Server Error" in r.body  # Tornado's own error page
        (e,) = sent(self.server_fx)
        assert e["exception"]["value"] == "tornado boom 7" and e["tags"]["item"] == "7"
        assert_tags(e, "tornado", "/items/{0}")
        assert_request(e, "/items/7")
        assert e["request"]["query"] == "token=[Filtered]&q=1"
        assert crumbs(e)[0]["message"] == "GET /items/7"

    def test_named_group_route(self):
        assert self.fetch("/named/bob").code == 500
        (e,) = sent(self.server_fx)
        assert e["tags"]["route"] == "/named/{name}"

    def test_http_errors(self):
        codes = [self.fetch(p).code for p in ("/error/404", "/error/400", "/error/403", "/ok", "/nowhere")]
        assert codes == [404, 400, 403, 200, 404]
        assert sent(self.server_fx) == []
        assert self.fetch("/error/503").code == 503
        (e,) = sent(self.server_fx)
        assert e["exception"]["type"] == "HTTPError" and e["tags"]["route"] == "/error/{0}"

    def test_performance_transactions(self):
        from fwutil import assert_perf

        codes = [self.fetch(p).code for p in ("/ok", "/ok", "/items/7", "/items/8", "/named/bob", "/error/404", "/error/503", "/nowhere/9")]
        assert codes == [200, 200, 500, 500, 500, 404, 503, 404]
        assert_perf(self.server_fx, {
            "GET /ok": (2, 0), "GET /items/{0}": (2, 2), "GET /named/{name}": (1, 1), "GET /error/{0}": (2, 1),
            "GET <unmatched>": (1, 0),
        })

    @gen_test
    async def test_concurrent_requests_isolated(self):
        a, b, c = await asyncio.gather(
            self.http_client.fetch(self.get_url("/items/1"), raise_error=False),
            self.http_client.fetch(self.get_url("/items/2"), raise_error=False),
            self.http_client.fetch(self.get_url("/ok"), raise_error=False),
        )
        assert (a.code, b.code, c.code) == (500, 500, 200)
        events = {e["exception"]["value"]: e for e in sent(self.server_fx)}
        assert events["tornado boom 1"]["tags"]["item"] == "1" and events["tornado boom 2"]["tags"]["item"] == "2"
        assert events["tornado boom 1"]["request"]["url"].endswith("/items/1")

    def test_mixin_and_setup_together_report_once(self):
        assert self.fetch("/mixin/3").code == 500
        (e,) = sent(self.server_fx)
        assert e["tags"]["mixin"] == "1" and e["tags"]["route"] == "/mixin/{0}"


class MixinOnly(AsyncHTTPTestCase):
    @pytest.fixture(autouse=True)
    def _fixtures(self, server, init):
        self.server_fx, self.init_fx = server, init

    def get_app(self):
        return make_app()

    def setUp(self):
        super().setUp()
        self.init_fx()

    def test_mixin_without_setup(self):
        r = self.fetch("/mixin/5", headers=SECRET_HEADERS)
        assert r.code == 500
        (e,) = sent(self.server_fx)
        assert e["exception"]["value"] == "mixin boom" and e["tags"]["mixin"] == "1"
        assert_tags(e, "tornado", "/mixin/{0}")
        assert_request(e, "/mixin/5")
        assert self.fetch("/ok").code == 200 and len(sent(self.server_fx)) == 1


class MixinOnlyPerf(MixinOnly):
    def test_mixin_without_setup_times_requests(self):
        from fwutil import assert_perf

        # only handlers that use the mixin are timed (/ok is a plain handler)
        assert [self.fetch(p).code for p in ("/ok", "/mixin/5", "/mixin/6")] == [200, 500, 500]
        assert_perf(self.server_fx, {"GET /mixin/{0}": (2, 2)})
