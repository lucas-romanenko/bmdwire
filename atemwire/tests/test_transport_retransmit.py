# SPDX-License-Identifier: LGPL-3.0-only
"""Outbound retransmission handling.

When the ATEM misses one of our reliable packets it sends a control
packet with FLAG_REQUEST_RETRANSMISSION carrying the first missing
sequence in ``remote_sequence_number``; per the protocol's go-back-N
semantics everything from that sequence onward must be resent in order,
marked FLAG_RETRANSMISSION, with the ORIGINAL sequence numbers.

The pre-fix transport logged the request and dropped it — the commands
were silently lost and the ATEM was left holding a permanently gapped
inbound stream, observed live (2026-06-12) wedging the switcher's
network engine until power cycle.

These tests drive ``_send_packet_low`` (buffer maintenance) and
``_retransmit_from`` (the resend) with a capturing socket stub; one
test feeds a crafted request packet through ``_receive_packet_low`` to
pin the wiring.
"""
import pytest

from atemwire.transport import Packet, UdpProtocol


class _CaptureSock:
    """Stand-in for the UDP socket: records sendto calls."""

    def __init__(self):
        self.sent = []
        self._rx = None

    def sendto(self, raw, addr):
        self.sent.append((bytes(raw), addr))
        return len(raw)

    def recvfrom(self, bufsize):
        return self._rx

    def close(self):
        pass


@pytest.fixture
def proto():
    p = UdpProtocol('127.0.0.1')
    p.sock.close()
    p.sock = _CaptureSock()
    p.session_id = 0x8001
    p.local_sequence_number = 0
    yield p


def send_reliable(p, payload=b'\x00\x08TEST'):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.data = payload
    p._send_packet_low(pkt)
    return pkt


def send_ack(p):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_ACK
    pkt.data = b''
    p._send_packet_low(pkt)
    return pkt


def resent_packets(p):
    """Parse every captured sendto into Packets."""
    return [Packet.from_bytes(raw) for raw, _ in p.sock.sent]


# ---------------------------------------------------------------------------
# Buffer maintenance
# ---------------------------------------------------------------------------

def test_reliable_sends_are_buffered_by_sequence(proto):
    send_reliable(proto, b'\x00\x08AAAA')
    send_reliable(proto, b'\x00\x08BBBB')
    assert proto.retransmission_buffer[1].data == b'\x00\x08AAAA'
    assert proto.retransmission_buffer[2].data == b'\x00\x08BBBB'


def test_acks_do_not_clobber_the_buffer(proto):
    """Regression: ACKs don't consume a sequence number, but the old code
    buffered them at the CURRENT local sequence — overwriting the newest
    reliable packet, so a retransmit request for it would have resent an
    ACK instead of the lost command."""
    send_reliable(proto, b'\x00\x08CMD1')
    send_ack(proto)
    buffered = proto.retransmission_buffer[1]
    assert buffered.flags & UdpProtocol.FLAG_RELIABLE
    assert buffered.data == b'\x00\x08CMD1'
    assert len(proto.retransmission_buffer) == 1


def test_buffer_is_pruned_to_the_window(proto, monkeypatch):
    monkeypatch.setattr(UdpProtocol, 'RETRANSMIT_WINDOW', 8)
    for _ in range(12):
        send_reliable(proto)
    # Seqs 1..12 sent; only the last 8 (5..12) survive.
    assert sorted(proto.retransmission_buffer) == list(range(5, 13))


# ---------------------------------------------------------------------------
# _retransmit_from
# ---------------------------------------------------------------------------

def test_retransmit_resends_in_order_with_flag_and_original_seqs(proto):
    payloads = {i: bytes([0, 8]) + bytes(f'CM{i:02d}', 'ascii')
                for i in range(1, 6)}
    for i in range(1, 6):
        send_reliable(proto, payloads[i])
    proto.sock.sent.clear()

    assert proto._retransmit_from(3) == 3
    out = resent_packets(proto)
    assert [p.sequence_number for p in out] == [3, 4, 5]
    for p in out:
        assert p.flags & UdpProtocol.FLAG_RETRANSMISSION
        assert p.flags & UdpProtocol.FLAG_RELIABLE
        assert p.data == payloads[p.sequence_number]
        assert p.session == 0x8001


def test_retransmit_skips_sequences_missing_from_buffer(proto):
    for _ in range(5):
        send_reliable(proto)
    del proto.retransmission_buffer[4]
    proto.sock.sent.clear()

    assert proto._retransmit_from(3) == 2
    assert [p.sequence_number for p in resent_packets(proto)] == [3, 5]


def test_request_outside_window_resends_nothing(proto, monkeypatch):
    monkeypatch.setattr(UdpProtocol, 'RETRANSMIT_WINDOW', 8)
    for _ in range(5):
        send_reliable(proto)         # local seq now 5
    proto.sock.sent.clear()

    # span = (5 - 32765) & 0x7FFF = 8 >= window -> unserviceable
    assert proto._retransmit_from(32765) == 0
    assert proto.sock.sent == []


def test_retransmit_is_fifteen_bit_wrap_aware(proto):
    # 15-bit space: outbound sequences wrap 0x7FFF -> 0 too.
    proto.local_sequence_number = 32765
    for _ in range(4):
        send_reliable(proto)         # seqs 32766, 32767, 0, 1
    proto.sock.sent.clear()

    assert proto._retransmit_from(32767) == 3
    assert [p.sequence_number for p in resent_packets(proto)] == [32767, 0, 1]


def test_second_request_for_same_range_resends_again(proto):
    send_reliable(proto)
    proto.sock.sent.clear()
    assert proto._retransmit_from(1) == 1
    assert proto._retransmit_from(1) == 1
    assert len(proto.sock.sent) == 2


def test_volley_is_capped_per_request(proto, monkeypatch):
    """A single request can never flood the switcher: at most
    RETRANSMIT_MAX_BURST packets go out; the ATEM re-requests from
    wherever it is still missing, which paces the rest."""
    monkeypatch.setattr(UdpProtocol, 'RETRANSMIT_MAX_BURST', 4)
    for _ in range(10):
        send_reliable(proto)
    proto.sock.sent.clear()

    assert proto._retransmit_from(1) == 4
    assert [p.sequence_number for p in resent_packets(proto)] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Receive-path wiring — the request seq lives in header bytes 6-7
# ---------------------------------------------------------------------------

def make_request_raw(request_seq, session=0x8001):
    """A 12-byte FLAG_REQUEST_RETRANSMISSION control packet as the ATEM
    sends it. The requested sequence is at header bytes 6-7 — bytes
    STRUCT_HEADER skips ('2x'), so it must be spliced in by hand
    (``to_bytes`` zeroes them; we never send requests ourselves)."""
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_REQUEST_RETRANSMISSION
    pkt.session = session
    pkt.data = b''
    raw = bytearray(pkt.to_bytes())
    raw[6:8] = request_seq.to_bytes(2, 'big')
    return bytes(raw)


def test_from_bytes_parses_request_seq_from_bytes_6_7():
    pkt = Packet.from_bytes(make_request_raw(0x1234))
    assert pkt.retransmission_request == 0x1234
    # Regression for the live wedge (2026-06-12): bytes 8-9
    # (remote_sequence_number) are NOT the request field — reading the
    # request from there replayed whole sessions from seq ~0.
    assert pkt.remote_sequence_number != pkt.retransmission_request


def test_receive_path_services_a_retransmission_request(proto):
    for _ in range(3):
        send_reliable(proto)
    proto.sock.sent.clear()

    proto.sock._rx = (make_request_raw(2), ('127.0.0.1', 9910))
    proto._receive_packet_low()

    out = [p for p in resent_packets(proto)
           if p.flags & UdpProtocol.FLAG_RETRANSMISSION]
    assert [p.sequence_number for p in out] == [2, 3]


def test_receive_path_ignores_stale_bytes_8_9_field(proto):
    """A request whose bytes 8-9 happen to hold a low value (e.g. the
    0x61 ACK magic) must NOT trigger a replay from that value — only
    bytes 6-7 select the resend start."""
    for _ in range(5):
        send_reliable(proto)
    proto.sock.sent.clear()

    raw = bytearray(make_request_raw(4))
    raw[8:10] = (0).to_bytes(2, 'big')   # poison the wrong field
    proto.sock._rx = (bytes(raw), ('127.0.0.1', 9910))
    proto._receive_packet_low()

    out = [p for p in resent_packets(proto)
           if p.flags & UdpProtocol.FLAG_RETRANSMISSION]
    assert [p.sequence_number for p in out] == [4, 5]
