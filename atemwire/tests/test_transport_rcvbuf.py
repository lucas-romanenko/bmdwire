# SPDX-License-Identifier: LGPL-3.0-only
"""The receive buffer request never fails a connect.

The transport asks for a 16 MB SO_RCVBUF. Linux caps it silently, but macOS
12 refuses anything above kern.ipc.maxsockbuf with errno 55 (ENOBUFS), and
the bare setsockopt raised out of ``UdpProtocol.__init__``, so no switcher
could be connected from a Mac on Monterey. The transport now steps down
16, 8, 4, 1 MB and keeps the first size the system takes.
"""
import errno
import socket

from atemwire import transport
from atemwire.transport import UdpProtocol


class _RefusingSock:
    """A UDP socket that refuses SO_RCVBUF above ``limit``, as macOS does."""

    def __init__(self, limit):
        self.limit = limit
        self.rcvbuf = None

    def settimeout(self, _timeout):
        pass

    def setsockopt(self, level, name, value):
        if name == socket.SO_RCVBUF:
            if value > self.limit:
                raise OSError(errno.ENOBUFS, 'No buffer space available')
            self.rcvbuf = value

    def close(self):
        pass


def _protocol_on(monkeypatch, sock):
    monkeypatch.setattr(transport.socket, 'socket', lambda *_args: sock)
    return UdpProtocol('10.0.0.99')


def test_a_refused_receive_buffer_falls_back_to_one_the_system_takes(monkeypatch):
    sock = _RefusingSock(limit=8 * 1024 * 1024)
    p = _protocol_on(monkeypatch, sock)
    assert p.sock is sock
    assert sock.rcvbuf == 8 * 1024 * 1024


def test_no_size_taken_still_constructs(monkeypatch):
    sock = _RefusingSock(limit=0)
    p = _protocol_on(monkeypatch, sock)
    assert p.sock is sock
    assert sock.rcvbuf is None
