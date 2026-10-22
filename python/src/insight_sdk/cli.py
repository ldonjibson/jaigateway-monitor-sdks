"""``insight-sdk send-test-event --host https://... --api-key ak_...``"""
import argparse
import sys
from typing import List, Optional

from . import __version__

_HINTS = {
    401: "The API key is invalid or expired.",
    403: "The API key does not have write permission.",
    404: "Endpoint not found; check --host (it should be the app URL, e.g. https://insight.example.com).",
    429: "Rate limited or monthly quota exceeded.",
}


def send_test_event(args: argparse.Namespace) -> int:
    import insight_sdk

    client = insight_sdk.init(
        api_key=args.api_key, host=args.host, endpoint=args.endpoint, release=args.release,
        environment=args.environment, integrations=[], debug=args.debug, max_attempts=2, backoff_base=0.5,
    )
    if not client.enabled:
        print("error: --api-key and --host (or --endpoint) are required", file=sys.stderr)
        return 2
    print("Sending a test error to %s ..." % client.endpoint)
    try:
        raise RuntimeError("Insight SDK test event (insight-sdk send-test-event)")
    except RuntimeError:
        insight_sdk.capture_exception(tags={"test_event": "true"})
    done = insight_sdk.flush(args.timeout)
    status = client.transport.last_status if client.transport else None
    if done and status is not None and 200 <= status < 300:
        print("OK: the server accepted the event (HTTP %d). Look for it in the Errors page." % status)
        return 0
    if status is None:
        print("FAILED: could not reach the server (%s)." % (client.transport.last_error if client.transport else "?"), file=sys.stderr)
    else:
        print("FAILED: HTTP %d. %s" % (status, _HINTS.get(status, "")), file=sys.stderr)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="insight-sdk", description="Insight SDK tools")
    parser.add_argument("--version", action="version", version="insight-sdk " + __version__)
    sub = parser.add_subparsers(dest="command")
    t = sub.add_parser("send-test-event", help="send a test error to verify your setup")
    t.add_argument("--host", help="app URL, e.g. https://insight.example.com")
    t.add_argument("--endpoint", help="full ingest URL (instead of --host)")
    t.add_argument("--api-key", required=True)
    t.add_argument("--release")
    t.add_argument("--environment", default="test")
    t.add_argument("--timeout", type=float, default=15.0)
    t.add_argument("--debug", action="store_true")
    t.set_defaults(func=send_test_event)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
