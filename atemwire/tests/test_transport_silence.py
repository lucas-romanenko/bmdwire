# SPDX-License-Identifier: LGPL-3.0-only
"""The silence timeout measures time since the switcher was last heard
(bmdwire#11).

The UDP thread used to time out only when ``select()`` had slept the whole
window, and ``select()`` also wakes for every packet the client sends and for
datagrams ``_receive_packet_low`` then drops. A client sending at least once
per window to a switcher that had gone silent therefore never timed out: no
``disconnected``, no re-handshake, ``is_ready`` True over stale state.

These run the real UDP thread against a "switcher" socket on 127.0.0.1 with a
short timeout. The re-handshake is observed on the wire, as the SYN the
switcher socket receives. Nothing sleeps: traffic is paced by the switcher
socket's receive timeout, and every wait ends as soon as its condition holds.
"""
import socket
import time

import pytest

from atemwire.transport import Packet, UdpProtocol

TIMEOUT = 0.3
PACE = 0.05
# Long enough for a loaded CI runner to see a 0.3 s timeout fire; on the
# behaviour before the fix the SYN never comes and each wait runs this out.
BUDGET = 3.0


@pytest.fixture
def switcher():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(('127.0.0.1', 0))
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def transport(switcher):
    """An established session whose UDP thread is running."""
    t = UdpProtocol('127.0.0.1', port=switcher.getsockname()[1], timeout=TIMEOUT)
    t.sock.bind(('127.0.0.1', 0))
    t.state = UdpProtocol.STATE_ESTABLISHED
    t.session_id = 0x8001
    t.enable_ack = True
    t.had_traffic = True
    t.thread.start()
    try:
        yield t
    finally:
        # select() on the closed socket raises; the thread logs it and exits.
        t.sock.close()
        t.thread_queue.close()
        t.thread.join(timeout=2.0)


def _ack():
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_ACK
    return pkt


def _from_switcher(seq):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.session = 0x8001
    pkt.sequence_number = seq
    pkt.data = b'\x00\x0c\x00\x00Time\x00\x00\x00\x00'
    return pkt.to_bytes()


def _syn_seen(transport, switcher, *, send_out=False, send_in=None, within=BUDGET):
    """Keep traffic flowing every PACE seconds until the switcher socket
    receives a SYN, or ``within`` runs out. ``send_out`` has the client
    send an ACK each round; ``send_in(round)`` returns a datagram for the
    switcher to send to the client that round, or None."""
    client = transport.sock.getsockname()
    deadline = time.monotonic() + within
    n = 0
    while time.monotonic() < deadline:
        n += 1
        if send_out:
            transport.send_packet(_ack())
        datagram = send_in(n) if send_in is not None else None
        if datagram is not None:
            switcher.sendto(datagram, client)
        # Read whatever the client sends until the round is over.
        round_end = min(time.monotonic() + PACE, deadline)
        while (left := round_end - time.monotonic()) > 0:
            switcher.settimeout(left)
            try:
                data, _ = switcher.recvfrom(2048)
            except socket.timeout:
                break
            if Packet.from_bytes(data).flags & UdpProtocol.FLAG_SYN:
                return True
    return False


def test_own_sends_do_not_hold_off_the_timeout(transport, switcher):
    # The switcher has gone silent; the client keeps sending.
    assert _syn_seen(transport, switcher, send_out=True), (
        'a client sending to a silent switcher must still time out and '
        're-handshake; its own packets are not the switcher being heard'
    )
    assert transport.thread_recv_queue.get(timeout=1.0) is None, (
        'the timeout must deliver the disconnect sentinel to the protocol'
    )


@pytest.mark.parametrize('datagram', [
    pytest.param(b'\xff' * 20, id='malformed'),
    pytest.param(b'\x01\x02\x03', id='short'),
])
def test_dropped_datagrams_do_not_hold_off_the_timeout(transport, switcher, datagram):
    assert _syn_seen(transport, switcher, send_in=lambda n: datagram), (
        'datagrams the transport drops are not the switcher being heard'
    )
    assert transport.thread_recv_queue.get(timeout=1.0) is None


def test_a_foreign_peer_does_not_hold_off_the_timeout(transport, switcher):
    # Valid frames, but from another address than the switcher's.
    foreign = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            foreign.bind(('127.0.0.2', 0))
        except OSError:
            pytest.skip('127.0.0.2 is not routed to loopback here')
        client = transport.sock.getsockname()

        def send_in(n):
            foreign.sendto(_from_switcher(n), client)
            return None         # and nothing from the switcher itself

        assert _syn_seen(transport, switcher, send_in=send_in)
    finally:
        foreign.close()


def test_switcher_traffic_keeps_the_session(transport, switcher):
    # Accepted datagrams every PACE seconds for over ten windows: no timeout.
    assert not _syn_seen(transport, switcher, send_in=_from_switcher,
                         within=10 * TIMEOUT + 0.5)
    assert transport.state == UdpProtocol.STATE_ESTABLISHED


def test_a_syn_gets_a_full_window(transport, switcher):
    # The first timeout re-SYNs; the next comes a full window after that
    # SYN, not at once: connect() restarts the clock.
    assert _syn_seen(transport, switcher)
    t0 = time.monotonic()
    assert _syn_seen(transport, switcher)
    assert time.monotonic() - t0 >= TIMEOUT / 2
