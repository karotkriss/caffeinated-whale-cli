"""Make SIGTERM and SIGHUP unwind the stack exactly like Ctrl+C (SIGINT) does.

cwcli's destructive verbs undo their side effects in a ``finally``: ``migrate`` /
``apps update`` put a site into maintenance mode and take it back out, and
``apps checkout`` / ``apps install`` / ``apps update`` stand up a per-invocation
git credential bridge (a socket, a shim, a ``git config`` line) and tear it down.
Python's DEFAULT SIGINT handler raises :class:`KeyboardInterrupt`, which unwinds
those ``finally`` blocks - so Ctrl+C is safe. But SIGTERM (a plain ``kill``, a
service/orchestrator stop) and SIGHUP (a closed terminal window) kill the process
with their DEFAULT disposition - immediate death, no stack unwind - so the
``finally`` never runs. The result: the site is left stuck in maintenance mode
(HTTP 503 to every user, indefinitely) and the bridge leaks its socket/shim/config.

Installing a handler that raises :class:`KeyboardInterrupt` for those two signals
makes them unwind exactly like Ctrl+C, reusing the cleanup that already exists - no
per-verb teardown. The handler first restores the DEFAULT disposition for both
signals, so a SECOND termination signal arriving DURING cleanup still kills the
process (a wedged cleanup can always be forced) instead of re-entering the handler.

The long-running daemons (``utils/auto_inspect.py``, ``utils/cred_daemon.py``)
install their OWN SIGTERM handlers when they run, which override this one, so this
entrypoint handler never interferes with them.
"""

from __future__ import annotations

import signal
import threading

# The termination signals we convert to a clean unwind. SIGHUP does not exist on
# Windows, so it is resolved defensively.
_UNWIND_SIGNALS = tuple(
    s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)) if s is not None
)


def _unwind(_signum, _frame):
    # Restore the default disposition for every termination signal FIRST, so a
    # second one during cleanup kills us rather than re-raising into the unwind.
    for sig in _UNWIND_SIGNALS:
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
    raise KeyboardInterrupt


def install_unwind_handlers() -> None:
    """Make SIGTERM and SIGHUP unwind like SIGINT. Safe to call once at startup.

    A no-op off the main thread (``signal.signal`` is main-thread only) and a no-op
    for any signal the platform will not accept, so it never breaks an embedded or
    threaded caller.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in _UNWIND_SIGNALS:
        try:
            signal.signal(sig, _unwind)
        except (ValueError, OSError):
            pass
