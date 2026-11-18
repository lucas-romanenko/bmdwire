"""Goodbye on SIGTERM, SIGHUP and SIGINT for the bench scripts here.

A process killed by a signal's default action exits at once: no ``finally``,
no ``atexit``, so every ATEM session it held is abandoned on the switcher
(no protocol goodbye), and abandoned sessions are what wedges a switcher's
session table. A closed ssh (SIGHUP), ``docker stop`` and a script started
in the background (SIGINT ignored) all hit that path. ``install()`` turns
the signal into an exception in the main thread instead, so ``with`` blocks,
``finally`` and ``atexit`` (the pool's goodbye to every pooled session) run.
SIGINT raises ``KeyboardInterrupt`` as Python always does; SIGTERM and
SIGHUP raise ``SystemExit(128 + signal)``. A second signal while unwinding
is ignored, so it cannot cut a goodbye in half (SIGKILL still works).

The same handler as AV Server's ``av_server/exit_signals.py``, where these
scripts lived until 2026-11-18. Standard library only.
"""
import signal
import sys
import threading

SIGNALS = tuple(getattr(signal, name) for name in ('SIGTERM', 'SIGHUP', 'SIGINT')
                if hasattr(signal, name))


def install():
    """Unwind (and so say goodbye) on SIGTERM / SIGHUP / SIGINT from now on.
    A no-op off the main thread, where Python cannot install one."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    unwinding = []

    def handler(signum, frame):
        if unwinding:
            return
        unwinding.append(signum)
        print(f'{signal.Signals(signum).name}: closing sessions before exit',
              file=sys.stderr, flush=True)
        if signum == getattr(signal, 'SIGINT', None):
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    return {sig: signal.signal(sig, handler) for sig in SIGNALS}
