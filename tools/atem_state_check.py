#!/usr/bin/env python3
"""Quick read-only state check on an ATEM.

Usage:
    python tools/atem_state_check.py 192.0.2.10
"""
import os, sys, time, logging
logging.basicConfig(level=logging.WARNING)
# From a checkout (python tools/<script>.py) the packages beside tools/
# import without an install.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from atemwire.protocol import AtemProtocol
from atemwire.ready import wait_ready


def pump(p, s):
    end = time.time() + s
    while time.time() < end:
        p.loop()
        time.sleep(0.005)


def main():
    if len(sys.argv) < 2:
        print('Usage: atem_state_check.py <ip>', file=sys.stderr)
        sys.exit(2)
    ip = sys.argv[1]
    proto = AtemProtocol(ip=ip)
    proto.connect()
    # L17 FIX (2026-07-06): goodbye on EVERY exit path (wait_ready timeout
    # included), not just the straight-line success — an abandoned bare
    # session isn't covered by the pool's atexit hook.
    import atexit

    def _clean_close():
        try:
            proto.transport.close_session()
        except Exception:
            pass
        try:
            proto.transport.sock.close()
        except Exception:
            pass
    atexit.register(_clean_close)
    wait_ready(proto, timeout=10.0)
    pump(proto, 1.5)
    mx = proto.mixerstate

    print('USK on-air state:')
    for k, entry in (mx.get('key-on-air', {}).get(0, {}) or {}).items():
        print(f'  USK{k}: enabled={getattr(entry, "enabled", "?")}')

    print('\nDSK state:')
    for k, entry in (mx.get('dkey-state', {}) or {}).items():
        print(f'  DSK{k}: on_air={getattr(entry, "on_air", "?")}, '
              f'transitioning={getattr(entry, "is_transitioning", "?")}')

    print('\nMacro slots used:')
    macros = mx.get('macro-properties', {}) or {}
    for k in sorted(macros.keys()):
        m = macros[k]
        if getattr(m, 'is_used', False):
            name = m.name if isinstance(m.name, bytes) else str(m.name)
            print(f'  slot {k}: name={name!r}')

    # Protocol goodbye BEFORE the socket close — an abandoned session
    # lingers on the switcher (and counts against its session table).
    try:
        proto.transport.close_session()
    except Exception:
        pass
    proto.transport.sock.close()

if __name__ == '__main__':
    # A signal unwinds instead of killing, so every session gets its goodbye.
    import _exit_signals
    _exit_signals.install()
    main()
