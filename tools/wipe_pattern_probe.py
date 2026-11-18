#!/usr/bin/env python3
"""Live wire-value probe for the transition WIPE PATTERN — closes audit MSG-1.

Read-only. Connects to an ATEM and prints the live wipe-pattern WIRE INTEGER
whenever it changes, alongside what pyatem's two internal tables currently claim
that integer is named. Point ATEM Software Control (ASC) at the SAME switcher,
select each wipe pattern in the Wipe transition palette, and this prints the
wire integer ASC just set — so you can tie the integer to the name ASC writes in
its Save Switcher State XML without trusting the grid ordering.

RESOLVED 2026-08-31 via this probe + one ASC Save Switcher State per pattern.
ASC's ground-truth names for wire 0-3 turned out to be LeftToRightBar,
TopToBottomBar, HorizontalBarnDoor, VerticalBarnDoor — both internal tables
were wrong at 0-1 and the profile table was wrong at 0-3. Both tables + the
doc are now aligned and pinned by pyatem/tests/test_wipe_pattern_names.py.
The tool stays for re-verifying against other ATEM models:

    wire   ASC actual (ground truth)   was: profile / macro
    ----   -------------------------   ---------------------------
    0      LeftToRightBar              HorizontalBars / HorizontalBar
    1      TopToBottomBar              VerticalBars / VerticalBar
    2      HorizontalBarnDoor          LeftToRightBar / (ok)
    3      VerticalBarnDoor            TopToBottomBar / (ok)

Usage:

    python tools/wipe_pattern_probe.py 192.0.2.10

Then, in ASC on that switcher, set the transition style to WIPE and click each of
the first four wipe-pattern icons. For each: note the wire integer this prints,
then File -> Save Switcher State to wipe<N>.xml. Send back the four XMLs (or just
their `<WipeParameters pattern="...">` lines) plus which integer each showed.
Ctrl-C to stop.
"""
import os
import sys
import time
import logging

logging.basicConfig(level=logging.WARNING)
# From a checkout (python tools/<script>.py) the packages beside tools/
# import without an install.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from atemwire.protocol import AtemProtocol
from atemwire.ready import wait_ready
from atemwire.messages.transition import wipe_pattern
from atemwire.profile._enums import WIPE_PATTERN_NAMES
from atemwire.macrotransfer._helpers import _WIPE_PATTERN_NAMES as MACRO_NAMES


def pump(p, seconds):
    end = time.time() + seconds
    while time.time() < end:
        p.loop()
        time.sleep(0.005)


def _describe(n):
    prof = WIPE_PATTERN_NAMES[n] if 0 <= n < len(WIPE_PATTERN_NAMES) else '?'
    macro = MACRO_NAMES.get(n, '?')
    verdict = 'agree' if prof == macro else 'DIFFER  <-- capture this one'
    return f'profile={prof:<18} macro={macro:<18} {verdict}'


def main():
    if len(sys.argv) < 2:
        print('Usage: wipe_pattern_probe.py <ip>', file=sys.stderr)
        sys.exit(2)
    ip = sys.argv[1]
    proto = AtemProtocol(ip=ip)
    proto.connect()

    import atexit

    def _clean_close():
        # Session hygiene: goodbye before the socket closes on EVERY exit path.
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
    pump(proto, 1.0)

    print(f'Connected to {ip}. In ATEM Software Control, set the transition')
    print('style to WIPE and click each wipe-pattern icon; each change prints below.')
    print('For every DIFFER line, also do File -> Save Switcher State and keep the XML.')
    print('Ctrl-C to stop.\n')

    prev = object()
    try:
        while True:
            pump(proto, 0.3)
            cur = wipe_pattern(proto.mixerstate, 0)
            if cur != prev:
                stamp = time.strftime('%H:%M:%S')
                print(f'[{stamp}]  wire pattern = {cur:>2}   {_describe(cur)}')
                prev = cur
    except KeyboardInterrupt:
        print('\nstopping.')


if __name__ == '__main__':
    # A signal unwinds instead of killing, so every session gets its goodbye.
    import _exit_signals
    _exit_signals.install()
    main()
