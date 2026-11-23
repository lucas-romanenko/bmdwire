# SPDX-License-Identifier: LGPL-3.0-only
"""Our hello goes again after 1 s without an answer, and the goodbye goes
three times.

The hello used to go only on the silence tick, every 5 s: a hello or its
answer lost on a lossy link cost 5 s, and after a pulled cable or a reboot
the session came back up to 5 s after the switcher could answer (the
network matrix's idle_cut: 10.0 s for an 8 s cut, 15.2 s once at 5 %
loss; now 8.5 s). One goodbye lost left the session on the switcher until
it reaped it (the matrix's disconnect, 1 in 3 at 5 % loss).
"""
import pytest

from atemwire.tests.test_upload_like_asc import _Sock
from atemwire.transport import Packet, UdpProtocol


@pytest.fixture
def clock(monkeypatch):
    t = [100.0]
    monkeypatch.setattr('atemwire.transport.time.monotonic', lambda: t[0])
    return t


@pytest.fixture
def udp(monkeypatch, clock):
    monkeypatch.setattr('atemwire.transport.time.sleep', lambda s: None)
    p = UdpProtocol('127.0.0.1')
    p.sock.close()
    p.sock = _Sock()
    # connect() without its thread: what it sends goes straight out.
    monkeypatch.setattr(p.thread, 'is_alive', lambda: True)
    p._send_packet = p._send_packet_low
    yield p
    p.thread_queue.close()


def hellos(udp):
    out = [Packet.from_bytes(r) for r in udp.sock.sent]
    return [q for q in out if q.flags & UdpProtocol.FLAG_SYN and bytes(q.data)[:1] == b'\x01']


def goodbyes(udp):
    out = [Packet.from_bytes(r) for r in udp.sock.sent]
    return [q for q in out if q.flags & UdpProtocol.FLAG_SYN and bytes(q.data)[:1] == b'\x04']


def test_an_unanswered_hello_goes_again_after_a_second_then_every_two(udp, clock):
    udp.connect()
    assert len(hellos(udp)) == 1
    clock[0] += UdpProtocol.HELLO_RETRY - 0.01
    assert udp._check_hello() == pytest.approx(0.01)
    assert len(hellos(udp)) == 1
    clock[0] += 0.01
    udp._check_hello()
    assert len(hellos(udp)) == 2, 'again after HELLO_RETRY'
    clock[0] += UdpProtocol.HELLO_RETRY_MAX
    udp._check_hello()
    assert len(hellos(udp)) == 3, 'then every HELLO_RETRY_MAX'
    h = hellos(udp)
    assert {q.session for q in h} == {0x1337} and all(q.sequence_number == h[0].sequence_number for q in h), \
        'the same hello, not a new handshake'
    assert udp.state == UdpProtocol.STATE_SYN_SENT


def test_the_silence_window_is_not_moved_by_a_hello_again(udp, clock):
    udp.connect()
    heard = udp._last_heard
    clock[0] += UdpProtocol.HELLO_RETRY
    udp._check_hello()
    assert udp._last_heard == heard, 'the silence tick still starts over at timeout'


def test_an_answered_hello_is_not_sent_again(udp, clock):
    udp.connect()
    udp.state = UdpProtocol.STATE_ESTABLISHED       # the switcher answered
    clock[0] += 10
    assert udp._check_hello() is None
    assert len(hellos(udp)) == 1


def test_the_goodbye_goes_three_times(udp):
    udp.session_id = 0x8001
    udp.local_sequence_number = 5
    udp.state = UdpProtocol.STATE_ESTABLISHED
    udp.close_session()
    assert len(goodbyes(udp)) == UdpProtocol.GOODBYES == 3
    assert {q.session for q in goodbyes(udp)} == {0x8001}
    udp.close_session()
    assert len(goodbyes(udp)) == 3, 'once closed, nothing more'
