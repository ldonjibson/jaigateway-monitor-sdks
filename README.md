# JaiGateway Monitor SDKs

| Platform | Folder | Verified |
|---|---|---|
| JavaScript (browser, Node, React, Express, Next.js) | [javascript/](javascript/) | 54 unit tests + live browser/Node tests incl. source maps (`e2e/sdk-js.spec.ts`); errors, cron jobs, performance, spans, traces and browser performance |
| Python: Django/DRF, Flask, FastAPI, Starlette, Litestar, Sanic, aiohttp, Tornado, Quart, Pyramid, Bottle, Falcon, Celery, RQ, Dramatiq, Huey, logging | [python/](python/) | 390 tests (each framework's own test client) + live servers; errors, cron jobs, performance, spans and traces; Python 3.9 only |
| PHP / Laravel | [php/](php/) | 109 tests on PHP 8.0-8.4 (via WebAssembly, minimal PHPUnit shim) + live wire checks; errors, cron jobs, performance, spans and traces; **real PHPUnit and a real Laravel app not run** (the Laravel middleware/provider wiring is desk-checked only) |
| Flutter / Dart | [flutter/](flutter/) | **Dart never executed**; wire format checked against the live server by hand |

**New here? Start with the [developer docs](https://monitor.jaigateway.com/docs/)**: create a key, install, verify, releases, source maps, limits and troubleshooting.

All speak the protocol in [PROTOCOL.md](PROTOCOL.md). None is published to a package registry yet: install from the [latest release](../../releases/latest) (zips, npm tarball, Python wheel, checksums).
