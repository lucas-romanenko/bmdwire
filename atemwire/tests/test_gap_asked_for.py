# SPDX-License-Identifier: LGPL-3.0-only
"""A packet of the switcher's lost is asked for, and a request from the
switcher says what it has.

The hardware bench, 2026-11-24 (Constellation HD, fw 2.30): the cable out
for 3 s in the middle of a still upload on a clean path. Everything we had
sent was acknowledged afterwards, and the upload sat at 47 % until the
caller gave up: the switcher's next budget grant (FTCD) had been lost in
the dropout, the packets after it were parked behind the gap, and the
switcher never sent it again on its own. We now ask for it.
"""
import struct

import pytest

from atemwire.tests.test_upload_like_asc import _Sock
from atemwire.transport import Packet, UdpProtocol


@pytest.fixture
def clock(monkeypatch):
    t = [100.0]
    monkeypatch.setattr('atemwire.transport.time.monotonic', lambda: t[0])
    return t


@pytest.fixture
def udp(monkeypatch, clock):
    monkeypatch.setattr('atemwire.transport.time.sleep', lambda s: None)
    p = UdpProtocol('127.0.0.1')
    p.sock.close()
    p.sock = _Sock()
    p.session_id = 0x8001
    p.local_sequence_number = 0
    p.state = UdpProtocol.STATE_ESTABLISHED
    p._send_packet = p._send_on_udp_thread
    yield p
    p.thread_queue.close()


def rx(udp, seq):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.session = udp.session_id
    pkt.sequence_number = seq
    pkt.data = b'\x00\x0c\x00\x00Time\x00\x00\x00\x00'
    raw = pkt.to_bytes()
    udp.sock.recvfrom = lambda n: (raw, ('127.0.0.1', 9910))
    return udp._receive_packet_low()


def requests(udp):
    out = []
    for raw in udp.sock.sent:
        if (struct.unpack_from('>H', raw, 0)[0] >> 11) & UdpProtocol.FLAG_REQUEST_RETRANSMISSION:
            out.append(struct.unpack_from('>H', raw, 6)[0])
    return out


def test_a_gap_that_stays_open_is_asked_for_and_again_until_it_fills(udp, clock):
    for seq in (1, 2):
        rx(udp, seq)
    for seq in (4, 5, 6):                        # 3 lost in a dropout
        rx(udp, seq)
    assert udp._check_gap() == pytest.approx(UdpProtocol.GAP_ASK_AFTER)
    assert requests(udp) == []
    clock[0] += UdpProtocol.GAP_ASK_AFTER
    udp._check_gap()
    assert requests(udp) == [3], 'asked for the first missing one'
    clock[0] += UdpProtocol.GAP_ASK_EVERY
    udp._check_gap()
    assert requests(udp) == [3, 3], 'and again while the gap stays open'
    rx(udp, 3)                                   # it came
    clock[0] += 10
    assert udp._check_gap() is None
    assert requests(udp) == [3, 3]


def test_a_request_of_ours_is_not_one_of_our_reliable_packets(udp, clock):
    for seq in (1, 3):
        rx(udp, seq)
    clock[0] += UdpProtocol.GAP_ASK_AFTER
    udp._check_gap()
    assert udp.local_sequence_number == 0 and not udp.retransmission_buffer
    raw = [r for r in udp.sock.sent if (struct.unpack_from('>H', r, 0)[0] >> 11) & 8][0]
    assert len(raw) == 12 and struct.unpack_from('>H', raw, 2)[0] == 0x8001


def test_no_gap_nothing_asked(udp, clock):
    for seq in (1, 2, 3):
        rx(udp, seq)
    clock[0] += 5
    assert udp._check_gap() is None and requests(udp) == []


def test_the_switchers_request_says_what_it_has(udp, clock):
    # Our packets 1-40 sent; its ACKs for 11-30 lost in a dropout, then it
    # asks for 31: it has 1-30, so those count as acknowledged and only 31
    # on go again (nothing it has is resent or taken back as too big).
    def reliable(n):
        pkt = Packet()
        pkt.flags = UdpProtocol.FLAG_RELIABLE
        pkt.data = b'\x00\x0c\x00\x00TEST' + n.to_bytes(4, 'big')
        return pkt
    for n in range(40):
        udp._send_packet_low(reliable(n))
    udp._on_ack(10, ack_only=True)
    udp.sock.sent.clear()
    req = Packet()
    req.flags = UdpProtocol.FLAG_REQUEST_RETRANSMISSION
    req.session = udp.session_id
    req.retransmission_request = 31
    raw = req.to_bytes()
    udp.sock.recvfrom = lambda n: (raw, ('127.0.0.1', 9910))
    udp._receive_packet_low()
    assert udp._acked_through == 30
    resent = [Packet.from_bytes(r).sequence_number for r in udp.sock.sent
              if Packet.from_bytes(r).flags & UdpProtocol.FLAG_RELIABLE]
    assert resent == list(range(31, 41))
