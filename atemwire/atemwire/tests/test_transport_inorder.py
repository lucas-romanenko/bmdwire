# SPDX-License-Identifier: LGPL-3.0-only
"""Transport in-order delivery gate (2026-07-02, ASC-style step 1).

The contiguous-ACK fix made the ACK stream gap-correct, but packets were still
DELIVERED to consumers in arrival order — a retransmitted packet landed
late, and the native transfer path appended its file chunk out of order,
corrupting the reassembled frame (observed live: 2 of 4 benchmark
downloads returned odd lengths from single-packet loss).

Contract under test, driving real datagrams through _receive_packet_low:
  * in-order reliable packets pass straight through (no behaviour change);
  * an ahead-of-gap packet is parked (consumer sees nothing for it yet);
  * when the gap fills, the filler AND its parked successors are pushed to
    thread_recv_queue in sequence order;
  * duplicates behind the cursor are dropped; flagged retransmit dups of
    anything already received are dropped (existing rule);
  * 15-bit wraparound (the ATEM wraps at 0x8000);
  * an absurd forward jump resyncs (delivers, drops stale parked) instead
    of deadlocking;
  * non-reliable control frames bypass the gate entirely.
"""
import socket as socket_mod
import struct

import pytest

from atemwire.transport import Packet, UdpProtocol


class _OneShotSock:
    """recvfrom returns the staged datagram once; sendto is recorded."""

    def __init__(self):
        self._rx = None
        self.sent = []

    def stage(self, raw):
        self._rx = (raw, ('127.0.0.1', 9910))

    def recvfrom(self, bufsize):
        if self._rx is None:
            raise socket_mod.timeout()
        rx, self._rx = self._rx, None
        return rx

    def sendto(self, raw, addr):
        self.sent.append(bytes(raw))
        return len(raw)

    def close(self):
        pass


@pytest.fixture
def proto():
    p = UdpProtocol('127.0.0.1')
    p.sock.close()
    p.sock = _OneShotSock()
    p.session_id = 0x8001
    yield p


def _raw(seq, flags=UdpProtocol.FLAG_RELIABLE, data=b'\x00\x0cCTst\x00\x00\x00\x00'):
    total = 12 + len(data)
    return struct.pack('>HHHHHH', (flags << 11) | total, 0x8001, 0, 0, 0, seq) + data


def feed(p, seq, flags=UdpProtocol.FLAG_RELIABLE):
    p.sock.stage(_raw(seq, flags=flags))
    return p._receive_packet_low()


def queued_seqs(p):
    out = []
    while not p.thread_recv_queue.empty():
        item = p.thread_recv_queue.get_nowait()
        out.append(item.sequence_number if isinstance(item, Packet) else item)
    return out


def test_in_order_passthrough(proto):
    for seq in (10, 11, 12):
        pkt = feed(proto, seq)
        assert isinstance(pkt, Packet) and pkt.sequence_number == seq
    assert queued_seqs(proto) == []          # nothing rerouted via the queue


def test_gap_parks_then_drains_in_order(proto):
    assert feed(proto, 10).sequence_number == 10   # establishes cursor
    assert feed(proto, 12) is True                 # parked (11 missing)
    assert feed(proto, 13) is True                 # parked behind the same gap
    assert queued_seqs(proto) == []
    assert feed(proto, 11) is True                 # filler + drain via queue
    assert queued_seqs(proto) == [11, 12, 13]      # strict sequence order
    # stream continues normally after the drain
    assert feed(proto, 14).sequence_number == 14


def test_duplicate_behind_cursor_dropped(proto):
    feed(proto, 10)
    feed(proto, 11)
    assert feed(proto, 10) is True                 # already delivered
    assert queued_seqs(proto) == []


def test_flagged_retransmit_of_received_dropped(proto):
    feed(proto, 10)
    feed(proto, 12)                                # parked
    r = feed(proto, 12, flags=UdpProtocol.FLAG_RELIABLE | UdpProtocol.FLAG_RETRANSMISSION)
    assert r is True                               # dup of a parked packet
    feed(proto, 11)
    assert queued_seqs(proto) == [11, 12]          # parked copy delivered once


def test_wraparound_gap(proto):
    # 15-bit space: the wrap is 0x7FFF -> 0x0000 (16-bit math here turned
    # every real wrap into an "absurd jump" resync mid-transfer).
    feed(proto, 0x7FFE)                            # cursor -> 0x7FFF
    assert feed(proto, 0x0001) is True             # parked across the wrap
    # 0x7FFF is in-order; with parked entries outstanding it is rerouted
    # via the queue so relative order with any drain is preserved
    assert feed(proto, 0x7FFF) is True
    assert queued_seqs(proto) == [0x7FFF]
    assert feed(proto, 0x0000) is True             # filler; drains 0x0001
    assert queued_seqs(proto) == [0x0000, 0x0001]


def test_absurd_jump_resyncs(proto):
    feed(proto, 10)
    feed(proto, 12)                                # parked
    far = (11 + UdpProtocol.DELIVERY_PARK_WINDOW + 5) & 0x7FFF
    pkt = feed(proto, far)
    assert isinstance(pkt, Packet) and pkt.sequence_number == far
    assert proto._parked == {}                     # stale parked dropped
    assert feed(proto, (far + 1) & 0x7FFF).sequence_number == (far + 1) & 0x7FFF


def test_non_reliable_bypasses_gate(proto):
    feed(proto, 10)
    feed(proto, 12)                                # parked, gap open
    # a non-reliable control frame must not be parked or reordered
    pkt = feed(proto, 0, flags=UdpProtocol.FLAG_ACK)
    assert isinstance(pkt, Packet)
    assert proto._parked                           # gap still open, untouched
