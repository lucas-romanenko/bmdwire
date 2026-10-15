# SPDX-License-Identifier: LGPL-3.0-only
"""Uploads as ATEM Software Control sends them (its restore capture,
2026-10-15; docs/MEDIA_LOCK.md).

Between two transfers ASC sends the release and the next lock request in
one packet, and the switcher grants it in under a millisecond; it sends
FTFD with the first FTCD budget, before the data; and it sends each budget
whole, back to back. Twenty 1080p stills took it about 12 s. atemwire
waited for the release's LKST echo before asking again (up to 6.6 s on
the same switcher), sent FTFD after the last chunk, and sent five chunks
per packet from the switcher, so a still took 23 s.

The lock request it used to defer was never eaten by the switcher: the
release broadcast of the frame given back arrives a frame later, after the
LKOB for the new request, and atemwire read it as losing the new grant.

The transport half: a send the kernel refuses is retried and, failing
that, keeps its sequence number so it is resent; and what the switcher
has not acknowledged is resent after RESEND_AFTER, since nothing follows
the last chunk of a budget to show the switcher it is missing.
"""
import errno
import struct

import pytest

from atemwire.protocol import AtemProtocol
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
def test_the_release_and_the_next_request_share_a_packet(proto, make):
    _first_of_two_done(proto, make)
    handover = proto.packets[-1]
    assert names(handover) == ['LockCommand', 'PartialLockCommand']
    assert handover[0].state is False and handover[1].slot == 2
    # Granted at once, as the switcher does: no wait for an LKST.
    grant(proto)
    assert proto.transfer.slot == 2
    assert proto.transfer_requested is True


def test_a_clear_next_takes_the_whole_store_in_the_same_packet(proto):
    _upload(proto, 1)
    proto.queue_clear(0, 5)
    grant(proto)
    tid = proto.transfer.tid
    budget(proto, 10)
    done(proto, tid)
    handover = proto.packets[-1]
    assert names(handover) == ['LockCommand', 'LockCommand']
    assert [c.state for c in handover] == [False, True]


def test_a_late_release_announcement_keeps_the_new_grant(proto):
    _first_of_two_done(proto, _upload)
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


def test_ftde_6_still_leaves_the_request_to_the_release_broadcast(proto):
    _upload(proto, 1)
    grant(proto)
    proto.save_field_data(b'FTDE', struct.pack('>HBx', proto.transfer.tid, 6))
    assert names(proto.packets[-1]) == ['LockCommand'], 'another session is queued: no request with it'
    lkst(proto, 0, False)
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
