# SPDX-License-Identifier: LGPL-3.0-only
"""A state dump that ends without a video mode re-handshakes at once
(bmdwire#13).

The transport takes the first reliable packet that arrives as the session's
baseline, so if the switcher's first dump packet is lost, it is never resent
and its fields never arrive. When that packet carried ``VidM``, ``wait_ready``
waited out its whole timeout (15 s through ``ATEMConnection``) and the connect
failed; after a transport re-handshake the session ran on without a video
mode. Now ``InCm`` with no ``video-mode`` makes the protocol say goodbye and
re-handshake for a fresh dump, as it does on transport corruption, at most
``INCOMPLETE_DUMP_RETRIES`` times in a row.

These drive ``AtemProtocol.loop()`` through the real transport's
``receive_packet``, with the dumps put straight on its receive queue and the
transport's ``connect`` / ``close_session`` recorded. ``connect`` queues the
switcher's next dump, as the wire would after a handshake. ``loop()`` is
never called on an empty queue, so nothing blocks.
"""
import logging
import struct

import pytest

from atemwire.protocol import INCOMPLETE_DUMP_RETRIES, AtemProtocol
from atemwire.ready import wait_ready
from atemwire.transport import Packet, UdpProtocol


def _field(code, payload):
    return struct.pack('>H2x4s', 8 + len(payload), code) + payload


VIDM = _field(b'VidM', bytes([10, 0, 0, 0]))           # 1080p25
TOP = _field(b'_top', bytes(16))
INCM = _field(b'InCm', b'\x01\x00\x00\x00')
TIME = _field(b'Time', bytes(8))


def _packet(*fields):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.session = 0x8001
    pkt.data = b''.join(fields)
    return Packet.from_bytes(pkt.to_bytes())    # .length set as on the wire


class _Harness:
    def __init__(self, next_dumps):
        self.p = AtemProtocol('127.0.0.1')
        t = self.t = self.p.transport
        t.state = UdpProtocol.STATE_ESTABLISHED
        t.enable_ack = True
        t.had_traffic = True
        self.calls = []
        self.events = []
        self._next_dumps = list(next_dumps)
        t.close_session = lambda: self.calls.append('close_session')
        t.connect = self._connect
        for ev in ('connected', 'disconnected'):
            self.p.on(ev, lambda ev=ev: self.events.append(ev))

    def _connect(self):
        self.calls.append('connect')
        # The handshake completes and the switcher dumps again.
        self.t.state = UdpProtocol.STATE_ESTABLISHED
        self.t.enable_ack = True
        self.t.had_traffic = True
        if self._next_dumps:
            for pkt in self._next_dumps.pop(0):
                self.t.thread_recv_queue.put(pkt)

    def feed(self, *packets):
        for pkt in packets:
            self.t.thread_recv_queue.put(pkt)

    def pump(self, n):
        for _ in range(n):
            if self.t.thread_recv_queue.empty() and self.t._stashed_packet is None:
                break
            self.p.loop()

    def close(self):
        self.t.sock.close()
        self.t.thread_queue.close()


@pytest.fixture
def harness():
    made = []

    def make(next_dumps=()):
        h = _Harness(next_dumps)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def test_a_dump_without_a_video_mode_rehandshakes_at_once(harness, caplog):
    h = harness()
    h.feed(_packet(INCM))
    with caplog.at_level(logging.ERROR, logger='AtemProtocol'):
        h.p.loop()

    assert h.calls == ['close_session', 'connect'], (
        'a dump that ended without a video mode must re-handshake at once, '
        'not wait out wait_ready'
    )
    assert h.events == ['disconnected']
    assert h.p.mixerstate == {}
    assert h.t.mark_next_connected is False, 'the incomplete dump must not count as ready'
    assert 'video-mode, topology, product-name' in caplog.text


def test_the_fresh_dump_makes_the_session_ready(harness):
    h = harness(next_dumps=[[_packet(TOP, VIDM, INCM), _packet(TIME)]])
    h.feed(_packet(TOP, INCM))

    h.p.loop()
    assert h.calls == ['close_session', 'connect']
    h.pump(3)

    assert h.events == ['disconnected', 'connected']
    assert h.p.initialized is True
    assert h.p.mixerstate['video-mode'].mode == 10
    # wait_ready returns at once rather than waiting out its timeout.
    assert wait_ready(h.p, timeout=0.5) is h.p.mixerstate


def test_a_dump_that_never_has_a_video_mode_gives_up(harness, caplog):
    # A switcher whose VidM never arrives (or never parses) costs
    # INCOMPLETE_DUMP_RETRIES handshakes, then the session carries on as
    # it did before: no loop.
    h = harness(next_dumps=[[_packet(INCM)]] * INCOMPLETE_DUMP_RETRIES)
    h.feed(_packet(INCM))
    with caplog.at_level(logging.ERROR, logger='AtemProtocol'):
        h.p.loop()
        assert h.calls, 'the first incomplete dump must re-handshake'
        h.pump(INCOMPLETE_DUMP_RETRIES)

    assert h.calls.count('connect') == INCOMPLETE_DUMP_RETRIES
    assert h.t.mark_next_connected is True
    assert 'carrying on' in caplog.text
    assert 'InCm' in h.p.mixerstate


def test_a_complete_dump_is_untouched(harness):
    h = harness()
    h.feed(_packet(VIDM, INCM))
    h.p.loop()
    assert h.calls == []
    assert h.events == []
    assert h.t.mark_next_connected is True
    assert 'InCm' in h.p.mixerstate


def test_the_retry_count_starts_again_after_a_complete_dump(harness):
    # Incomplete, then complete: the next incomplete dump gets the full
    # INCOMPLETE_DUMP_RETRIES again.
    h = harness(next_dumps=[[_packet(VIDM, INCM), _packet(TIME)]])
    h.feed(_packet(INCM))
    h.p.loop()
    assert h.calls == ['close_session', 'connect']
    h.pump(3)
    assert h.events[-1] == 'connected'
    assert h.p._incomplete_dumps == 0
