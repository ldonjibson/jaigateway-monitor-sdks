"""Report uncaught exceptions in the main thread (sys.excepthook) and other threads (threading.excepthook)."""
import sys
import threading
from typing import Any, Callable

_IGNORED = (KeyboardInterrupt, SystemExit)


def setup(client: Any) -> Callable[[], None]:
    from .. import capture_exception

    original_sys = sys.excepthook
    original_thread = getattr(threading, "excepthook", None)

    def sys_hook(exc_type: Any, exc: Any, tb: Any) -> None:
        if not issubclass(exc_type, _IGNORED):
            capture_exception((exc_type, exc, tb), level="fatal", tags={"handled": "no"})
        original_sys(exc_type, exc, tb)

    sys.excepthook = sys_hook
    thread_hook = None
    if original_thread is not None:

        def thread_hook(args: Any) -> None:  # type: ignore[misc]
            if args.exc_type is not None and not issubclass(args.exc_type, _IGNORED) and args.exc_value is not None:
                capture_exception(
                    (args.exc_type, args.exc_value, args.exc_traceback),
                    level="fatal",
                    tags={"handled": "no"},
                    extra={"thread": getattr(args.thread, "name", None)},
                )
            original_thread(args)  # type: ignore[misc]

        threading.excepthook = thread_hook

    def undo() -> None:
        if sys.excepthook is sys_hook:
            sys.excepthook = original_sys
        if thread_hook is not None and threading.excepthook is thread_hook:
            threading.excepthook = original_thread  # type: ignore[assignment]

    return undo
