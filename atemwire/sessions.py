# SPDX-License-Identifier: LGPL-3.0-only
"""
How many sessions an ATEM has free, and so how many are in use, by everyone:
ATEM Software Control, panels, Companion, our own app, anything.

The switcher does not report its sessions: nothing in the protocol, the
state dump or ATEM Software Control says how many clients are connected
(looked for 2026-11-05). So this counts by headroom. It opens sessions one
at a time, each waiting for the switcher's answer to the last, until the
switcher answers a hello 0x03 ("no room"), then logs every one off with the
protocol goodbye. ``free`` is how many it got. ``in_use`` is the model's
maximum minus ``free``: derived, not reported, and only known once the
maximum for that model has been measured (run this on that model with
nothing else connected: ``free`` is then the maximum; record it in
``MAX_SESSIONS``).

Sessions already open are never touched. While the count runs (well under
a second on a healthy link) the table is full, so someone else connecting
in that moment can be turned away; they get the same 0x03 and can retry.

Run by hand, one switcher per run::

    python -m atemwire.sessions 192.0.2.10
    python -m atemwire.sessions 192.0.2.10 --json
"""

import argparse
import json
import logging
import sys
import threading
import time

from atemwire.connection import CONNECT_HELLO_TIMEOUT
from atemwire.messages.system_info import product_name
from atemwire.protocol import AtemProtocol
from atemwire.transport import UdpProtocol

# Sessions each model allows at most, measured with nothing else connected
# (the ``free`` of a count then). Empty until measured: never guessed.
MAX_SESSIONS = {}

# A switcher that never says "no room" is not filled past this.
DEFAULT_LIMIT = 16

# What the switcher's answer to a hello must not take longer than: two
# hellos (the transport re-sends one every 5 s), as connect() waits.
ANSWER_TIMEOUT = CONNECT_HELLO_TIMEOUT

# How long an accepted session gets to send its first packet, which gives
# its switcher-assigned id and starts the state dump (product name).
FIRST_PACKET_TIMEOUT = 2.0

WARNING = ('Opens sessions one at a time until the switcher has no room, '
           'then logs each one off. Sessions already open are not touched; '
           'for the second or so this takes, someone else connecting may be '
           'turned away and has to try again.')


class _Session:
    """One handshake session, pumped on its own thread so it is ACKed like
    any client's, until ``close``."""

    def __init__(self, ip, port):
        self.protocol = AtemProtocol(ip, port)
        self.transport = self.protocol.transport
        self.accept = None  # the payload of the switcher's 0x02 answer
        self._stop = threading.Event()
        handshake = self.transport._handshake

        def record(packet):
            if (packet.flags & UdpProtocol.FLAG_SYN and packet.data
                    and self.accept is None):
                self.accept = bytes(packet.data)
            handshake(packet)

        self.transport._handshake = record
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name='atem-session-count')

    def open(self):
        self.protocol.connect()
        self._thread.start()

    def _pump(self):
        while not self._stop.is_set():
            try:
                self.protocol.loop()
            except Exception:
                if self._stop.is_set():
                    return

    @property
    def refused(self):
        return self.transport.refused

    @property
    def accepted(self):
        return self.transport.state == UdpProtocol.STATE_ESTABLISHED

    @property
    def session_id(self):
        sid = self.transport.session_id
        return None if sid in (None, 0x1337) else sid

    def close(self):
        self._stop.set()
        try:
            self.transport.close_session()
        except Exception:
            pass
        try:
            self.transport.sock.close()
        except Exception:
            pass
        # Wake the pump now rather than at the transport's next tick.
        self.transport.thread_recv_queue.put(None)

    def join(self, timeout):
        if self._thread.is_alive():
            self._thread.join(timeout)


def _wait(cond, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.002)
    return cond()


def count_sessions(ip, *, port=9910, limit=DEFAULT_LIMIT,
                   answer_timeout=ANSWER_TIMEOUT, max_sessions=None):
    """Count the free sessions on the ATEM at ``ip``.

    Returns a dict:

    ``free``          sessions it could open (int)
    ``full``          True when the switcher answered "no room" (``free`` is
                      exact); False when it stopped at ``limit`` (``free``
                      is at least that) or the switcher stopped answering
    ``stopped``       ``'no room'``, ``'limit'`` or ``'no answer'``
    ``model``         the switcher's product name, or None
    ``max``           sessions that model allows (``max_sessions`` or
                      ``MAX_SESSIONS``), or None when not measured
    ``in_use``        ``max - free`` when both are known, else None
    ``session_ids``   the ids the switcher gave the sessions it accepted
    ``accepts``       the payload of each 0x02 answer, hex
    ``seconds``       how long the count held the sessions

    Every session it opens is logged off before it returns, on any path.
    """
    sessions = []
    stopped = 'limit'
    started = time.monotonic()
    model = None
    try:
        while len(sessions) < limit:
            s = _Session(ip, port)
            s.open()
            answered = _wait(lambda: s.refused or s.accepted, answer_timeout)
            if s.refused:
                s.close()
                stopped = 'no room'
                break
            if not answered:
                s.close()
                stopped = 'no answer'
                break
            sessions.append(s)
            _wait(lambda: s.session_id is not None, FIRST_PACKET_TIMEOUT)
            if model is None:
                _wait(lambda: product_name(s.protocol.mixerstate, None),
                      FIRST_PACKET_TIMEOUT)
                model = product_name(s.protocol.mixerstate, None) or None
    finally:
        held = time.monotonic() - started
        for s in sessions:
            s.close()
        for s in sessions:
            s.join(1.0)

    free = len(sessions)
    full = stopped == 'no room'
    if max_sessions is None and model:
        max_sessions = MAX_SESSIONS.get(model)
    in_use = max_sessions - free if (full and max_sessions is not None) else None
    if in_use is not None and in_use < 0:
        in_use = None
    return {
        'ip': ip,
        'free': free,
        'full': full,
        'stopped': stopped,
        'model': model,
        'max': max_sessions,
        'in_use': in_use,
        'session_ids': [s.session_id for s in sessions],
        'accepts': [s.accept.hex() if s.accept else None for s in sessions],
        'seconds': round(held, 3),
    }


def format_result(r):
    """The plain-text report of a ``count_sessions`` result."""
    name = f"{r['model']} ({r['ip']})" if r['model'] else r['ip']
    lines = [name]
    if r['stopped'] == 'no answer' and r['free'] == 0:
        lines.append('  no answer from the switcher (nothing at that address, '
                     'or the network drops the traffic)')
        return '\n'.join(lines)
    if r['full']:
        lines.append(f"  free     {r['free']}")
    elif r['stopped'] == 'limit':
        lines.append(f"  free     at least {r['free']} (stopped there; it "
                     f"still had room)")
    else:
        lines.append(f"  free     at least {r['free']} (the switcher stopped "
                     f"answering, so the count is not complete)")
    if r['in_use'] is not None:
        lines.append(f"  in use   {r['in_use']}   = max {r['max']} − free "
                     f"{r['free']} (worked out; the switcher does not "
                     f"report it)")
    elif r['full'] and r['max'] is None:
        lines.append('  in use   unknown: the most sessions this model allows '
                     'is not measured yet. Run this once with nothing else '
                     'connected; that free count is the max.')
    else:
        lines.append('  in use   unknown')
    lines.append(f"  took     {r['seconds']:.1f} s, every session logged off")
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='python -m atemwire.sessions',
        description='How many sessions one ATEM has free and in use, by '
                    'everyone. ' + WARNING)
    ap.add_argument('ip', help='the switcher (one per run)')
    ap.add_argument('--port', type=int, default=9910)
    ap.add_argument('--limit', type=int, default=DEFAULT_LIMIT,
                    help=f'open at most this many (default {DEFAULT_LIMIT})')
    ap.add_argument('--max', type=int, default=None, dest='max_sessions',
                    help='the most sessions this model allows, if known '
                         'and not yet in MAX_SESSIONS')
    ap.add_argument('--json', action='store_true',
                    help='print the result as JSON (with the session ids '
                         'and accept payloads)')
    args = ap.parse_args(argv)
    if args.limit < 1:
        ap.error('--limit must be at least 1')

    # The count's own 0x03 and every session's buffer notice are expected
    # here, not news.
    for name in ('UdpTransport', 'AtemProtocol'):
        logging.getLogger(name).setLevel(logging.ERROR)
    if not args.json:
        print(f'{args.ip}: counting free sessions. {WARNING}', file=sys.stderr)
    r = count_sessions(args.ip, port=args.port, limit=args.limit,
                       max_sessions=args.max_sessions)
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print(format_result(r))
    return 0 if r['free'] or r['stopped'] != 'no answer' else 1


if __name__ == '__main__':
    sys.exit(main())
