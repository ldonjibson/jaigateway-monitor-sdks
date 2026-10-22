import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

import insight_sdk


class FakeServer:
    """In-process ingest server. ``script`` is a list of responses consumed in order;
    afterwards ``default`` (201) is used. A response is (status, headers, body) or None to drop
    the connection. ``responder(request)`` can override everything."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.script: List[Optional[Tuple[int, Dict[str, str], Any]]] = []
        self.responder: Optional[Callable[[Dict[str, Any]], Any]] = None
        self.default: Tuple[int, Dict[str, str], Any] = (201, {}, {"success": True})
        # Performance metrics (POST /api/ingest/metrics/) are a separate stream with their own script/responder, so
        # error-event tests never see them and metrics tests never consume event responses.
        self.metrics_requests: List[Dict[str, Any]] = []
        self.metrics_script: List[Optional[Tuple[int, Dict[str, str], Any]]] = []
        self.metrics_responder: Optional[Callable[[Dict[str, Any]], Any]] = None
        self.metrics_default: Tuple[int, Dict[str, str], Any] = (201, {}, {"success": True})
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw)
                req = {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                    "events": body if isinstance(body, list) else [body],
                    "time": time.monotonic(),
                }
                is_metrics = self.path.split("?", 1)[0].rstrip("/").endswith("/ingest/metrics")
                with outer.lock:
                    if is_metrics:
                        outer.metrics_requests.append(req)
                        if outer.metrics_responder:
                            resp = outer.metrics_responder(req)
                        elif outer.metrics_script:
                            resp = outer.metrics_script.pop(0)
                        else:
                            resp = outer.metrics_default
                    else:
                        outer.requests.append(req)
                        if outer.responder:
                            resp = outer.responder(req)
                        elif outer.script:
                            resp = outer.script.pop(0)
                        else:
                            resp = outer.default
                if resp is None:
                    self.connection.close()
                    return
                status, headers, payload = resp
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.host = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    @property
    def events(self) -> List[Dict[str, Any]]:
        return [e for r in list(self.requests) for e in r["events"]]

    @property
    def metrics(self) -> List[Dict[str, Any]]:
        """Bodies of the metrics requests received so far."""
        return [r["body"] for r in list(self.metrics_requests)]

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(autouse=True)
def fresh_scope():
    from insight_sdk import scope

    scope._current.set(None)
    yield
    scope._current.set(None)


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.stop()


@pytest.fixture
def init(server):
    """init() against the fake server with fast retries and no auto integrations."""

    def _init(**kw: Any):
        opts = dict(api_key="ak_test", host=server.host, integrations=[], backoff_base=0.01, shutdown_timeout=5)
        opts.update(kw)
        return insight_sdk.init(**opts)

    yield _init
    insight_sdk.close(1)


def wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()
