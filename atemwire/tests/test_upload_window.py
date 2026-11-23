# SPDX-License-Identifier: LGPL-3.0-only
"""Our reliable packets go out as a congestion window lets them, and a lost
one is sent again with everything after it as the ACKs come back
(WebATEM #63: uploads that never finished over an 80 ms VPN).

An upload budget (FTCD, 768 chunks of a 1080p still) went out whole, back
to back. The switcher takes our packets in order only and drops the rest,
so on a slower or longer link one lost chunk cost every chunk after it;
the resend sent RETRANSMIT_MAX_BURST of them and waited out the timer for
each next lot. Measured offline against a fake switcher behind 40 ms each
way: 10 Mbit/s with a 70-packet queue, 34 s a still (6.6 s of data); 2 %
loss, 14 s and twice no end within 90 s.

The switcher here is a model of the captures (docs/MEDIA_LOCK.md): it takes
our packets in order only, drops any that come early, and acknowledges
cumulatively every 16 packets and at the end of what arrived.
"""
import pytest

from atemwire.tests.test_upload_like_asc import _Sock
from atemwire.transport import Packet, UdpProtocol

RTT = 0.08


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
    # A session under way: something sent and acknowledged, one round trip
    # measured.
    p._send_packet_low(chunk(0))
    clock[0] += RTT
    p._on_ack(1, ack_only=True)
    p.sock.sent.clear()
    yield p
    p.thread_queue.close()


def chunk(i):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    # An FTDa as _queue_chunks sends it: transfer 7, 1,388 bytes of data.
    pkt.data = (1400).to_bytes(2, 'big') + b'\x00\x00FTDa' + (7).to_bytes(2, 'big') \
        + (1388).to_bytes(2, 'big') + i.to_bytes(4, 'big') + bytes(1384)
    return pkt


def queue(p, n, start=1):
    """An upload budget, as drain_queue hands it to the UDP thread."""
    for i in range(start, start + n):
        p._send_on_udp_thread(chunk(i))


class Switcher:
    """In order only; an early packet is dropped; cumulative ACKs."""

    def __init__(self, p, lose=()):
        self.p = p
        self.have = p._acked_through
        self.lose = set(lose)
        self.read = 0
        self.dropped = 0

    def deliver(self):
        """What we sent since the last call arrives; the ACKs it sends."""
        acks, n = [], 0
        for raw in self.p.sock.sent[self.read:]:
            seq = Packet.from_bytes(raw).sequence_number
            if seq in self.lose:
                self.lose.discard(seq)           # lost once, on the way
                continue
            if seq == (self.have + 1) & 0x7FFF:
                self.have = seq
            elif ((seq - self.have) & 0x7FFF) < 0x4000:
                self.dropped += 1
            n += 1
            if n % 16 == 0:
                acks.append(self.have)
        self.read = len(self.p.sock.sent)
        if n % 16:
            acks.append(self.have)
        return acks


def run(p, sw, clock, last, rtt=RTT, max_rounds=2000):
    """Round trips until the switcher has everything through ``last``;
    the time it took."""
    start = clock[0]
    for _ in range(max_rounds):
        if sw.have == last:
            return clock[0] - start
        acks = sw.deliver()
        clock[0] += rtt
        for a in acks:
            p._on_ack(a, ack_only=True)
        if not acks and p._resend_due is not None:
            clock[0] = max(clock[0], p._resend_due)
            p._resend_unacked()
    raise AssertionError(f'switcher stuck at {sw.have} of {last}')


def test_the_first_budget_goes_whole_and_the_next_as_the_window_lets_it(udp):
    # CWND_START is one budget: on a good link the first goes back to back
    # as ATEM Software Control sends it (a smaller start cost 200 ms links
    # 0.4 s a still for nothing).
    queue(udp, 2 * 768)
    assert len(udp.sock.sent) == UdpProtocol.CWND_START == 768


def test_the_window_opens_as_the_acks_come(udp, clock):
    udp._cwnd = 128.0                            # after a loss, say
    queue(udp, 768)
    sw = Switcher(udp)
    took = run(udp, sw, clock, last=769)
    assert took <= 4 * RTT                       # 128, 256, 512: then the rest
    assert sw.dropped == 0
    seqs = [Packet.from_bytes(r).sequence_number for r in udp.sock.sent]
    assert seqs == list(range(2, 770)), 'each once, in order'


def test_a_lost_chunk_costs_round_trips_not_timers(udp, clock):
    queue(udp, 768)
    sw = Switcher(udp, lose={60})
    took = run(udp, sw, clock, last=769)
    assert udp.resends == 0, 'the repeated ACK said so: no timer'
    assert took <= 6 * RTT                       # 1.14.5: 15 round trips, 5 timers


def test_chunks_lost_at_a_full_queue_are_sent_again_in_one_go(udp, clock):
    # A queue on the way that takes 100 packets of a burst and drops the
    # rest. The whole lot after the first one lost goes again as the window
    # allows, instead of 128 at a time with a timer in between.
    queue(udp, 768)
    sw = Switcher(udp, lose=set(range(102, 770)))
    took = run(udp, sw, clock, last=769)
    assert took <= 11 * RTT                      # 1.14.5: 14.6, 6 timers


def test_loss_without_a_queue_keeps_the_window(udp, clock):
    # Random loss, the round trip as short as ever: halving the window for
    # it only slowed the upload (2 % loss over 80 ms: 45 s a still).
    queue(udp, 300)
    clock[0] += RTT
    udp._on_ack(10, ack_only=True)
    cwnd = udp._cwnd
    udp._on_ack(10, ack_only=True)
    udp._on_ack(10, ack_only=True)               # the second repeat
    assert udp._cwnd >= cwnd


def test_loss_with_a_queue_halves_the_window(udp, clock):
    udp._cwnd = 128.0
    queue(udp, 300)
    clock[0] += RTT + 0.05                       # 50 ms waiting in a queue
    udp._srtt = RTT + 0.05
    udp._on_ack(10, ack_only=True)
    flight = udp._in_flight()
    udp._on_ack(10, ack_only=True)
    udp._on_ack(10, ack_only=True)
    assert udp._cwnd == pytest.approx(max(UdpProtocol.CWND_MIN, flight * UdpProtocol.CWND_BETA))


def test_a_timeout_with_a_queue_starts_again_small(udp, clock):
    udp._srtt = RTT + 0.05
    queue(udp, 300)
    clock[0] = udp._resend_due
    udp._resend_unacked()
    assert udp._cwnd == UdpProtocol.CWND_MIN
    resent = [Packet.from_bytes(r) for r in udp.sock.sent
              if Packet.from_bytes(r).flags & UdpProtocol.FLAG_RETRANSMISSION]
    assert len(resent) == UdpProtocol.CWND_MIN


def test_a_steady_round_trip_leaves_the_timer_room(udp, clock):
    # srtt + 4 rttvar alone came to 80.0 ms on a steady 80 ms link: every
    # ACK a little late was a resend nobody needed.
    for _ in range(30):
        udp._send_packet_low(chunk(0))
        clock[0] += RTT
        udp._on_ack(udp.local_sequence_number, ack_only=True)
    assert udp.resend_after() >= RTT + 0.02


def test_a_command_waits_its_turn_behind_the_chunks(udp, clock):
    queue(udp, 300)
    cmd = Packet()
    cmd.flags = UdpProtocol.FLAG_RELIABLE
    cmd.data = b'\x00\x0c\x00\x00LOCK\x00\x00\x00\x00'
    udp._send_on_udp_thread(cmd)
    sw = Switcher(udp)
    run(udp, sw, clock, last=302)
    last = Packet.from_bytes(udp.sock.sent[-1])
    assert last.sequence_number == 302 and bytes(last.data)[4:8] == b'LOCK'


def test_acks_and_hellos_never_wait(udp):
    queue(udp, 300)
    ack = Packet()
    ack.flags = UdpProtocol.FLAG_ACK
    ack.acknowledgement_number = 5
    before = len(udp.sock.sent)
    udp._send_on_udp_thread(ack)
    assert len(udp.sock.sent) == before + 1


def test_a_new_session_forgets_what_was_waiting(udp):
    queue(udp, UdpProtocol.CWND_START + 300)
    assert udp._held
    udp.state = UdpProtocol.STATE_CLOSED
    udp.sock = _Sock()
    udp.connect()
    assert not udp._held
    assert udp._cwnd == UdpProtocol.CWND_START


def ftua(n=0):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.data = b'\x00\x0c\x00\x00FTUA' + n.to_bytes(2, 'big') + bytes(2)
    return pkt


def test_only_upload_chunks_wait_for_the_window(udp, clock):
    # A download's acknowledgements (FTUA) held behind a loss held the
    # switcher's own send window shut: a slow site's still download, 46 s
    # on 1.14.7, took 54 s. Only an upload's chunks burst; only they wait.
    udp._cwnd = 16.0
    queue(udp, 16)                               # the window full, none waiting
    before = len(udp.sock.sent)
    udp._send_on_udp_thread(ftua())
    assert len(udp.sock.sent) == before + 1, 'goes at once'
    udp._send_on_udp_thread(chunk(99))
    assert udp._held, 'a chunk waits its turn'


def test_an_acknowledgement_goes_at_once_while_lost_chunks_go_again(udp, clock):
    udp._cwnd = 32.0
    queue(udp, 32)
    clock[0] += RTT
    udp._on_ack(5, ack_only=True)
    udp._on_ack(5, ack_only=True)
    udp._on_ack(5, ack_only=True)                # a loss: go-back-N under way
    assert udp._sent_through != udp.local_sequence_number or udp._held is not None
    before = len(udp.sock.sent)
    udp._send_on_udp_thread(ftua(1))
    sent = [Packet.from_bytes(r) for r in udp.sock.sent[before:]]
    assert any(bytes(q.data)[4:8] == b'FTUA' for q in sent)


def test_a_command_waits_behind_chunks_that_wait(udp, clock):
    # In order behind what is waiting: an FTFD must never overtake the
    # data it describes.
    queue(udp, UdpProtocol.CWND_START + 10)
    before = len(udp.sock.sent)
    udp._send_on_udp_thread(ftua(2))
    assert len(udp.sock.sent) == before and udp._held[-1].data[4:8] == b'FTUA'
