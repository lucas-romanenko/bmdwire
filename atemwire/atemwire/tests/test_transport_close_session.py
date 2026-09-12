# SPDX-License-Identifier: LGPL-3.0-only
"""Unit tests for the transport's clean session close + hang hardening
(added 2026-07-02 after live debugging).

Contract under test:
  * close_session() on an ESTABLISHED session sends exactly one SYN
    packet whose hello payload opcode is 0x04 ("closing"), stamped with
    the live session id, and flips the transport to STATE_CLOSED.
    Abandoning sessions without this goodbye left ~5-minute zombies on
    the switcher; enough of them refused all new sessions / never
    granted the still-store lock ("wedged network engine", power-cycle
    to clear).
  * close_session() on a non-established transport sends nothing (no
    goodbye during handshake or after close; idempotent).
  * _send_packet_low never lets an unserializable packet kill the UDP
    thread (struct.error hung upload jobs forever) — it drops + logs.
  * a dying _udp_thread always leaves a None sentinel on
    thread_recv_queue so blocked consumers surface a disconnect instead
    of hanging forever.
"""
import pytest

from atemwire.transport import Packet, UdpProtocol


class _CaptureSock:
    """Stand-in for the UDP socket: records sendto calls."""

    def __init__(self):
        self.sent = []

    def sendto(self, raw, addr):
        self.sent.append((bytes(raw), addr))
        return len(raw)

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


def test_close_session_sends_goodbye_and_closes_state(proto):
    proto.state = UdpProtocol.STATE_ESTABLISHED

    proto.close_session()

    assert len(proto.sock.sent) == 1
    pkt = Packet.from_bytes(proto.sock.sent[0][0])
    assert pkt.flags & UdpProtocol.FLAG_SYN
    assert pkt.data[0] == 0x04                      # hello opcode: closing
    assert pkt.session == 0x8001                    # the live session, not a fresh one
    assert proto.state == UdpProtocol.STATE_CLOSED


def test_close_session_noop_unless_established(proto):
    for state in (UdpProtocol.STATE_CLOSED, UdpProtocol.STATE_SYN_SENT):
        proto.state = state
        proto.close_session()
        assert proto.sock.sent == []

    # Idempotent: second call after a real close sends nothing more.
    proto.state = UdpProtocol.STATE_ESTABLISHED
    proto.close_session()
    proto.close_session()
    assert len(proto.sock.sent) == 1


def test_send_packet_low_drops_unserializable_packet(proto):
    """A post-reset None field must drop the one packet, not raise out of
    the UDP thread (which stranded consumers on a dead queue)."""
    proto.session_id = None                          # poisons Packet.to_bytes

    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.data = b'\x00\x08TEST'
    proto._send_packet_low(pkt)                      # must not raise

    assert proto.sock.sent == []


def test_udp_thread_death_leaves_disconnect_sentinel(proto, monkeypatch):
    """Whatever kills the UDP thread, consumers blocked on
    thread_recv_queue.get() must be released with the disconnect
    sentinel instead of hanging forever."""
    def _boom():
        raise RuntimeError('simulated thread death')

    monkeypatch.setattr(proto, '_udp_thread_loop', _boom)

    proto._udp_thread()                              # must not raise

    assert proto.thread_recv_queue.get_nowait() is None
