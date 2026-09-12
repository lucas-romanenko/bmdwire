# SPDX-License-Identifier: LGPL-3.0-only
"""Inbound contiguous-ACK gap detection.

The ATEM dumps its initial state as a burst of reliable, sequenced packets and
retransmits anything we don't ACK. The transport must ACK only the highest
*gap-free* sequence, so a dropped packet in the middle of the burst is resent
instead of silently swallowed (which left sources blank in the UI). These tests
drive ``UdpProtocol._update_ack_number`` with scripted arrival orders.
"""
import pytest

from atemwire.transport import Packet, UdpProtocol


@pytest.fixture
def proto():
    p = UdpProtocol('127.0.0.1')
    try:
        yield p
    finally:
        p.sock.close()


def arrive(p, seq, reliable=True):
    """Simulate one packet's sequence path through _receive_packet_low."""
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE if reliable else 0
    p.remote_sequence_number = seq
    p.received_packets.append(seq)
    p._update_ack_number(pkt)
    return p.ack_number


def test_in_order_acks_each(proto):
    assert arrive(proto, 10) == 10          # first reliable packet = baseline
    assert arrive(proto, 11) == 11
    assert arrive(proto, 12) == 12


def test_gap_stalls_ack_until_filled(proto):
    assert arrive(proto, 10) == 10
    assert arrive(proto, 11) == 11
    assert arrive(proto, 13) == 11          # 12 missing -> ACK stalls at 11
    assert arrive(proto, 12) == 13          # 12 fills -> drain through 13


def test_out_of_order_burst_then_fill(proto):
    assert arrive(proto, 10) == 10
    assert arrive(proto, 13) == 10
    assert arrive(proto, 12) == 10          # 11 still missing
    assert arrive(proto, 11) == 13          # 11 fills -> 11,12,13 all drain


def test_duplicate_does_not_advance_past_gap(proto):
    assert arrive(proto, 10) == 10
    assert arrive(proto, 11) == 11
    assert arrive(proto, 11) == 11          # dup -> no spurious advance


def test_fifteen_bit_wraparound(proto):
    # The ATEM's sequence space wraps at 0x8000 (observed live 2026-07-07:
    # "cursor 32768 -> 0" resync + 20k-packet retransmission storm when
    # this math was 16-bit). 32767 -> 0 must read as contiguous.
    assert arrive(proto, 32766) == 32766
    assert arrive(proto, 32767) == 32767
    assert arrive(proto, 0) == 0            # 32767 -> 0 is contiguous
    assert arrive(proto, 1) == 1


def test_handshake_packets_mirror_latest_before_baseline(proto):
    # non-reliable control/handshake frames mirror the latest seq and must NOT
    # establish the contiguity baseline
    assert arrive(proto, 5, reliable=False) == 5
    assert proto._rx_established is False
    # the first reliable packet establishes the baseline
    assert arrive(proto, 1, reliable=True) == 1
    assert proto._rx_established is True
    assert arrive(proto, 2, reliable=True) == 2


def test_no_loss_path_is_unchanged(proto):
    # regression guarantee: with no loss the contiguous mark always equals the
    # latest received sequence, so behaviour is byte-identical to before.
    for s in range(100, 160):
        assert arrive(proto, s) == s
