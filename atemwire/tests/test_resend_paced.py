# SPDX-License-Identifier: LGPL-3.0-only
"""Our unacknowledged packets are resent on a timer that follows the link,
and at once when the switcher's ACKs say one went missing (2026-11-06).

The switcher takes our packets in order only and never asks for one it
missed. A lost FTUA held its send window shut until our timer resent it, a
fixed 250 ms each time, where its ACK comes back in 2 ms (p50) to 28 ms
(p99) on a LAN: at 5 % loss, a 1080p still took 96 s instead of 0.8 s.
The timer is now the smoothed round trip plus four times its variation
(RFC 6298), between RESEND_MIN and RESEND_MAX, doubled per unanswered
resend; and the same ACK arriving twice more resends at once.
"""
import pytest

from atemwire.tests.test_upload_like_asc import _Sock, send_reliable, sent_seqs  # noqa: F401
from atemwire.transport import UdpProtocol


@pytest.fixture
def clock(monkeypatch):
    t = [100.0]
    monkeypatch.setattr('atemwire.transport.time.monotonic', lambda: t[0])
    return t


@pytest.fixture
def udp(monkeypatch, clock):
    monkeypatch.setattr(UdpProtocol, 'SEND_RETRIES', 3)
    monkeypatch.setattr('atemwire.transport.time.sleep', lambda s: None)
    p = UdpProtocol('127.0.0.1')
    p.sock.close()
    p.sock = _Sock()
    p.session_id = 0x8001
    p.local_sequence_number = 0
    p.state = UdpProtocol.STATE_ESTABLISHED
    yield p
    p.thread_queue.close()


def test_before_anything_is_measured_the_timer_is_resend_after(udp):
    assert udp.resend_after() == UdpProtocol.RESEND_AFTER


def test_a_fast_link_resends_after_resend_min(udp, clock):
    for _ in range(20):
        send_reliable(udp)
        clock[0] += 0.002                        # ACKed 2 ms later
        udp._on_ack(udp.local_sequence_number)
    assert udp.resend_after() == UdpProtocol.RESEND_MIN


def test_a_slow_link_waits_longer(udp, clock):
    for _ in range(20):
        send_reliable(udp)
        clock[0] += 0.3                          # 300 ms round trips
        udp._on_ack(udp.local_sequence_number)
    assert 0.3 <= udp.resend_after() <= UdpProtocol.RESEND_MAX


def test_the_timer_follows_the_measurement(udp, clock):
    send_reliable(udp)
    clock[0] += 0.002
    udp._on_ack(1)
    send_reliable(udp)
    due = udp._resend_due
    assert due == pytest.approx(clock[0] + UdpProtocol.RESEND_MIN)


def test_a_resent_packet_is_not_timed(udp, clock):
    # Karn: its ACK may answer either copy.
    send_reliable(udp)
    clock[0] += 0.002
    udp._on_ack(1)
    send_reliable(udp)
    srtt = udp._srtt
    clock[0] += 1.0
    udp._resend_unacked()
    assert sent_seqs(udp, retransmitted=True) == [2]
    udp._on_ack(2)
    assert udp._srtt == srtt


def test_unanswered_resends_back_off_to_resend_max(udp, clock):
    send_reliable(udp)
    clock[0] += 0.002
    udp._on_ack(1)
    send_reliable(udp)
    waits = []
    for _ in range(8):
        clock[0] = udp._resend_due
        udp._resend_unacked()
        waits.append(round(udp._resend_due - clock[0], 3))
    assert waits[:3] == [0.1, 0.2, 0.4]
    assert waits[-1] == UdpProtocol.RESEND_MAX
    udp._on_ack(2)
    assert udp._backoff == 1, 'an ACK ends the backing off'


def _ack_only(udp, n):
    udp._on_ack(n, ack_only=True)


def test_the_same_ack_twice_more_resends_at_once(udp, clock):
    for _ in range(5):
        send_reliable(udp)
    _ack_only(udp, 2)                            # 3 lost; 4 and 5 arrive
    _ack_only(udp, 2)
    assert sent_seqs(udp, retransmitted=True) == []
    _ack_only(udp, 2)
    assert sent_seqs(udp, retransmitted=True) == [3, 4, 5]
    _ack_only(udp, 2)
    assert sent_seqs(udp, retransmitted=True) == [3, 4, 5], 'once per ACK'
    _ack_only(udp, 5)
    assert udp._resend_due is None


def test_a_data_packet_repeating_the_ack_is_no_sign_of_loss(udp):
    for _ in range(3):
        send_reliable(udp)
    for _ in range(4):
        udp._on_ack(1)
    assert sent_seqs(udp, retransmitted=True) == []


def test_nothing_outstanding_nothing_resent(udp):
    send_reliable(udp)
    for _ in range(4):
        _ack_only(udp, 1)
    assert sent_seqs(udp, retransmitted=True) == []


# --- our ACKs: only when they say something -------------------------------

def _rx(udp, seq, retransmitted=False):
    from atemwire.transport import Packet
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE | (UdpProtocol.FLAG_RETRANSMISSION if retransmitted else 0)
    pkt.session = udp.session_id
    pkt.sequence_number = seq
    pkt.data = b'\x00\x0c\x00\x00Time\x00\x00\x00\x00'
    raw = pkt.to_bytes()
    udp.sock.recvfrom = lambda n: (raw, ('127.0.0.1', 9910))
    return udp._receive_packet_low()


def _acks(udp):
    from atemwire.transport import Packet
    out = [Packet.from_bytes(r) for r in udp.sock.sent]
    return [q.acknowledgement_number for q in out if q.flags & UdpProtocol.FLAG_ACK]


def test_packets_parked_behind_a_gap_are_not_acknowledged_again(udp):
    udp._send_packet = udp._send_on_udp_thread
    _rx(udp, 1)
    _rx(udp, 2)
    assert _acks(udp) == [1, 2]
    for seq in (4, 5, 6):                        # 3 lost
        _rx(udp, seq)
    assert _acks(udp) == [1, 2], 'the same ACK again says nothing'
    _rx(udp, 3, retransmitted=True)              # the gap fills
    assert _acks(udp)[-1] == 6


def test_a_resend_of_what_we_have_is_acknowledged_again(udp):
    # Our ACK was lost: the switcher resends, and must hear it again.
    udp._send_packet = udp._send_on_udp_thread
    _rx(udp, 1)
    _rx(udp, 2)
    _rx(udp, 2, retransmitted=True)
    assert _acks(udp) == [1, 2, 2]


def _rx_ack_only(udp, ack=0):
    from atemwire.transport import Packet
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_ACK
    pkt.session = udp.session_id
    pkt.acknowledgement_number = ack
    pkt.sequence_number = 0                      # as the switcher sends them
    pkt.data = b''
    raw = pkt.to_bytes()
    udp.sock.recvfrom = lambda n: (raw, ('127.0.0.1', 9910))
    return udp._receive_packet_low()


def test_a_lost_packet_0_is_not_taken_for_received(udp):
    # The switcher's sequence wraps 32767 -> 0, and its ACK-only packets all
    # carry sequence 0. Its real packet 0 lost, the ACK must stop before it
    # and its resend must be delivered.
    udp._send_packet = udp._send_on_udp_thread
    delivered = []
    for seq in (32765, 32766, 32767):
        p = _rx(udp, seq)
        if p not in (None, True):
            delivered.append(p.sequence_number)
    _rx_ack_only(udp)                            # seq 0 on an ACK-only packet
    for seq in (1, 2):                           # 0 lost
        _rx(udp, seq)
    assert udp.ack_number == 32767, 'not past the missing 0'
    _rx(udp, 0, retransmitted=True)
    assert udp.ack_number == 2
    queued = []
    while not udp.thread_recv_queue.empty():
        queued.append(udp.thread_recv_queue.get().sequence_number)
    assert queued == [0, 1, 2], 'its resend is delivered, then what waited for it'
