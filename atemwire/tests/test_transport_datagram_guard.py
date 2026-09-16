# SPDX-License-Identifier: LGPL-3.0-only
"""Foreign / malformed datagram guard on the UDP recv path.

The transport socket is unconnected, so ANY host on the local network can hit
our ephemeral port. Before the guard, a single garbage datagram raised out of
``Packet.from_bytes`` inside ``_receive_packet_low`` and killed the transport
thread — dropping the operator's live control session (pool eviction
recovered, but the session still died). These tests drive
``_receive_packet_low`` with a scripted socket and pin: foreign peers are
ignored, malformed frames are ignored, and a valid frame still parses.
"""
import pytest

from atemwire.transport import Packet, UdpProtocol


class _ScriptedSock:
    """Stands in for the UDP socket: recvfrom() pops scripted datagrams."""

    def __init__(self, datagrams):
        self._datagrams = list(datagrams)

    def recvfrom(self, _bufsize):
        return self._datagrams.pop(0)

    def close(self):
        pass


@pytest.fixture
def proto():
    p = UdpProtocol('10.0.0.99')
    real_sock = p.sock
    try:
        yield p
    finally:
        real_sock.close()


def _valid_frame():
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.session = 0x1234
    return pkt.to_bytes()


def test_foreign_peer_datagram_is_dropped(proto):
    proto.sock = _ScriptedSock([(_valid_frame(), ('10.0.0.66', 9910))])
    assert proto._receive_packet_low() is None


def test_malformed_datagram_is_dropped_not_fatal(proto):
    # < 12 bytes: struct.error territory; junk 20 bytes: length-field mismatch.
    proto.sock = _ScriptedSock([
        (b'\x01\x02\x03', ('10.0.0.99', 9910)),
        (b'\xff' * 20, ('10.0.0.99', 9910)),
    ])
    assert proto._receive_packet_low() is None
    assert proto._receive_packet_low() is None


def test_valid_datagram_from_peer_still_parses(proto):
    proto.sock = _ScriptedSock([(_valid_frame(), ('10.0.0.99', 9910))])
    pkt = proto._receive_packet_low()
    assert pkt is not None
    assert pkt.session == 0x1234
