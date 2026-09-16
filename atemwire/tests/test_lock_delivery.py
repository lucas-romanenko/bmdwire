# SPDX-License-Identifier: LGPL-3.0-only
"""The media lock, as measured on the wire (1 M/E Constellation HD, 2026-09-16).

What the switcher does:
  * a PLCK / LOCK answered during the state dump is GRANTED, and the LKOB
    arrives as the first packet after the dump;
  * a PLCK sent while another session holds the store is queued and granted
    directly on release (sometimes with no LKST release broadcast at all);
  * two sessions requesting within ~150 ms can BOTH receive LKOB; the one
    whose FTSU loses gets FTDE status 6 and must simply re-request;
  * a PLCK grant is never auto-released after one transfer — the explicit
    unlock is required, and it echoes LKST(unlocked) ~40 ms later;
  * a lock still held at the goodbye is released by the switcher within
    10 ms; an abandoned session's lock is reaped in ~5 s.

What was wrong on our side, each pinned below:
  * ``receive_packet`` threw away the packet it had just pulled whenever it
    owed the caller a sentinel — so the first packet after every state dump
    was lost, and its retransmissions were deduplicated. When that packet
    was our LKOB the session held the media lock without knowing it, for
    its whole life, refusing every other client ("locked by another
    instance", with nobody else there). Same shape for the flush sentinel
    mid-upload.
  * ``wait_ready`` returned on the FIRST packet of the dump (``connected``
    flips on any data packet), which is what put lock requests inside the
    dump in the first place.
  * FTDE 6 was treated as fatal: the task was dropped and the caller sat
    out its timeout. It is the normal outcome of two clients colliding.
  * the store lock was released only AFTER the ~250 ms Python RLE decode.
  * every queued download re-sent the lock request (three PLCKs for one
    grant).
  * the dump went unACKed until the switcher's post-dump ping, so any dump
    slower than the ~80 ms retransmit timer was resent in full, repeatedly.
"""
import struct
import threading
import types

import pytest

from atemwire.connection import ATEMConnection
from atemwire.protocol import AtemProtocol
from atemwire.ready import wait_ready
from atemwire.transport import (
    ConnectionReady, Packet, UdpProtocol, _trace_command_tags,
)
from atemwire._transfer import TransferQueueFlushed


# --------------------------------------------------------------------------
# transport: a sentinel never eats the packet it displaced
# --------------------------------------------------------------------------

def _data_packet(seq=19, payload=b'\x00\x0c\x00\x00LKOB\x00\x00\x00\x00'):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.session = 0x8001
    pkt.sequence_number = seq
    pkt.data = payload
    return Packet.from_bytes(pkt.to_bytes())      # sets .length like the wire


@pytest.fixture
def transport():
    t = UdpProtocol('127.0.0.1')
    t.state = UdpProtocol.STATE_ESTABLISHED
    t.enable_ack = True
    t.had_traffic = True
    try:
        yield t
    finally:
        t.sock.close()


def test_connection_ready_sentinel_keeps_the_packet(transport):
    pkt = _data_packet()
    transport.mark_next_connected = True
    transport.thread_recv_queue.put(pkt)

    first = transport.receive_packet()
    second = transport.receive_packet()

    assert isinstance(first, ConnectionReady)
    assert second is pkt, (
        'the first packet after the state dump must reach the protocol '
        'layer — it was the LKOB for a lock requested during the dump'
    )
    assert _trace_command_tags(second.data) == ['LKOB']


def test_flush_sentinel_keeps_the_packet(transport):
    pkt = _data_packet(payload=b'\x00\x14\x00\x00FTCD' + b'\x00' * 12)
    transport.queue_enabled = True       # send queue just drained
    transport.thread_recv_queue.put(pkt)

    first = transport.receive_packet()
    second = transport.receive_packet()

    assert isinstance(first, TransferQueueFlushed)
    assert second is pkt, 'the FTCD budget grant must not be lost to the flush sentinel'


def test_stash_is_cleared_on_connect(transport):
    transport._stashed_packet = _data_packet()
    transport.state = UdpProtocol.STATE_CLOSED
    transport.thread = threading.Thread(target=lambda: None)   # never started
    transport._send_packet = lambda pkt: None
    transport.connect()
    assert transport._stashed_packet is None


# --------------------------------------------------------------------------
# transport: the state dump is ACKed as it arrives
# --------------------------------------------------------------------------

class _StagedSock:
    def __init__(self, raw):
        self._raw = raw
    def recvfrom(self, n):
        return self._raw, ('127.0.0.1', 9910)
    def close(self):
        pass


def _receive_low(state, enable_ack):
    t = UdpProtocol('127.0.0.1')
    t.sock.close()
    t.sock = _StagedSock(_data_packet(seq=1).to_bytes())
    t.state = state
    t.enable_ack = enable_ack
    sent = []
    t._send_packet = sent.append
    t._receive_packet_low()
    return t, sent


def test_reliable_dump_packet_is_acked_once_established():
    t, sent = _receive_low(UdpProtocol.STATE_ESTABLISHED, enable_ack=False)
    assert any(p.flags & UdpProtocol.FLAG_ACK for p in sent), (
        'the first packet of the dump must be ACKed; waiting for the '
        'post-dump ping made the switcher resend the whole dump every 80 ms'
    )
    assert t.enable_ack is True


def test_no_ack_before_the_session_is_established():
    t, sent = _receive_low(UdpProtocol.STATE_SYN_SENT, enable_ack=False)
    assert not any(p.flags & UdpProtocol.FLAG_ACK for p in sent)
    assert t.enable_ack is False


# --------------------------------------------------------------------------
# ready: "ready" means the dump is complete, not that it has started
# --------------------------------------------------------------------------

class _DumpingProtocol:
    """video-mode lands on the first loop (first dump packet); the dump is
    complete (InCm → ConnectionReady) only on the third."""
    def __init__(self, finish_at=3):
        self.loops = 0
        self.connected = False
        self.initialized = False
        self.mixerstate = {}
        self._finish_at = finish_at
        self._cbs = {}
    def on(self, event, cb):
        self._cbs[event] = cb
        return 1
    def off(self, event, cb_id):
        pass
    def loop(self):
        self.loops += 1
        self.connected = True
        self.mixerstate['video-mode'] = object()
        if self.loops == self._finish_at:
            self.initialized = True
            self._cbs.get('connected', lambda: None)()


def test_wait_ready_waits_for_the_end_of_the_dump():
    p = _DumpingProtocol(finish_at=3)
    wait_ready(p, timeout=2.0, pump_interval=0)
    assert p.loops == 3, 'returned mid-dump: connected + video-mode is not "ready"'


def test_wait_ready_times_out_if_the_dump_never_completes():
    p = _DumpingProtocol(finish_at=10 ** 9)
    with pytest.raises(TimeoutError):
        wait_ready(p, timeout=0.05, pump_interval=0.001)


def test_wait_ready_fast_path_needs_initialized():
    p = _DumpingProtocol(finish_at=1)
    p.connected = True
    p.mixerstate['video-mode'] = object()      # mid-dump state, not initialized
    wait_ready(p, timeout=1.0, pump_interval=0)
    assert p.loops == 1, 'the fast path must not trust protocol.connected'


# --------------------------------------------------------------------------
# protocol: the lock dance
# --------------------------------------------------------------------------

@pytest.fixture
def proto():
    p = AtemProtocol(ip='127.0.0.1')
    p.transport.sock.close()
    p.wire = []                                  # [(tags), ...] in send order
    p.transport.send_packet = lambda pkt: p.wire.append(_trace_command_tags(pkt.data))
    p.initialized = True
    p.connected = True
    try:
        yield p
    finally:
        p.transport.thread_queue.close()


def _sent(p, code):
    return sum(1 for tags in p.wire for t in tags if t == code)


def _grant(p, store=0):
    p.save_field_data(b'LKOB', struct.pack('>Hxx', store))


def test_one_lock_request_for_several_queued_downloads(proto):
    proto.download(0, 1)
    proto.download(0, 2)
    proto.download(0, 3)
    assert _sent(proto, 'PLCK') == 1, 'three downloads queued together sent three PLCKs'
    _grant(proto)
    assert _sent(proto, 'FTSU') == 1


def test_ftde_6_releases_keeps_the_task_and_re_requests_on_release(proto):
    proto.download(0, 4)
    _grant(proto)
    tid = proto.transfer.tid
    assert _sent(proto, 'FTSU') == 1

    proto.save_field_data(b'FTDE', struct.pack('>HBx', tid, 6))

    assert proto.transfer_queue[0], 'the task must survive: 6 is a collision, not a rejection'
    assert _sent(proto, 'LOCK') == 1, 'we still hold the lock after 6 — it must be given back'
    assert proto.locks.get(0) is False and proto.transfer is None
    assert proto.transfer_requested is False
    assert _sent(proto, 'PLCK') == 1, 'no blind re-request while the other session is queued'

    # The store frees (our echo, or the other session's release): re-request.
    proto.save_field_data(b'LKST', struct.pack('>HBB', 0, 0, 0))
    assert _sent(proto, 'PLCK') == 2
    _grant(proto)
    assert _sent(proto, 'FTSU') == 2, 'the kept task runs under the new grant'


def test_ftde_6_is_not_raised_as_a_fatal_error(proto):
    fatal = []
    proto.on('file-transfer-error', fatal.append)
    proto.download(0, 4)
    _grant(proto)
    proto.save_field_data(b'FTDE', struct.pack('>HBx', proto.transfer.tid, 6))
    assert fatal == []


def test_lock_is_released_before_the_frame_is_decoded_and_delivered(proto):
    order = []
    proto.transport.send_packet = lambda pkt: order.append(('tx', _trace_command_tags(pkt.data)))
    proto.on('download-done', lambda store, slot, data: order.append(('done', slot, data)))

    proto.download(0, 7)
    _grant(proto)
    proto.transfer_buffer = [b'\x00' * 8]
    proto.transfer_buffer_bytes = 8
    proto.save_field_data(b'FTDC', struct.pack('>HBB', proto.transfer.tid, 1, 2))

    kinds = [o[0] if o[0] != 'tx' else o[1][0] for o in order]
    assert 'LOCK' in kinds and 'done' in kinds
    assert kinds.index('LOCK') < kinds.index('done'), (
        'the unlock must go out before the (slow) decode + delivery'
    )
    assert order[-1] == ('done', 7, b'\x00' * 8)
    assert proto.locks.get(0) is False and proto.transfer is None


def test_lock_request_flag_clears_on_release_broadcast(proto):
    proto.download(0, 1)
    assert 0 in proto._lock_requested
    proto.save_field_data(b'LKST', struct.pack('>HBB', 0, 0, 0))   # someone released
    assert _sent(proto, 'PLCK') == 2, 'the pounce re-requests; a queued duplicate is harmless'


def test_lane_resets_clear_the_request_flag(proto):
    proto.download(0, 1)
    proto.abort_transfers()
    assert not proto._lock_requested
    proto.download(0, 1)
    proto._reset_transfer_lane()
    assert not proto._lock_requested


def test_initialized_tracks_the_dump():
    p = AtemProtocol(ip='127.0.0.1')
    p.transport.sock.close()
    try:
        p.transport.send_packet = lambda pkt: None
        p.transport.state = UdpProtocol.STATE_ESTABLISHED
        p.transport.enable_ack = True
        p.transport.had_traffic = True
        assert p.initialized is False
        # The dump's last packet carried InCm; the next packet is real data.
        p.transport.mark_next_connected = True
        p.transport.thread_recv_queue.put(_data_packet())
        p.loop()                                              # ConnectionReady
        assert p.initialized is True and p.connected is True
        p.loop()                                              # the kept packet
        assert p.locks.get(0) is True, 'the LKOB behind the sentinel was delivered'
        p.transport.thread_recv_queue.put(None)               # session died
        p.loop()
        assert p.initialized is False
    finally:
        p.transport.thread_queue.close()


# --------------------------------------------------------------------------
# connection: a fatal rejection fails the caller now, not at the timeout
# --------------------------------------------------------------------------

def test_download_transfer_fails_fast_on_fatal_rejection():
    class _P:
        def __init__(self):
            self.cbs = {}; self.i = 0; self.aborted = 0
        def on(self, ev, cb):
            self.cbs.setdefault(ev, {})[self.i] = cb; self.i += 1; return self.i - 1
        def off(self, ev, i):
            del self.cbs[ev][i]
        def download(self, store, slot):
            for cb in list(self.cbs['file-transfer-error'].values()):
                cb('<file-transfer-error transfer=43 status=not-found>')
        def abort_transfers(self):
            self.aborted += 1

    p = _P()
    ns = types.SimpleNamespace(is_connected=True, ip_address='10.0.0.1', _protocol=p,
                               _transfer_serial_lock=threading.Lock())
    ns._download_transfer = ATEMConnection._download_transfer.__get__(ns)
    with pytest.raises(RuntimeError, match='rejected by the switcher'):
        ATEMConnection.download_still(ns, 3, timeout=5.0)
    assert p.aborted == 0, 'the protocol already dropped the task and released the lane'
    assert p.cbs['file-transfer-error'] == {}
