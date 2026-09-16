"""A store lock we hold must be given back before the session ends.

The failure this pins is not theoretical. A lock still held at teardown is
kept by the switcher for the rest of that session's life, and for an
abandoned session that is the ATEM's own ~5 minute timeout. Every client
asking for the media store in that window is refused: a switcher that will
not load a still, and an ATEM Software Control media pool slot spinning
forever on a thumbnail it cannot download.

The subtle part, and the reason abort_transfers is not enough on a close
path: a normal command is QUEUED for the worker thread, and on teardown that
worker is already exiting, so the packet is never sent. The release has to go
straight to the socket the way the goodbye does. These tests assert the
datagram reaches the wire, not merely that a method was called.
"""
import pytest

from atemwire.protocol import AtemProtocol


class _FakeTransport:
    """Records what is written straight to the socket vs queued for a worker."""

    def __init__(self):
        self.sent_low = []      # reached the wire
        self.queued = []        # would need a live worker to drain
        self.closed = False

    def _send_packet_low(self, packet):
        self.sent_low.append(packet)

    def send_packet(self, packet):
        self.queued.append(packet)

    def close_session(self):
        self.closed = True


def _protocol_holding(*stores):
    p = AtemProtocol.__new__(AtemProtocol)
    import logging
    p.log = logging.getLogger('test')
    p.transport = _FakeTransport()
    p.locks = {s: True for s in stores}
    p._lock_release_pending = set()
    return p


def test_a_held_lock_is_released_straight_to_the_socket():
    p = _protocol_holding(0)
    p.release_locks_now()

    assert len(p.transport.sent_low) == 1, (
        'the release must go straight to the socket; queued, it dies with the '
        'worker and the switcher keeps the lock'
    )
    assert not p.transport.queued, 'must not rely on a worker that is exiting'
    assert p.locks[0] is False


def test_nothing_is_sent_when_no_lock_is_held():
    p = _protocol_holding()
    p.locks = {0: False}
    p.release_locks_now()
    assert not p.transport.sent_low and not p.transport.queued


def test_the_macro_store_is_skipped():
    """0xffff is lock-exempt on the wire; asking to release it is meaningless."""
    p = _protocol_holding(0xffff)
    p.release_locks_now()
    assert not p.transport.sent_low


def test_every_held_store_is_released():
    p = _protocol_holding(0, 1)
    p.release_locks_now()
    assert len(p.transport.sent_low) == 2
    assert all(v is False for v in p.locks.values())


def test_it_never_raises_on_a_dead_socket():
    """Teardown is not a place to raise: we are going away regardless."""
    p = _protocol_holding(0)

    def _boom(packet):
        raise OSError(9, 'Bad file descriptor')
    p.transport._send_packet_low = _boom

    p.release_locks_now()          # must not raise
    assert p.locks[0] is False
