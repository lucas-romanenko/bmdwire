# SPDX-License-Identifier: LGPL-3.0-only
"""Uploads as ATEM Software Control sends them (its restore capture,
2026-10-15; docs/MEDIA_LOCK.md).

ASC sends FTFD with the first FTCD budget, before the data, and each
budget whole, back to back. Twenty 1080p stills took it about 12 s.
atemwire sent FTFD after the last chunk and five chunks per packet from
the switcher, so a still took 23 s.

Between two transfers ASC sends the release and the next lock request in
one packet. 1.5.0 did the same, and shut ASC out: another client's
request waiting for the store is handed it at our release, unannounced,
and a request of ours soon after is granted beside it and aborts its
transfer (FTDE 6). So the next request waits for the store to be
announced free and LOCK_COURTESY more; not announced, someone has it, and
we wait while anyone is seen at work, asking after LOCK_QUIET of quiet.

The lock request atemwire deferred until 1.5.0 was never eaten by the
switcher: a release broadcast arriving after the LKOB for a new request
was read as losing the new grant.

The transport half: a send the kernel refuses is retried and, failing
that, keeps its sequence number so it is resent; and what the switcher
has not acknowledged is resent after RESEND_AFTER, since nothing follows
the last chunk of a budget to show the switcher it is missing.
"""
import errno
import struct

import pytest

from atemwire.protocol import LOCK_COURTESY, LOCK_QUIET, AtemProtocol
from atemwire.transport import BaseProtocol, Packet, UdpProtocol

CHUNK = 1392                     # the switcher's 1396, in whole 8-byte words
# Ten chunks of data with no run of 0xFE words (an RLE header) to cut short.
DATA = bytes((i * 7) % 251 for i in range(CHUNK * 10))


@pytest.fixture
def proto():
    p = AtemProtocol(ip='127.0.0.1')
    p.transport.sock.close()
    p.packets = []                               # each send_commands: [command, ...]
    p.send_commands = lambda commands: p.packets.append(list(commands))
    p.chunks = []                                # FTDa packets, as drained
    p.transport._send_packet = p.chunks.append
    p.initialized = True
    p.connected = True
    try:
        yield p
    finally:
        p.transport.thread_queue.close()


def names(packet):
    return [type(c).__name__ for c in packet]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr('atemwire.protocol.time.monotonic', c)
    return c


def grant(p, store=0):
    p.save_field_data(b'LKOB', struct.pack('>Hxx', store))


def budget(p, count, size=1396):
    p.save_field_data(b'FTCD', struct.pack('>H4xHH2x', p.transfer.tid, size, count))


def done(p, tid):
    p.save_field_data(b'FTDC', struct.pack('>HBB', tid, 1, 2))


def lkst(p, store, state):
    p.save_field_data(b'LKST', struct.pack('>H?B', store, state, 0))


def metadata_sent(p):
    return sum(names(packet).count('TransferFileDataCommand') for packet in p.packets)


def chunk_payloads(p):
    # FTDa: 8-byte command header, transfer id, size, then the chunk.
    return [bytes(pkt.data[12:]) for pkt in p.chunks]


def test_an_upload_goes_as_atem_software_control_sends_it(proto):
    proto.upload(0, 4, DATA, compress=False, name='card')
    assert [names(pk) for pk in proto.packets] == [['PartialLockCommand']]
    grant(proto)
    assert names(proto.packets[-1]) == ['TransferUploadRequestCommand']
    tid = proto.transfer.tid

    budget(proto, 4)
    assert names(proto.packets[-1]) == ['TransferFileDataCommand'], 'FTFD before the data'
    assert len(proto.chunks) == 4, 'the whole budget at once'
    budget(proto, 4)
    budget(proto, 4)
    assert len(proto.chunks) == 10
    assert b''.join(chunk_payloads(proto)) == DATA
    proto._queue_flushed()                       # the drain, reported
    assert metadata_sent(proto) == 1

    done(proto, tid)
    assert names(proto.packets[-1]) == ['LockCommand']
    assert proto.packets[-1][0].state is False, 'nothing queued: the release alone'


def _first_of_two_done(p, make):
    make(p, 1)
    make(p, 2)
    grant(p)
    tid = p.transfer.tid
    if p.transfer.upload:
        budget(p, 10)
    done(p, tid)


def _upload(p, slot):
    p.upload(0, slot, DATA, compress=False)


def _download(p, slot):
    p.download(0, slot)


@pytest.mark.parametrize('make', [_upload, _download], ids=['uploads', 'downloads'])
def test_the_store_is_left_free_until_the_release_is_seen(proto, clock, make):
    _first_of_two_done(proto, make)
    assert names(proto.packets[-1]) == ['LockCommand'], 'the release alone'
    assert proto.packets[-1][0].state is False
    asked = len(proto.packets)
    lkst(proto, 0, False)                        # the release announced
    clock.t += LOCK_COURTESY / 2
    proto._service_yields()
    assert len(proto.packets) == asked, 'asked before anyone could take it'
    clock.t += LOCK_COURTESY
    proto._service_yields()
    assert names(proto.packets[-1]) == ['PartialLockCommand']
    assert proto.packets[-1][0].slot == 2
    grant(proto)
    assert proto.transfer.slot == 2 and proto.transfer_requested is True


def test_a_transfer_queued_after_the_release_waits_too(proto, clock):
    # WebATEM's media pool watcher queues one still at a time, the next
    # after decoding the last: nothing is queued at the release, and the
    # request that follows a moment later must wait its turn as well.
    _download(proto, 1)
    grant(proto)
    done(proto, proto.transfer.tid)
    assert names(proto.packets[-1]) == ['LockCommand']
    clock.t += 0.25                              # the decode
    _download(proto, 2)
    assert names(proto.packets[-1]) == ['LockCommand'], 'asked before the store was seen free'
    lkst(proto, 0, False)
    clock.t += LOCK_COURTESY
    proto._service_yields()
    assert names(proto.packets[-1]) == ['PartialLockCommand']


def test_after_ftde_6_the_store_is_theirs_until_seen_free(proto, clock):
    _upload(proto, 1)
    grant(proto)
    proto.save_field_data(b'FTDE', struct.pack('>HBx', proto.transfer.tid, 6))
    asked = len(proto.packets)
    clock.t += LOCK_COURTESY * 3
    proto._service_yields()
    assert len(proto.packets) == asked


def test_without_an_announcement_we_ask_after_lock_quiet(proto, clock):
    _first_of_two_done(proto, _upload)
    asked = len(proto.packets)
    clock.t += LOCK_QUIET * 0.9
    proto._service_yields()
    assert len(proto.packets) == asked
    clock.t += LOCK_QUIET * 0.2
    proto._service_yields()
    assert names(proto.packets[-1]) == ['PartialLockCommand']


def test_a_store_handed_to_another_client_waits_while_it_works(proto, clock):
    # Not announced free after our release: someone waiting was handed it.
    # Their transfers ending (FTDC, another session's id) keep us waiting.
    _first_of_two_done(proto, _upload)
    asked = len(proto.packets)
    for _ in range(3):
        clock.t += LOCK_QUIET * 0.6
        proto.save_field_data(b'FTDC', struct.pack('>HBB', 0x6c02, 1, 2))
        proto._service_yields()
    assert len(proto.packets) == asked, 'asked while another client worked'
    lkst(proto, 0, False)                        # done: announced free
    clock.t += LOCK_COURTESY
    proto._service_yields()
    assert names(proto.packets[-1]) == ['PartialLockCommand']


def test_another_clients_turn_lasts_until_they_release(proto, clock):
    # ASC's request waited for the store and was granted at our release.
    # A request of ours now would be granted too and abort ASC's transfer
    # with FTDE 6 (the capture of 2026-10-15), so we ask once ASC lets go.
    _first_of_two_done(proto, _upload)
    asked = len(proto.packets)
    lkst(proto, 0, True)                         # taken, not by us
    clock.t += LOCK_QUIET * 2
    proto._service_yields()
    assert len(proto.packets) == asked, 'asked while another client held it'
    lkst(proto, 0, False)                        # they let go
    clock.t += LOCK_COURTESY
    proto._service_yields()
    assert names(proto.packets[-1]) == ['PartialLockCommand']


def test_a_store_another_client_holds_is_not_asked_for(proto, clock):
    lkst(proto, 0, True)                         # someone else's
    proto.download(0, 3)
    assert proto.packets == []
    lkst(proto, 0, False)
    assert proto.packets == [], 'a frame for them to take it again'
    clock.t += LOCK_COURTESY
    proto._service_yields()
    assert names(proto.packets[-1]) == ['PartialLockCommand']


def test_another_client_releasing_and_retaking_keeps_its_turn(proto, clock):
    # ASC releases one still and takes the store for its next in one
    # packet; the switcher announces a release and a lock together. A
    # request sent on the release would abort ASC's next transfer.
    lkst(proto, 0, True)
    proto.download(0, 3)
    lkst(proto, 0, False)
    lkst(proto, 0, True)
    clock.t += LOCK_COURTESY * 2
    proto._service_yields()
    assert proto.packets == []


def test_someone_taking_the_store_in_the_courtesy_gets_their_turn(proto, clock):
    _first_of_two_done(proto, _upload)
    lkst(proto, 0, False)                        # our release, announced
    lkst(proto, 0, True)                         # and taken a frame later
    clock.t += LOCK_COURTESY * 2
    proto._service_yields()
    assert names(proto.packets[-1]) == ['LockCommand'], 'no request in their turn'


def test_the_yield_wakes_the_protocol(proto, clock):
    woken = []
    proto.transport.wake_at = woken.append
    _first_of_two_done(proto, _upload)
    assert woken == [clock.t + LOCK_QUIET]
    lkst(proto, 0, False)
    assert woken[-1] == clock.t + LOCK_COURTESY


def test_a_pumps_wakeups_in_the_yield_ask_nothing(proto, clock, monkeypatch):
    # WebATEM's pump puts a Wakeup on every tick: 50 a second, each one a
    # trigger and two INFO lines through a 37 s turn (2026-10-15).
    woken = []
    proto.transport.wake_at = woken.append
    _first_of_two_done(proto, _upload)
    asked = len(proto.packets)
    triggers = []
    real = proto._transfer_trigger
    monkeypatch.setattr(proto, '_transfer_trigger',
                        lambda *a, **k: (triggers.append(a), real(*a, **k)))
    for _ in range(50):
        clock.t += 0.02
        proto._service_yields()
    assert triggers == [] and len(proto.packets) == asked
    clock.t += LOCK_QUIET
    proto._service_yields()
    assert len(triggers) == 1
    assert names(proto.packets[-1]) == ['PartialLockCommand']


def test_a_yield_moved_by_someone_at_work_is_woken_for_again(proto, clock):
    woken = []
    proto.transport.wake_at = woken.append
    _first_of_two_done(proto, _upload)
    asked = len(proto.packets)
    clock.t += LOCK_QUIET * 0.5
    proto.save_field_data(b'FTDC', struct.pack('>HBB', 0x6c02, 1, 2))
    clock.t += LOCK_QUIET * 0.5                  # the first wake comes
    proto._service_yields()
    assert len(proto.packets) == asked
    assert woken[-1] == clock.t + LOCK_QUIET * 0.5
    clock.t += LOCK_QUIET * 0.5
    proto._service_yields()
    assert names(proto.packets[-1]) == ['PartialLockCommand']


def test_a_clear_next_takes_the_whole_store_after_the_yield(proto, clock):
    _upload(proto, 1)
    proto.queue_clear(0, 5)
    grant(proto)
    tid = proto.transfer.tid
    budget(proto, 10)
    done(proto, tid)
    assert names(proto.packets[-1]) == ['LockCommand']
    clock.t += LOCK_QUIET
    proto._service_yields()
    assert names(proto.packets[-1]) == ['LockCommand'] and proto.packets[-1][0].state is True


def test_a_late_release_announcement_keeps_the_new_grant(proto, clock):
    # The announcement can come after LOCK_QUIET, and so after the LKOB
    # for the next request.
    _first_of_two_done(proto, _upload)
    clock.t += LOCK_QUIET
    proto._service_yields()
    grant(proto)
    tid = proto.transfer.tid
    sent = len(proto.packets)
    # The first frame's release, announced a frame late, after the LKOB.
    lkst(proto, 0, False)
    assert proto.locks[0] is True
    assert len(proto.packets) == sent, 'no request from the holder'
    lkst(proto, 0, True)
    budget(proto, 10)
    done(proto, tid)
    assert names(proto.packets[-1]) == ['LockCommand'], 'the second frame given back'
    assert proto.packets[-1][0].state is False
    assert proto.locks[0] is False


def test_ftde_6_still_leaves_the_request_to_the_release_broadcast(proto, clock):
    _upload(proto, 1)
    grant(proto)
    proto.save_field_data(b'FTDE', struct.pack('>HBx', proto.transfer.tid, 6))
    assert names(proto.packets[-1]) == ['LockCommand'], 'another session is queued: no request with it'
    lkst(proto, 0, False)
    clock.t += LOCK_COURTESY
    proto._service_yields()
    assert names(proto.packets[-1]) == ['PartialLockCommand']


def test_a_retried_upload_starts_again_with_its_metadata(proto):
    _upload(proto, 3)
    grant(proto)
    tid = proto.transfer.tid
    budget(proto, 4)
    assert metadata_sent(proto) == 1
    proto.save_field_data(b'FTDE', struct.pack('>HBx', tid, 1))     # try again
    assert names(proto.packets[-1]) == ['TransferUploadRequestCommand']
    del proto.chunks[:]
    budget(proto, 10)
    assert metadata_sent(proto) == 2
    assert b''.join(chunk_payloads(proto)) == DATA, 'from the start, not from where it stopped'


def test_draining_reports_the_queue_once():
    q = BaseProtocol()
    q.sent = []
    q._send_packet = q.sent.append
    for i in range(5):
        q.queue_packet(i)
    q.drain_queue()
    assert q.sent == [0, 1, 2, 3, 4] and not q.send_queue
    assert q.queue_trigger() is True
    assert q.queue_trigger() is False


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------

class _Sock:
    """Records sends; refuses the first ``refuse`` of them with ``err``."""

    def __init__(self, refuse=0, err=errno.ENOBUFS):
        self.sent = []
        self.refuse = refuse
        self.err = err

    def sendto(self, raw, addr):
        if self.refuse:
            self.refuse -= 1
            raise OSError(self.err, 'refused')
        self.sent.append(bytes(raw))
        return len(raw)

    def close(self):
        pass


@pytest.fixture
def udp(monkeypatch):
    monkeypatch.setattr(UdpProtocol, 'SEND_RETRIES', 3)
    monkeypatch.setattr('atemwire.transport.time.sleep', lambda s: None)
    p = UdpProtocol('127.0.0.1')
    p.sock.close()
    p.sock = _Sock()
    p.session_id = 0x8001
    p.local_sequence_number = 0
    p.state = UdpProtocol.STATE_ESTABLISHED
    yield p
    p.thread_queue.close()


def send_reliable(p, payload=b'\x00\x08TEST'):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.data = payload
    p._send_packet_low(pkt)
    return pkt


def sent_seqs(p, retransmitted=None):
    out = [Packet.from_bytes(raw) for raw in p.sock.sent]
    if retransmitted is not None:
        out = [q for q in out if bool(q.flags & UdpProtocol.FLAG_RETRANSMISSION) == retransmitted]
    return [q.sequence_number for q in out]


def test_a_refused_send_is_tried_again(udp):
    udp.sock.refuse = 2
    send_reliable(udp)
    assert sent_seqs(udp) == [1]
    assert udp.local_sequence_number == 1


def test_a_packet_the_network_keeps_refusing_keeps_its_number(udp):
    udp.sock.refuse = 3                          # every try
    send_reliable(udp, b'\x00\x08LOST')
    assert udp.sock.sent == []
    assert udp.local_sequence_number == 1, 'its number is used, so the switcher sees a gap'
    assert 1 in udp.retransmission_buffer
    send_reliable(udp, b'\x00\x08NEXT')
    assert sent_seqs(udp) == [2]
    udp._retransmit_from(1)                      # the switcher asks for the gap
    assert sent_seqs(udp, retransmitted=True) == [1, 2]


def test_other_send_errors_still_raise(udp):
    udp.sock.refuse, udp.sock.err = 1, errno.EBADF
    with pytest.raises(OSError):
        send_reliable(udp)


def test_what_the_switcher_has_not_acknowledged_is_resent(udp):
    for _ in range(3):
        send_reliable(udp)
    assert udp._resend_due is not None
    udp._on_ack(1)                               # it has 1, not 2 and 3
    udp._resend_unacked()
    assert sent_seqs(udp, retransmitted=True) == [], 'not before RESEND_AFTER'
    udp._resend_due = 0.0                        # RESEND_AFTER has passed
    udp._resend_unacked()
    assert sent_seqs(udp, retransmitted=True) == [2, 3]
    assert udp._resend_due is not None, 'and again later if still not acknowledged'


def test_an_ack_for_everything_stops_the_timer(udp):
    send_reliable(udp)
    send_reliable(udp)
    udp._on_ack(2)
    assert udp._resend_due is None
    udp._resend_unacked()
    assert sent_seqs(udp, retransmitted=True) == []


def test_nothing_is_resent_before_the_first_ack(udp):
    send_reliable(udp)
    udp._resend_due = 0.0
    udp._resend_unacked()
    assert sent_seqs(udp, retransmitted=True) == []


def test_the_switchers_acks_are_read(udp):
    for _ in range(3):
        send_reliable(udp)
    ack = Packet()
    ack.flags = UdpProtocol.FLAG_ACK
    ack.session = udp.session_id
    ack.acknowledgement_number = 2
    ack.data = b''
    udp.sock.recvfrom = lambda n: (ack.to_bytes(), ('127.0.0.1', 9910))
    udp._receive_packet_low()
    assert udp._acked_through == 2


def test_wake_at_hands_the_protocol_a_wakeup_when_due(udp, monkeypatch):
    from atemwire.transport import Wakeup
    now = [500.0]
    monkeypatch.setattr('atemwire.transport.time.monotonic', lambda: now[0])
    udp.wake_at(501.0)
    udp.wake_at(502.0)                           # the earliest wins
    udp._fire_wake()
    assert udp.thread_recv_queue.empty(), 'not before it is due'
    now[0] = 501.0
    udp._fire_wake()
    assert isinstance(udp.thread_recv_queue.get_nowait(), Wakeup)
    udp._fire_wake()
    assert udp.thread_recv_queue.empty(), 'once'


def test_wake_at_wakes_the_udp_thread_only_for_a_sooner_time(udp):
    udp.wake_at(501.0)
    udp.wake_at(502.0)
    udp.wake_at(501.0)
    assert udp.thread_queue.qsize() == 1
    udp.wake_at(500.5)
    assert udp.thread_queue.qsize() == 2
