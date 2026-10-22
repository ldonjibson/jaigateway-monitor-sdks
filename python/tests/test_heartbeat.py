import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import insight_sdk
from insight_sdk import heartbeat


class _Ping(BaseHTTPRequestHandler):
    seen = []

    def _h(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        _Ping.seen.append((self.command, self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK\n")

    do_GET = do_POST = _h

    def log_message(self, *a):
        pass


def _server():
    srv = HTTPServer(("127.0.0.1", 0), _Ping)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def test_decorator_sends_start_then_success_and_returns_the_value():
    _Ping.seen.clear()
    srv, host = _server()
    try:
        @insight_sdk.monitor(host + "/api/ping/tok/")
        def job(x):
            return x * 2

        assert job(21) == 42
        assert [(m, p) for m, p, _ in _Ping.seen] == [("GET", "/api/ping/tok/start/"), ("GET", "/api/ping/tok/")]
    finally:
        srv.shutdown()


def test_failure_sends_the_traceback_and_still_raises():
    _Ping.seen.clear()
    srv, host = _server()
    try:
        try:
            with insight_sdk.monitor(host + "/api/ping/tok/start/"):  # a pasted ".../start/" URL still works
                raise ValueError("job broke")
        except ValueError:
            pass
        else:
            raise AssertionError("the job's exception must propagate")
        assert [p for _, p, _ in _Ping.seen] == ["/api/ping/tok/start/", "/api/ping/tok/fail/"]
        assert "ValueError: job broke" in _Ping.seen[1][2]
        assert _Ping.seen[1][0] == "POST"
    finally:
        srv.shutdown()


def test_bare_token_uses_the_configured_host_and_failures_never_raise():
    _Ping.seen.clear()
    srv, host = _server()
    try:
        assert insight_sdk.check_in("abc123") is False  # not initialised: host unknown
        insight_sdk.init(api_key="ak_x", host=host, integrations=[])
        assert insight_sdk.check_in("abc123", "log", "processed 5 rows") is True
        assert _Ping.seen[-1][:2] == ("POST", "/api/ping/abc123/log/")
        assert _Ping.seen[-1][2] == "processed 5 rows"
    finally:
        srv.shutdown()
        insight_sdk.close()
    # server gone: still no exception
    assert heartbeat.check_in(host + "/api/ping/x/", timeout=0.5) is False
    with insight_sdk.monitor(host + "/api/ping/x/"):
        pass
