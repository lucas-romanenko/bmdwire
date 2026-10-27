# SPDX-License-Identifier: LGPL-3.0-only
"""The UDP thread never waits on its own send queue.

``thread_queue`` is a SocketQueue: every put writes a byte to a socketpair
so the UDP thread's select() wakes, and the UDP thread is the only reader.
A socketpair holds a few hundred one-byte writes (278 on Linux), and an
upload budget is up to 768 chunks, which ``drain_queue`` puts back to back
(bmdwire#36): the protocol thread then waits, as it should, while the UDP
thread sends. But the UDP thread queued its own packets too, the ACK for
each reliable packet the switcher sends among them. A packet arriving in
the middle of a budget had the UDP thread wait on the socketpair it alone
drains: both threads stopped for good, the upload with them, and the
caller's timeout never ran because its pump was the stuck thread. Found on
hardware (2026-10-15): a WebATEM profile restore with ATEM Software Control
and WebATEM's media pool watcher on the same switcher, whose lock and
transfer broadcasts reached the uploading session mid-budget.

Now what the UDP thread sends goes to the socket at once; every other
thread still queues.
"""
import threading
import time

import pytest

from atemwire.transport import Packet, UdpProtocol


class _CaptureSock:
    """Stand-in for the UDP socket: records sendto, returns one datagram."""

    def __init__(self):
        self.sent = []
        self._rx = None

    def sendto(self, raw, addr):
        self.sent.append(bytes(raw))
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
    p.state = UdpProtocol.STATE_ESTABLISHED
    p.enable_ack = True
    yield p
    p.thread_queue.close()


def _chunk():
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.data = b'\x05\x80FTDa' + bytes(1388)
    return pkt


def _from_switcher(seq):
    """A reliable packet from the switcher: a state broadcast, say."""
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.session = 0x8001
    pkt.sequence_number = seq
    pkt.data = b'\x00\x0cLKST\x00\x00\x00\x00'
    return pkt.to_bytes()


def _fill_like_a_budget(p, count=768):
    """Queue an upload budget from another thread, as drain_queue does on
    the protocol thread; it blocks once the socketpair is full."""
    filler = threading.Thread(
        target=lambda: [p._send_packet(_chunk()) for _ in range(count)],
        daemon=True)
    filler.start()
    deadline = time.monotonic() + 5
    while p.thread_queue.qsize() < 200 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    assert filler.is_alive(), "the budget should outgrow the socketpair"
    return filler


def _drain(p, filler):
    """Take what the filler queues until it has queued all of it. Only take
    an item the queue holds: once the filler's last item is taken, its
    thread can still be alive on its way out, and a get() then waits for a
    byte that never comes (CI hung here on Python 3.14)."""
    while filler.is_alive() or p.thread_queue.qsize():
        if p.thread_queue.qsize():
            p.thread_queue.get()
        else:
            time.sleep(0.001)
    filler.join(5)


def _as_udp_thread(p, work):
    """Run ``work`` on a thread the transport takes for its UDP thread."""
    done = threading.Event()

    def run():
        work()
        done.set()

    t = threading.Thread(target=run, daemon=True)
    p.thread = t
    t.start()
    return done


def test_an_ack_mid_budget_goes_out_at_once(proto):
    filler = _fill_like_a_budget(proto)
    proto.sock._rx = (_from_switcher(1), ('127.0.0.1', 9910))
    done = _as_udp_thread(proto, proto._receive_packet_low)
    try:
        assert done.wait(2), "the UDP thread waited on its own queue"
        acks = [Packet.from_bytes(raw) for raw in proto.sock.sent]
        assert [a.flags & UdpProtocol.FLAG_ACK for a in acks] == [UdpProtocol.FLAG_ACK]
        assert acks[0].acknowledgement_number == 1
    finally:
        _drain(proto, filler)


def test_other_threads_still_queue(proto):
    proto._send_packet(_chunk())
    assert proto.sock.sent == []
    assert proto.thread_queue.qsize() == 1


def test_a_transient_error_on_the_udp_thread_drops_the_ack(proto):
    """As the loop does for a queued ACK: logged and dropped, the thread
    stays up."""
    def refuse(raw, addr):
        raise OSError(113, 'No route to host')
    proto.sock.sendto = refuse
    proto.sock._rx = (_from_switcher(1), ('127.0.0.1', 9910))
    done = _as_udp_thread(proto, proto._receive_packet_low)
    assert done.wait(2)
