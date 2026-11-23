# SPDX-License-Identifier: LGPL-3.0-only
"""A new session's reliable packets are numbered from 1 (bmdwire#13).

The ACK and the delivery cursor started at whatever reliable packet came
first. With the switcher's first packet lost, the second set them: the ACK
acknowledged the lost one too, so the switcher never sent it again, and its
resend was dropped as a duplicate. When it carried the video mode the
session waited 15 s and failed (until 1.4.0's re-handshake on a dump with
no video mode).

The first reliable packet after the handshake was seq 1 (1,394 bytes) on 10
of 10 connects to two Constellation HD units (2026-11-23), so the ACK and
the cursor start there once the handshake completes.
"""
from atemwire.tests.test_transport_stale_session import _TIME, NEW, OLD, _Session
from atemwire.transport import UdpProtocol


def test_after_the_handshake_the_ack_and_the_cursor_start_at_1():
    s = _Session()
    s.connect()
    s.handshake()
    assert (s.t.ack_number, s.t._deliver_next, s.t._rx_established) == (0, 1, True)


def test_a_lost_first_packet_is_waited_for_and_delivered_first():
    s = _Session()
    s.connect()
    s.handshake()
    s.wire.sent.clear()                         # the handshake's own ACK
    got = s.dump(NEW, [2, 3])                    # 1 lost on the way
    assert got == [True, True], 'parked behind the gap, not delivered'
    assert s.t.ack_number == 0, 'the ACK does not pass the lost packet'
    s.flush()
    assert [p.acknowledgement_number for p in s.wire.sent
            if p.flags & UdpProtocol.FLAG_ACK] == [0]
    again = s.rx(session=NEW, seq=1, data=_TIME,
                 flags=UdpProtocol.FLAG_RELIABLE | UdpProtocol.FLAG_RETRANSMISSION)
    delivered = [again] if again is not True else []
    while not s.t.thread_recv_queue.empty():
        delivered.append(s.t.thread_recv_queue.get_nowait())
    assert [p.sequence_number for p in delivered] == [1, 2, 3]
    assert s.t.ack_number == 3


def test_a_re_handshake_starts_at_1_again():
    s = _Session()
    s.connect()
    s.handshake()
    s.dump(OLD, [1, 2, 3])
    s.flush()
    s.connect()
    assert s.t._rx_established is False
    s.handshake()
    assert (s.t.ack_number, s.t._deliver_next) == (0, 1)
    got = s.dump(NEW, [1])
    assert [p.sequence_number for p in got] == [1]



def test_a_packet_1_that_never_comes_is_not_waited_for_for_ever(monkeypatch):
    # A switcher that numbered from elsewhere, or a packet 1 that never gets
    # through: after FIRST_PACKET_WAIT delivery starts from the first that
    # came, as before, and the dump-without-video-mode re-handshake follows.
    t = [100.0]
    monkeypatch.setattr('atemwire.transport.time.monotonic', lambda: t[0])
    s = _Session()
    s.connect()
    s.handshake()
    assert s.dump(NEW, [5, 6]) == [True, True]
    t[0] += UdpProtocol.FIRST_PACKET_WAIT + 0.1
    s.dump(NEW, [7])
    delivered = []
    while not s.t.thread_recv_queue.empty():
        delivered.append(s.t.thread_recv_queue.get_nowait())
    assert [p.sequence_number for p in delivered] == [5, 6, 7]
    assert s.t.ack_number == 7
    assert s.dump(NEW, [8])[0].sequence_number == 8


def test_packet_1_late_but_within_the_wait_is_still_first(monkeypatch):
    t = [100.0]
    monkeypatch.setattr('atemwire.transport.time.monotonic', lambda: t[0])
    s = _Session()
    s.connect()
    s.handshake()
    s.dump(NEW, [2, 3])
    t[0] += UdpProtocol.FIRST_PACKET_WAIT / 2
    s.dump(NEW, [4])
    assert s.t.thread_recv_queue.empty() and s.t.ack_number == 0
