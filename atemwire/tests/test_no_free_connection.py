# SPDX-License-Identifier: LGPL-3.0-only
"""A switcher with every slot taken answers the hello 0x03 ("no room").

A connect says so at once (``NoFreeConnection``) instead of re-sending the
hello until its timeout and reading as "offline"; and a connect waits for as
long as the switcher keeps sending, giving up only after
``CONNECT_STALL_TIMEOUT`` with nothing from it."""
import random
import socket
import struct
import threading
import time

import pytest

from atemwire import NoFreeConnection
from atemwire import connection as _connection
from atemwire.connection import ATEMConnection
from atemwire.pool import ATEMInstanceManager, acquire_connection
from atemwire.probe import probe
from atemwire.protocol import AtemProtocol
from atemwire.ready import wait_ready


class NoRoomSwitcher(threading.Thread):
    """Answers every hello on UDP 9910 with 0x03, like a full ATEM; or, with
    ``code=0x02``, accepts and then says nothing more."""

    def __init__(self, code=0x03):
        super().__init__(daemon=True)
        self.code = code
        self.ip = f'127.{random.randint(1, 254)}.{random.randint(1, 254)}.{random.randint(2, 254)}'
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.ip, 9910))
        self.sock.settimeout(0.1)
        self.hellos = 0
        self.acks = 0
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                data, peer = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 13:
                continue
            flags = data[0] >> 3
            if flags & 0x02 and data[12] == 0x01:
                self.hellos += 1
                self.sock.sendto(struct.pack('>HHHHHH', (0x02 << 11) | 20, 0x1337, 0, 0, 0, 0)
                                 + bytes([self.code, 0, 0, 0, 0, 0, 0, 0]), peer)
            elif flags & 0x10:
                self.acks += 1

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.join(1)
        self.sock.close()


@pytest.fixture
def clean_pool():
    yield
    for ip in list(ATEMInstanceManager._instances):
        ATEMInstanceManager._instances.pop(ip, None)


def test_a_full_switcher_is_named_at_once(clean_pool):
    with NoRoomSwitcher() as sw:
        conn = ATEMConnection('t')
        t0 = time.monotonic()
        assert conn.connect(sw.ip) is False
        took = time.monotonic() - t0
        assert took < 1.5, f'waited {took:.1f}s for a switcher that said no room'
        assert conn.connect_refused
        assert 'no free connection' in conn._connect_error
        assert sw.acks == 0, 'a refused hello is not ACKed'


def test_the_pool_raises_no_free_connection(clean_pool):
    with NoRoomSwitcher() as sw:
        t0 = time.monotonic()
        with pytest.raises(NoFreeConnection) as e:
            with acquire_connection(sw.ip):
                pass
        assert time.monotonic() - t0 < 1.5
        assert e.value.ip == sw.ip and 'no free connection on this switcher' in str(e.value)
        assert isinstance(e.value, RuntimeError)      # old `except RuntimeError` keeps working


def test_probe_reports_it(clean_pool):
    with NoRoomSwitcher() as sw:
        r = probe(sw.ip)
    assert r['connection_status'] is False and r['no_free_connection'] is True


def test_wait_ready_raises_it_for_a_bare_protocol():
    with NoRoomSwitcher() as sw:
        p = AtemProtocol(ip=sw.ip)
        p.connect()
        try:
            t0 = time.monotonic()
            with pytest.raises(NoFreeConnection):
                wait_ready(p, timeout=10, pump_interval=0.005)
            assert time.monotonic() - t0 < 1.5
        finally:
            p.transport.close_session()
            p.transport.sock.close()


def test_a_switcher_that_goes_quiet_fails_after_the_stall_not_the_cap(clean_pool, monkeypatch):
    monkeypatch.setattr(_connection, 'CONNECT_STALL_TIMEOUT', 0.6)
    monkeypatch.setattr(_connection, 'CONNECT_MAX', 30.0)
    with NoRoomSwitcher(code=0x02) as sw:       # welcomes, then sends no state
        conn = ATEMConnection('t')
        t0 = time.monotonic()
        assert conn.connect(sw.ip) is False
        took = time.monotonic() - t0
    # the transport wakes the pump on its 5 s silence tick, so a quiet
    # switcher is noticed within that, not at the 30 s cap
    assert 0.5 < took < 7.0, took
    assert not conn.connect_refused and 'nothing from the switcher' in conn._connect_error


class _Trickle:
    """A protocol whose state dump keeps arriving for ``seconds``, a packet
    every ``gap`` s, then completes: a big dump over a slow link."""

    def __init__(self, seconds, gap):
        self.transport = type('T', (), {'refused': False, 'last_rx_monotonic': 0.0, 'ip': 'x'})()
        self.mixerstate = {}
        self.initialized = False
        self.end = time.monotonic() + seconds
        self.gap = gap
        self.next = time.monotonic()

    def on(self, *a):
        return 1

    def off(self, *a):
        pass

    def loop(self):
        now = time.monotonic()
        if now >= self.next:
            self.transport.last_rx_monotonic = now
            self.next = now + self.gap
        if now >= self.end:
            self.initialized = True
            self.mixerstate['video-mode'] = object()


def test_a_slow_dump_that_keeps_arriving_is_waited_for():
    p = _Trickle(seconds=1.5, gap=0.2)
    assert 'video-mode' in wait_ready(p, timeout=10, stall_timeout=0.5, pump_interval=0.01)


def test_a_dump_that_stops_arriving_is_given_up_on():
    p = _Trickle(seconds=5, gap=2.0)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match='nothing from the switcher'):
        wait_ready(p, timeout=10, stall_timeout=0.5, pump_interval=0.01)
    assert time.monotonic() - t0 < 1.5


def test_connect_progress_says_where_it_is(clean_pool):
    conn = ATEMConnection('t')
    assert conn.connect_progress() == {'stage': 'hello', 'packets': 0}
    with NoRoomSwitcher() as sw:
        conn.connect(sw.ip)
    assert conn.connect_progress()['stage'] == 'no room'
