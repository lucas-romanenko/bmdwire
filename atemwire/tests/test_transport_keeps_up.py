# SPDX-License-Identifier: LGPL-3.0-only
"""The UDP thread keeps up with a still download at line rate (2026-11-04).

A 4K Plus sends a still's chunks back to back, ~150 of them before it waits
for their FTUAs, and every ~80 ms resends everything it has not had ACKed.
The transport woke, read one datagram and sent one ACK per packet, scanned
a 1024-entry deque twice per packet, and dropped a resend of something it
already had without a word. Beside any other busy thread in the process it
fell behind the wire, the resends it then had to wade through put it
further behind, and the download never finished (captures
ours-4kplus-1/2.pcap: ACK p50 ~110 ms, p90 300-800 ms, 59-76% of what came
in a resend).

Now each wake reads every datagram waiting and acknowledges them with one
cumulative ACK (one per ACK_EVERY of a long run), a resend of something we
have is ACKed again, the duplicate check is O(1), and a receive buffer the
system capped is reported.
"""
import errno
import logging
import socket
import types

import pytest

from atemwire import transport
from atemwire._socketqueue import SocketQueue
from atemwire.transport import Packet, UdpProtocol, _SeenWindow

PEER = ('10.0.0.99', 9910)
SESSION = 0x8001


class _Wire:
    """The socket: recvfrom() pops scripted datagrams (BlockingIOError when
    there are none, as a non-blocking socket says), sendto() records."""

    def __init__(self):
        self.inbox = []
        self.sent = []

    def recvfrom(self, _bufsize):
        if not self.inbox:
            raise BlockingIOError(errno.EAGAIN, 'Resource temporarily unavailable')
        return self.inbox.pop(0)

    def sendto(self, raw, _addr):
        self.sent.append(Packet.from_bytes(raw))
        return len(raw)

    def close(self):
        pass


@pytest.fixture
def t():
    p = UdpProtocol(PEER[0])
    p.sock.close()
    p.sock = _Wire()
    p.thread = types.SimpleNamespace(is_alive=lambda: True)
    p._send_packet = p._send_packet_low          # what the UDP thread does
    p.state = UdpProtocol.STATE_ESTABLISHED
    p.session_id = SESSION
    try:
        yield p
    finally:
        p.thread_queue.close()


def arrive(p, seq, *, resend=False, data=b'\x00\x0cFTDa\x00\x01\x00\x00'):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE | (UdpProtocol.FLAG_RETRANSMISSION if resend else 0)
    pkt.session = SESSION
    pkt.sequence_number = seq
    pkt.data = data
    p.sock.inbox.append((pkt.to_bytes(), PEER))


def acks(p):
    return [pk.acknowledgement_number for pk in p.sock.sent
            if pk.flags & UdpProtocol.FLAG_ACK]


def test_a_burst_is_acknowledged_once_after_it_is_read(t):
    for seq in range(100, 120):
        arrive(t, seq)
    t._drain_socket()
    assert acks(t) == [119], 'one cumulative ACK for the burst'
    assert t.thread_recv_queue.qsize() == 20, 'every packet handed on'


def test_a_long_burst_is_acknowledged_as_it_goes(t):
    for seq in range(100, 100 + 3 * UdpProtocol.ACK_EVERY + 5):
        arrive(t, seq)
    t._drain_socket()
    n = UdpProtocol.ACK_EVERY
    assert acks(t) == [100 + n - 1, 100 + 2 * n - 1, 100 + 3 * n - 1, 100 + 3 * n + 4]


def test_a_resend_of_something_we_have_is_acknowledged_again(t):
    for seq in range(100, 110):
        arrive(t, seq)
    t._drain_socket()
    t.sock.sent.clear()
    for seq in range(100, 110):                  # our ACK never got there
        arrive(t, seq, resend=True)
    t._drain_socket()
    assert acks(t) == [109], 'the switcher has to hear it again'
    assert t.thread_recv_queue.qsize() == 10, 'the resends are not handed on twice'


def test_a_resend_alone_is_acknowledged_at_once_outside_a_drain(t):
    arrive(t, 100)
    t._receive_packet_low()
    arrive(t, 100, resend=True)
    assert t._receive_packet_low() is True
    assert acks(t) == [100, 100]


def test_the_ack_stays_on_the_gap_through_a_drain(t):
    for seq in (100, 101, 103, 104):             # 102 lost
        arrive(t, seq)
    t._drain_socket()
    assert acks(t) == [101]
    arrive(t, 102, resend=True)
    t._drain_socket()
    assert acks(t)[-1] == 104


def test_an_empty_socket_ends_the_drain_quietly(t):
    t._drain_socket()
    assert acks(t) == []


def test_the_seen_window_keeps_the_newest_and_answers_at_once():
    w = _SeenWindow(4)
    for seq in (1, 2, 3, 4, 5):
        w.append(seq)
    assert 1 not in w and all(s in w for s in (2, 3, 4, 5))
    w.append(2)                                  # seen again: newest now
    w.append(6)
    assert 3 not in w and 2 in w and len(w) == 4
    w.clear()
    assert len(w) == 0 and 5 not in w


def test_the_socket_queue_hands_over_everything_waiting():
    q = SocketQueue()
    try:
        assert q.get_waiting() == []
        for i in range(200):                     # the socketpair holds ~270 marks
            q.put(i)
        assert q.get_waiting() == list(range(200))
        assert q.get_waiting() == []
    finally:
        q.close()


class _CappedSock:
    """A socket whose receive buffer the system caps, as Linux does at
    net.core.rmem_max (reporting double what it grants)."""

    def __init__(self, granted):
        self.granted = granted

    def settimeout(self, _timeout):
        pass

    def setsockopt(self, level, name, value):
        pass

    def getsockopt(self, level, name):
        return self.granted * 2 if transport.sys.platform.startswith('linux') else self.granted

    def close(self):
        pass


def test_a_capped_receive_buffer_is_reported_once(monkeypatch, caplog):
    monkeypatch.setattr(transport, '_rcvbuf_warned', False)
    monkeypatch.setattr(transport.socket, 'socket', lambda *_a: _CappedSock(212992))
    with caplog.at_level(logging.WARNING, logger='UdpTransport'):
        p = UdpProtocol('10.0.0.99')
        UdpProtocol('10.0.0.98')
    assert p.rcvbuf == 212992
    warned = [r.getMessage() for r in caplog.records if 'receive buffer' in r.getMessage()]
    assert len(warned) == 1
    assert '208 KB' in warned[0] and 'net.core.rmem_max' in warned[0]


def test_a_receive_buffer_granted_in_full_is_not_reported(monkeypatch, caplog):
    monkeypatch.setattr(transport, '_rcvbuf_warned', False)
    monkeypatch.setattr(transport.socket, 'socket', lambda *_a: _CappedSock(16 * 1024 * 1024))
    with caplog.at_level(logging.WARNING, logger='UdpTransport'):
        p = UdpProtocol('10.0.0.99')
    assert p.rcvbuf == 16 * 1024 * 1024
    assert not [r for r in caplog.records if 'receive buffer' in r.getMessage()]


def test_the_udp_thread_drains_a_real_socket():
    """End to end on loopback: a burst sent to the transport's socket, read
    by the UDP thread itself, is acknowledged with the last sequence."""
    switcher = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    switcher.bind(('127.0.0.1', 0))
    switcher.settimeout(2.0)
    p = UdpProtocol('127.0.0.1', port=switcher.getsockname()[1])
    try:
        p.connect()
        syn, addr = switcher.recvfrom(2048)
        reply = Packet()
        reply.flags = UdpProtocol.FLAG_SYN
        reply.session = 0x1337
        reply.data = b'\x02' + b'\x00' * 7
        switcher.sendto(reply.to_bytes(), addr)
        assert isinstance(p.thread_recv_queue.get(timeout=2.0), Packet)
        p.state = UdpProtocol.STATE_ESTABLISHED
        p.session_id = SESSION
        for seq in range(1, 41):
            pkt = Packet()
            pkt.flags = UdpProtocol.FLAG_RELIABLE
            pkt.session = SESSION
            pkt.sequence_number = seq
            pkt.data = b'\x00\x0cFTDa\x00\x01\x00\x00'
            switcher.sendto(pkt.to_bytes(), addr)
        last = None
        while last != 40:
            got = Packet.from_bytes(switcher.recvfrom(2048)[0])
            if got.flags & UdpProtocol.FLAG_ACK:
                last = got.acknowledgement_number
        assert last == 40
    finally:
        p.close_session()
        p.sock.close()
        switcher.close()
