# SPDX-License-Identifier: MIT
"""SH-20a (2026-07-06 session hygiene): ``Hyperdeck.connect`` never leaks a
socket. A greeting-read failure closes and clears the socket it just opened;
a reconnect over a live connection says ``quit`` and closes the old socket
before opening the new one. Moved here from AV Server (2026-11-18), which
tested this client from the outside.
"""
from typing import List, Optional

import pytest

from hyperdeckwire.client import Hyperdeck


class _FakeHDSocket:
    """Minimal 9993 socket stand-in (same shape as the one in
    test_hyperdeckwire_client): ``script`` is one byte-chunk per recv."""

    def __init__(self, script: List[bytes]):
        self._script = list(script)
        self._inbox = b''
        self.sent = b''
        self.closed = False
        self._timeout: Optional[float] = None

    def settimeout(self, t):
        self._timeout = t

    def sendall(self, data):
        if self.closed:
            raise OSError('closed')
        self.sent += data

    def recv(self, n):
        if self.closed:
            return b''
        if not self._inbox:
            if not self._script:
                return b''  # EOF, not a hang
            self._inbox = self._script.pop(0)
        chunk, self._inbox = self._inbox[:n], self._inbox[n:]
        return chunk

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


GREETING = (
    b'500 connection info:\r\n'
    b'protocol version: 1.13\r\n'
    b'model: HyperDeck Studio HD Mini\r\n'
    b'\r\n'
)


def test_connect_greeting_failure_closes_socket_and_clears_sock():
    """EOF during the greeting read must close + clear the just-opened
    socket (pre-fix it lingered on the instance until GC)."""
    sock = _FakeHDSocket([])  # unit accepts, then closes before greeting
    hd = Hyperdeck('192.0.2.11', socket_factory=lambda *a, **kw: sock)
    with pytest.raises(ConnectionError):
        hd.connect()
    assert sock.closed
    assert hd._sock is None


def test_reconnect_over_live_connection_closes_old_socket_first():
    s1 = _FakeHDSocket([GREETING])
    s2 = _FakeHDSocket([GREETING])
    queue = [s1, s2]
    hd = Hyperdeck('192.0.2.11', socket_factory=lambda *a, **kw: queue.pop(0))
    hd.connect()
    assert hd._sock is s1
    hd.connect()  # pre-fix: overwrote _sock, s1 leaked open
    assert s1.closed
    assert b'quit\n' in s1.sent  # polite goodbye on the old session
    assert hd._sock is s2
    assert not s2.closed
