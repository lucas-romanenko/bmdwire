# SPDX-License-Identifier: LGPL-3.0-only
"""A re-handshake ignores the session it is replacing (bmdwire#12).

After a silence the transport re-SYNs, but the switcher goes on resending
the old session until it reaps it (~5 s). ``_receive_packet_low`` checked
the peer and the parse, never the session, so each stale packet:
  * restarted the silence window;
  * became the new session's baseline: the first stale reliable packet set
    the ACK and the delivery cursor, and the new dump, starting again at low
    sequence numbers, was then dropped as duplicates;
  * could be adopted as the session id when it arrived just after the
    handshake ACK.

These drive ``_receive_packet_low`` with scripted datagrams and play the UDP
thread's part by hand: what the transport queues is sent with
``_send_packet_low``, what the switcher answers is fed back in.
"""
import types

import pytest

from atemwire.transport import Packet, UdpProtocol

PEER = ('10.0.0.99', 9910)
OLD, NEW = 0x8001, 0x8002


class _Wire:
    """The socket: recvfrom() pops scripted datagrams, sendto() records."""

    def __init__(self):
        self.inbox = []
        self.sent = []

    def recvfrom(self, _bufsize):
        return self.inbox.pop(0)

    def sendto(self, raw, _addr):
        self.sent.append(Packet.from_bytes(raw))
        return len(raw)

    def close(self):
        pass


class _Session:
    def __init__(self):
        self.t = UdpProtocol(PEER[0])
        self.t.sock.close()
        self.wire = self.t.sock = _Wire()
        self.t.thread = types.SimpleNamespace(is_alive=lambda: True)
        self.queued = []
        self.t._send_packet = self.queued.append

    def flush(self):
        """The UDP thread sending what was queued."""
        while self.queued:
            self.t._send_packet_low(self.queued.pop(0))

    def rx(self, *, session, seq=0, flags=UdpProtocol.FLAG_RELIABLE, data=b''):
        pkt = Packet()
        pkt.flags = flags
        pkt.session = session
        pkt.sequence_number = seq
        pkt.data = data
        self.wire.inbox.append((pkt.to_bytes(), PEER))
        return self.t._receive_packet_low()

    def connect(self):
        self.t.state = UdpProtocol.STATE_CLOSED
        self.t.connect()
        self.flush()

    def handshake(self):
        """The switcher accepts the SYN; the consumer ACKs it."""
        reply = self.rx(session=0x1337, flags=UdpProtocol.FLAG_SYN,
                        data=b'\x02' + b'\x00' * 7)
        assert reply is not None, 'the SYN reply must be accepted'
        self.t._handshake(reply)
        self.flush()
        assert self.t.state == UdpProtocol.STATE_ESTABLISHED
        assert self.t.session_id is None        # adopted from the next RX

    def dump(self, session, seqs):
        return [self.rx(session=session, seq=s, data=_TIME) for s in seqs]


_TIME = b'\x00\x0c\x00\x00Time\x00\x00\x00\x00'


@pytest.fixture
def s():
    return _Session()


def _established(s):
    s.connect()
    s.handshake()
    got = s.dump(OLD, [100, 101, 102])
    assert [p.sequence_number for p in got] == [100, 101, 102]
    assert s.t.session_id == OLD
    s.flush()                                   # the dump's ACKs
    s.wire.sent.clear()


def test_a_stale_packet_after_the_resyn_touches_nothing(s):
    _established(s)
    s.connect()
    heard, rx_at, rx_count = s.t._last_heard, s.t.last_rx_monotonic, s.t.rx_packet_count

    assert s.rx(session=OLD, seq=103, data=_TIME) is None
    assert s.t._rx_established is False
    assert s.t._deliver_next is None
    assert s.t._last_heard == heard, 'a stale packet must not hold off the silence timeout'
    assert s.t.last_rx_monotonic == rx_at
    assert s.t.rx_packet_count == rx_count
    assert 103 not in s.t.received_packets
    s.flush()
    assert not any(p.flags & UdpProtocol.FLAG_ACK for p in s.wire.sent), (
        'a stale packet must not be ACKed into the new session'
    )


def test_the_new_dump_is_delivered_after_stale_packets(s):
    _established(s)
    s.connect()
    assert s.rx(session=OLD, seq=103, data=_TIME) is None
    s.handshake()
    # One more straggler right after the handshake ACK: not adopted.
    assert s.rx(session=OLD, seq=104, data=_TIME) is None
    assert s.t.session_id is None

    got = s.dump(NEW, [1, 2, 3])
    assert [p.sequence_number for p in got] == [1, 2, 3], (
        "the new session's dump must reach the protocol in order"
    )
    assert s.t.session_id == NEW
    assert s.t.ack_number == 3
    # And the old session stays ignored once the new one is adopted.
    assert s.rx(session=OLD, seq=105, data=_TIME) is None
    assert s.t.ack_number == 3


def test_while_the_syn_is_out_only_its_reply_is_accepted(s):
    s.connect()
    assert s.t._prev_session_id is None         # first connect: nothing to drop
    assert s.rx(session=0x8005, seq=1, data=_TIME) is None
    assert s.rx(session=0x8005, flags=UdpProtocol.FLAG_SYN,
                data=b'\x02' + b'\x00' * 7) is None
    assert s.t._rx_established is False
    assert s.t.state == UdpProtocol.STATE_SYN_SENT
    s.handshake()


def test_an_unanswered_resyn_forgets_the_old_id(s):
    # The old id is remembered only from a session the switcher assigned:
    # a SYN that got no reply, or a handshake whose session never got an
    # id, leaves nothing to drop, so a switcher reusing the id costs one
    # silence window rather than every reconnect.
    _established(s)
    s.connect()
    assert s.t._prev_session_id == OLD
    s.connect()                                 # the re-SYN went unanswered
    assert s.t._prev_session_id is None
    s.handshake()
    got = s.dump(OLD, [1])
    assert [p.sequence_number for p in got] == [1]
    assert s.t.session_id == OLD
