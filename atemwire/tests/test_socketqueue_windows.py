# SPDX-License-Identifier: LGPL-3.0-only
"""SocketQueue.get_waiting where there is no socket.MSG_DONTWAIT (Windows).

1.11.0 gave the UDP thread get_waiting, which read the wakeup bytes with
MSG_DONTWAIT. Python on Windows has no such flag: the thread ended with an
AttributeError at its first send, and no switcher connected from Windows
until bmdwire#83 asked select() instead. These run the queue the way
Windows does on any system: without the flag, and over the loopback TCP
pair a non-POSIX system gets in place of a socketpair.
"""
import select
import socket
import types

from atemwire import _socketqueue
from atemwire._socketqueue import SocketQueue


def _drain(q, count):
    """What the UDP thread does: wait for the queue in select(), take what
    is waiting, until `count` items have come."""
    got = []
    while len(got) < count:
        readable, _, _ = select.select([q], [], [], 5.0)
        assert readable, "nothing readable with %d of %d taken" % (len(got), count)
        got += q.get_waiting()
    return got


def test_get_waiting_needs_no_msg_dontwait(monkeypatch):
    monkeypatch.delattr(socket, 'MSG_DONTWAIT', raising=False)
    q = SocketQueue()
    try:
        assert q.get_waiting() == []
        for i in range(200):
            q.put(i)
        assert _drain(q, 200) == list(range(200))
        assert q.get_waiting() == []
    finally:
        q.close()


def test_the_loopback_pair_loses_nothing(monkeypatch):
    # The TCP pair can hand over fewer wakeup bytes than were put; the rest
    # come at the next wakeup, in order.
    monkeypatch.delattr(socket, 'MSG_DONTWAIT', raising=False)
    monkeypatch.setattr(_socketqueue, 'os', types.SimpleNamespace(name='nt'))
    for _ in range(20):
        q = SocketQueue()
        try:
            assert q._getsocket.family == socket.AF_INET
            for i in range(200):
                q.put(i)
            assert _drain(q, 200) == list(range(200))
            assert q.get_waiting() == []
        finally:
            q.close()
