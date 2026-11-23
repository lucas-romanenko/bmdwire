# SPDX-License-Identifier: LGPL-3.0-only
"""A connect that cannot succeed gives up on time on Windows too.

Two things kept a failing connect waiting on Windows, both measured on a
Windows runner (1.14.3; Linux gave up after 5 s, 10 s and 10 s):

- A switcher that welcomes and then sends nothing: 10 to 40 s, the cap, in
  place of 5. With nothing arriving, ``wait_ready`` looks only when the
  transport's 5 s silence tick wakes it, and its limits are whole ticks. On
  a clock that steps in 15.6 ms the tick reads exactly 5.000 s after the
  answer, and the answer the same instant as the start of the wait; the
  tests were strict (``>``), so each tick waited for the next.
- An address with nothing on 9910 (a wrong address, a switcher still
  booting): 41 s, the cap, in place of 10. Windows hands the ICMP
  port-unreachable to the next ``recvfrom`` as WSAECONNRESET, which ended
  the UDP thread and every re-hello with it.
"""
import socket
import time
import types

import pytest

from atemwire import ready
from atemwire.ready import wait_ready
from atemwire.transport import UdpProtocol

TICK = 5.0


class _TickProtocol:
    """A protocol whose loop() returns only on the silence tick, on a clock
    that reads whole ticks: what wait_ready sees of a quiet switcher on
    Windows."""

    def __init__(self, clock, answers_at=()):
        self.clock = clock
        self.answers_at = answers_at          # loop numbers the hello is answered in
        self.transport = types.SimpleNamespace(last_rx_monotonic=0.0, refused=False)
        self.mixerstate = {}
        self.initialized = False
        self.loops = 0

    def on(self, _event, _handler):
        return 1

    def off(self, _event, _handler_id):
        pass

    def loop(self):
        self.clock.t += TICK
        self.loops += 1
        if self.loops in self.answers_at:
            self.transport.last_rx_monotonic = self.clock.t


@pytest.fixture
def clock(monkeypatch):
    c = types.SimpleNamespace(t=1000.0)
    monkeypatch.setattr(ready, 'time', types.SimpleNamespace(
        monotonic=lambda: c.t, sleep=lambda _s: None))
    return c


def test_a_welcome_then_silence_fails_at_the_first_tick(clock):
    p = _TickProtocol(clock)
    p.transport.last_rx_monotonic = clock.t   # welcomed the instant the wait starts
    with pytest.raises(TimeoutError, match='nothing from the switcher for 5.0s'):
        wait_ready(p, timeout=60, stall_timeout=5.0, hello_timeout=10.0, pump_interval=0)
    assert p.loops == 1


def test_no_answer_fails_at_the_second_tick(clock):
    p = _TickProtocol(clock)
    with pytest.raises(TimeoutError, match='nothing from the switcher for 10.0s'):
        wait_ready(p, timeout=60, stall_timeout=5.0, hello_timeout=10.0, pump_interval=0)
    assert p.loops == 2


def test_an_answer_to_the_second_hello_gets_its_own_stall(clock):
    p = _TickProtocol(clock, answers_at=(1,))
    with pytest.raises(TimeoutError, match='nothing from the switcher for 5.0s'):
        wait_ready(p, timeout=60, stall_timeout=5.0, hello_timeout=10.0, pump_interval=0)
    assert p.loops == 2


class _ResetSock:
    """recvfrom as Windows gives it after an ICMP port-unreachable."""

    def __init__(self, resets):
        self.resets = resets

    def recvfrom(self, _size):
        if self.resets:
            self.resets -= 1
            raise ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host')
        raise BlockingIOError

    def close(self):
        pass


def test_a_reset_on_recvfrom_is_not_a_datagram():
    p = UdpProtocol('127.0.0.1')
    p.sock.close()
    p.sock = _ResetSock(resets=3)
    try:
        p._drain_socket()                     # raised ConnectionResetError
        assert p.sock.resets == 0
    finally:
        p.thread_queue.close()


def test_nothing_on_the_port_keeps_the_udp_thread_and_its_hellos():
    """On the wire: a loopback port nothing listens on. On Windows every
    hello comes back as a reset; the thread has to outlive them and keep
    re-sending the hello on its silence tick."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    p = UdpProtocol('127.0.0.1', port=port, timeout=0.3)
    hellos = []
    connect = p.connect
    p.connect = lambda: (hellos.append(time.monotonic()), connect())[1]
    try:
        p.connect()
        if not p.thread.is_alive():
            p.thread.start()
        deadline = time.monotonic() + 5.0
        while len(hellos) < 4 and p.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert p.thread.is_alive(), "the UDP thread ended"
        assert len(hellos) >= 4, hellos
    finally:
        p.sock.close()
        p.thread_queue.close()
        p.thread.join(timeout=2.0)
