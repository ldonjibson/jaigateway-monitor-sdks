"""``init(integrations=None)``: only already-imported frameworks that need no app object are enabled."""
import subprocess
import sys

import pytest

from test_integrations import FRAMEWORKS


def run_py(code):
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip(), out.stderr


def test_auto_enables_only_what_is_imported_and_logs_one_debug_line():
    code = ("import celery, flask, sys, insight_sdk\n"
            "insight_sdk.init(api_key='k', host='http://127.0.0.1:1', debug=True)\n"
            "print([m for m in %r if m in sys.modules])" % (FRAMEWORKS,))
    out, err = run_py(code)
    lines = [l for l in err.splitlines() if "auto-enabled" in l]
    assert len(lines) == 1
    assert lines[0].endswith("logging, excepthook, flask, celery"), lines
    assert "django" not in lines[0] and "rq" not in lines[0]
    for name in ("fastapi", "starlette", "rq", "sanic", "aiohttp", "tornado"):
        assert "'%s'" % name not in out  # init() imported no framework that the app had not imported


def test_auto_with_nothing_imported():
    out, err = run_py("import insight_sdk; insight_sdk.init(api_key='k', host='http://127.0.0.1:1', debug=True)")
    assert [l for l in err.splitlines() if "auto-enabled" in l][0].endswith("logging, excepthook")


def test_auto_rq_when_rq_already_imported():
    pytest.importorskip("rq")
    out, err = run_py("import rq, insight_sdk; insight_sdk.init(api_key='k', host='http://127.0.0.1:1', debug=True)")
    assert [l for l in err.splitlines() if "auto-enabled" in l][0].endswith("logging, excepthook, rq")


def test_explicit_names_for_app_integrations_explain_the_one_liner(server, init, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="insight_sdk"):
        init(integrations=["fastapi"])
    assert "init_app(app)" in caplog.text


def test_auto_module_selection():
    from insight_sdk.integrations import auto

    assert auto.select({"django": 1, "fastapi": 1}) == ["logging", "excepthook", "django"]
    assert auto.select({}) == ["logging", "excepthook"]
    assert "fastapi" in auto.EXPLICIT and "fastapi" not in dict(auto.AUTOMATIC)
