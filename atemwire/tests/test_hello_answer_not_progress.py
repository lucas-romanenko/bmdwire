# SPDX-License-Identifier: LGPL-3.0-only
"""The answer to a hello the silence tick re-sent is not progress (bmdwire#89).

A switcher that welcomes the hello and then sends nothing is given up on at
the transport's silence tick. The tick re-sends the hello, and this switcher
answers that one too. ``wait_ready`` read the stall from
``last_rx_monotonic``, which the answer moves: whenever the UDP thread took
the answer before the pump looked, the stall clock restarted and the connect
waited one more tick, each tick the same race. Seen on Linux as
``test_one_lost_hello_is_not_a_failed_connect`` taking 15 s where it allows
12, and the quiet-switcher test taking 10 s where it allows 7.

The transport now notes, at the tick, how long the switcher had been
silent (``silence_tick``), and ``wait_ready`` judges the tick from that.
These tests make the UDP thread win every time.
"""
import time
import types

import pytest

from atemwire import ready
from atemwire.protocol import AtemProtocol
from atemwire.ready import wait_ready
from atemwire.tests.test_no_free_connection import NoRoomSwitcher, _LostHello

TICK = 5.0


class _AnsweringProtocol:
    """A protocol whose loop() returns only on the silence tick, the
    switcher's answer to the re-sent hello already taken by the UDP thread:
    the order that lost the race."""

    def __init__(self, clock, welcome_at=0, data_from=None):
        self.clock = clock
        self.welcome_at = welcome_at       # the tick whose hello is first answered
        self.data_from = data_from         # ticks before the dump arrives (-1: none)
        self.transport = types.SimpleNamespace(
            last_rx_monotonic=0.0, refused=False, silence_tick=None)
        self.mixerstate = {}
        self.initialized = False
        self.loops = 0

    def on(self, _event, _handler):
        return 1

    def off(self, _event, _handler_id):
        pass

    def loop(self):
        t = self.transport
        if self.data_from is not None and self.loops > self.data_from:
            # the switcher's state dump, a packet each pump
            self.clock.t += 0.01
            t.last_rx_monotonic = self.clock.t
            if self.loops > self.data_from + 50:
                self.initialized = True
                self.mixerstate['video-mode'] = object()
            self.loops += 1
            return
        self.clock.t += TICK
        self.loops += 1
        t.silence_tick = (self.clock.t, t.last_rx_monotonic)
        if self.loops >= self.welcome_at:
            self.clock.t += 0.001
            t.last_rx_monotonic = self.clock.t


@pytest.fixture
def clock(monkeypatch):
    c = types.SimpleNamespace(t=1000.0)
    monkeypatch.setattr(ready, 'time', types.SimpleNamespace(
        monotonic=lambda: c.t, sleep=lambda _s: None))
    return c


def _welcomed(p, clock):
    p.transport.last_rx_monotonic = clock.t   # welcomed the instant the wait starts


def test_a_quiet_switcher_answering_each_re_hello_fails_at_the_first_tick(clock):
    p = _AnsweringProtocol(clock)
    _welcomed(p, clock)
    with pytest.raises(TimeoutError, match='nothing from the switcher for 5.0s'):
        wait_ready(p, timeout=60, stall_timeout=5.0, hello_timeout=10.0, pump_interval=0)
    assert p.loops == 1


def test_a_lost_hello_then_a_quiet_switcher_fails_at_the_second_tick(clock):
    # the first hello is lost; the switcher welcomes the one the first tick
    # re-sent, then answers the next re-hello and nothing else
    p = _AnsweringProtocol(clock, welcome_at=1)
    with pytest.raises(TimeoutError, match='nothing from the switcher for 5.0s'):
        wait_ready(p, timeout=60, stall_timeout=5.0, hello_timeout=10.0, pump_interval=0)
    assert p.loops == 2


def test_a_dump_after_the_answer_to_a_re_hello_is_waited_for(clock):
    # not answered until the re-hello, inside hello_timeout, then the state
    # dump: the tick's verdict is "not yet", and the dump is progress
    p = _AnsweringProtocol(clock, welcome_at=1, data_from=0)
    assert 'video-mode' in wait_ready(p, timeout=60, stall_timeout=5.0, hello_timeout=10.0,
                                      pump_interval=0)


def test_a_tick_before_the_wait_is_not_judged(clock):
    p = _AnsweringProtocol(clock, data_from=-1)
    p.transport.silence_tick = (clock.t - 1.0, clock.t - 20.0)
    _welcomed(p, clock)
    assert 'video-mode' in wait_ready(p, timeout=60, stall_timeout=5.0, hello_timeout=10.0,
                                      pump_interval=0)


def _slow_pump(p, lag):
    """loop() that lets the UDP thread run before the pump looks, every time:
    the switcher's answer to the re-sent hello is taken first."""
    loop = p.loop

    def slow():
        loop()
        time.sleep(lag)
    p.loop = slow


@pytest.mark.parametrize('switcher', [NoRoomSwitcher, _LostHello], ids=['quiet', 'lost-hello'])
def test_on_the_wire_the_answer_to_the_re_hello_does_not_restart_the_stall(switcher):
    sw = switcher(code=0x02) if switcher is NoRoomSwitcher else switcher()
    with sw:
        p = AtemProtocol(ip=sw.ip)
        p.transport.timeout = 1.0             # the silence tick, 5 s shipped
        p.connect()
        _slow_pump(p, 0.2)
        try:
            t0 = time.monotonic()
            with pytest.raises(TimeoutError, match='nothing from the switcher for 0.5'):
                wait_ready(p, timeout=8, stall_timeout=0.5, hello_timeout=2.0, pump_interval=0.005)
            took = time.monotonic() - t0
        finally:
            p.transport.close_session()
            p.transport.sock.close()
    # quiet: the first tick; lost hello: the second. Before the fix the
    # answer to each re-hello restarted the clock, and both ran to the 8 s cap.
    ticks = 1 if switcher is NoRoomSwitcher else 2
    assert ticks - 0.1 < took < ticks + 0.6, (sw.hellos, took)
    assert sw.hellos >= ticks + 1, sw.hellos


def test_the_tick_is_noted_before_the_re_hello():
    """silence_tick is set before connect() sends the hello: an answer can
    never be taken between the hello and the note."""
    from atemwire.transport import UdpProtocol
    p = UdpProtocol('127.0.0.1', port=9, timeout=0.3)
    seen = []
    connect = p.connect
    p.connect = lambda: (seen.append(p.silence_tick), connect())[1]
    try:
        p.connect()
        p.last_rx_monotonic = 123.0
        p.thread.start() if not p.thread.is_alive() else None
        deadline = time.monotonic() + 3.0
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(seen) >= 2, seen
        at, last = seen[1]
        assert last == 123.0 and at <= time.monotonic()
    finally:
        p.sock.close()
        p.thread_queue.close()
        p.thread.join(timeout=2.0)
