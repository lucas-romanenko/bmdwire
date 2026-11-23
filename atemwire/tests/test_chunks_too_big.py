# SPDX-License-Identifier: LGPL-3.0-only
"""Upload chunks too big for the path go again smaller (WebATEM #63).

An ATEM Mini Pro over a VPN at 80 ms: the lock request, FTSD and FTFD went
through, and not one data chunk (1,416-byte datagrams) in 90 s and some 70
resends. The switcher takes our packets in order only, so nothing after the
first chunk counted either; it asked for that chunk again the moment a small
packet came after it (the lock release, after the caller gave up).

When the first packet the switcher has not acknowledged is a data chunk,
sent TOO_BIG_AFTER times more while the switcher goes on sending, it and
every chunk after it are taken back and go again in chunks of the next
CHUNK_STEPS size, for that switcher from then on.
"""
import struct

import pytest

from atemwire import transport as T
from atemwire.messages import LockCommand, TransferDataCommand
from atemwire.protocol import AtemProtocol
from atemwire.tests.test_upload_like_asc import _Sock
from atemwire.transport import ChunksTakenBack, Packet, UdpProtocol


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(T, '_CHUNK_LIMITS', {})
    return T._CHUNK_LIMITS


@pytest.fixture
def clock(monkeypatch):
    t = [100.0]
    monkeypatch.setattr('atemwire.transport.time.monotonic', lambda: t[0])
    return t


@pytest.fixture
def udp(monkeypatch, clock):
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


def reliable(data):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.data = data
    return pkt


def chunk(n, size=1392):
    return reliable(TransferDataCommand(7, bytes([n]) * size).get_command())


def a_stuck_upload(udp, clock, chunks=10):
    """FTFD acknowledged, then chunks the switcher never gets."""
    udp._send_packet_low(reliable(b'\x00\xdcFTFD' + bytes(212)))
    clock[0] += 0.08
    udp._on_ack(1, ack_only=True)
    for n in range(chunks):
        udp._send_packet_low(chunk(n))


def timer_fires(udp, clock, times, answering=True):
    for _ in range(times):
        clock[0] = udp._resend_due if udp._resend_due else clock[0] + 1
        if answering:
            udp.last_rx_monotonic = clock[0]     # its Time packets keep coming
        udp._resend_unacked()


def taken_back(udp):
    out = []
    while not udp.thread_recv_queue.empty():
        item = udp.thread_recv_queue.get_nowait()
        if isinstance(item, ChunksTakenBack):
            out.append(item)
    return out


def test_chunks_that_never_get_through_are_taken_back(udp, clock, limits):
    a_stuck_upload(udp, clock)
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER)
    assert taken_back(udp) == [], 'not before TOO_BIG_AFTER resends'
    timer_fires(udp, clock, 1)
    [taken] = taken_back(udp)
    assert (taken.transfer, taken.sizes, taken.limit) == (7, [1392] * 10, 1224)
    assert udp.local_sequence_number == 1, 'their sequence numbers are free again'
    assert all(seq <= 1 for seq in udp.retransmission_buffer)
    assert udp.max_chunk == 1224 and limits == {'127.0.0.1': 1224}


def test_a_lost_chunk_on_a_quiet_link_is_only_resent(udp, clock):
    # Nothing from the switcher either: the link is down, not too small.
    a_stuck_upload(udp, clock)
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER + 2, answering=False)
    assert taken_back(udp) == []
    assert udp.max_chunk is None


def test_an_acknowledgement_starts_the_count_again(udp, clock):
    a_stuck_upload(udp, clock)
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER)
    udp._on_ack(2, ack_only=True)                # the first chunk got there
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER)
    assert taken_back(udp) == []


def test_only_data_chunks_are_made_smaller(udp, clock):
    udp._send_packet_low(reliable(b'\x00\x08TEST'))
    clock[0] += 0.08
    udp._on_ack(1, ack_only=True)
    udp._send_packet_low(reliable(b'\x00\x0cLOCK' + bytes(4)))
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER + 2)
    assert taken_back(udp) == []


def test_the_steps_go_down_to_the_smallest(udp, clock, limits):
    limits['127.0.0.1'] = UdpProtocol.CHUNK_STEPS[-1]
    udp._send_packet_low(reliable(b'\x00\xdcFTFD' + bytes(212)))
    clock[0] += 0.08
    udp._on_ack(1, ack_only=True)
    udp._send_packet_low(chunk(0, size=UdpProtocol.CHUNK_STEPS[-1]))
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER + 2)
    assert taken_back(udp) == [], 'nothing smaller to try'


def test_other_packets_among_them_go_again_now_in_order(udp, clock):
    a_stuck_upload(udp, clock, chunks=3)
    release = reliable(LockCommand(0, False).get_command())
    udp._send_packet_low(release)
    udp.sock.sent.clear()
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER + 1)
    [taken] = taken_back(udp)
    assert taken.sizes == [1392] * 3
    resent = [Packet.from_bytes(r) for r in udp.sock.sent]
    assert [(q.sequence_number, bytes(q.data)[4:8]) for q in resent][-1] == (2, b'LOCK')
    assert not resent[-1].flags & UdpProtocol.FLAG_RETRANSMISSION


# --- the protocol: the chunks go again, smaller ---------------------------

@pytest.fixture
def proto():
    p = AtemProtocol(ip='127.0.0.1')
    p.transport.sock.close()
    p.send_commands = lambda commands: None
    p.chunks = []
    p.transport.queue_packet = lambda pkt: p.chunks.append(bytes(pkt.data))
    p.transport.drain_queue = lambda: None
    p.initialized = p.connected = True
    try:
        yield p
    finally:
        p.transport.thread_queue.close()


def payloads(p):
    # FTDa: 8-byte command header, transfer id and size, then the data.
    return [c[12:] for c in p.chunks]


def start(p, data, store=0):
    p.upload(store, 0, data, compress=False)
    p.save_field_data(b'LKOB', struct.pack('>Hxx', store))
    p.save_field_data(b'FTCD', struct.pack('>H4xHH2x', p.transfer.tid, 1396, 768))


def take_back(p, n):
    sizes = [len(c) for c in payloads(p)[-n:]]
    p.transport.max_chunk = 1224
    p._chunks_taken_back(ChunksTakenBack(p.transfer.tid, sizes, 1224))


DATA = bytes((i * 7) % 251 for i in range(1392 * 10))


def test_they_go_again_from_where_they_started(proto):
    start(proto, DATA)
    assert len(payloads(proto)) == 10
    take_back(proto, 10)
    again = payloads(proto)[10:]
    assert {len(c) for c in again[:-1]} == {1224}
    assert b''.join(again) == DATA


def test_part_of_a_budget_taken_back(proto):
    start(proto, DATA)
    take_back(proto, 4)                          # the switcher had the first six
    assert b''.join(payloads(proto)[:6] + payloads(proto)[10:]) == DATA


def test_a_last_short_chunk_taken_back(proto):
    # A macro: 4,096 bytes, the last chunk 1,312. The offset runs past the
    # end after it; the first 80 bytes were lost when it was not clamped.
    macro = bytes(range(256)) * 16
    start(proto, macro, store=0xFFFF)
    assert [len(c) for c in payloads(proto)] == [1392, 1392, 1312]
    take_back(proto, 3)
    assert b''.join(payloads(proto)[3:]) == macro


def test_never_more_chunks_than_the_budget(proto):
    # Ten chunks granted, all ten taken back: ten go again, 12,240 of the
    # 13,920 bytes, and the rest waits for the next FTCD.
    proto.upload(0, 0, DATA, compress=False)
    proto.save_field_data(b'LKOB', struct.pack('>Hxx', 0))
    proto.save_field_data(b'FTCD', struct.pack('>H4xHH2x', proto.transfer.tid, 1396, 10))
    take_back(proto, 10)
    assert len(payloads(proto)) == 20
    assert proto.transfer.offset == 1224 * 10


def test_chunks_of_a_transfer_gone_are_dropped(proto):
    start(proto, DATA)
    proto._chunks_taken_back(ChunksTakenBack(proto.transfer.tid ^ 1, [1392], 1224))
    assert len(payloads(proto)) == 10


def test_the_next_session_to_that_switcher_starts_small(limits):
    limits['127.0.0.1'] = 1224
    p = AtemProtocol(ip='127.0.0.1')
    p.transport.sock.close()
    p.send_commands = lambda commands: None
    p.chunks = []
    p.transport.queue_packet = lambda pkt: p.chunks.append(bytes(pkt.data))
    p.transport.drain_queue = lambda: None
    p.initialized = p.connected = True
    try:
        start(p, DATA)
        assert {len(c) for c in payloads(p)[:-1]} == {1224}
        assert b''.join(payloads(p)) == DATA
    finally:
        p.transport.thread_queue.close()


# --- the whole upload, over a path that drops datagrams over 1,400 bytes --

def test_an_upload_over_a_path_that_drops_big_datagrams(clock, monkeypatch):
    monkeypatch.setattr('atemwire.transport.time.sleep', lambda s: None)
    p = AtemProtocol(ip='127.0.0.1')
    p.transport.sock.close()
    udp = p.transport
    udp.sock = _Sock()
    udp.session_id = 0x8001
    udp.local_sequence_number = 0
    udp.state = UdpProtocol.STATE_ESTABLISHED
    p.send_commands = lambda commands: [udp._send_packet_low(reliable(c.get_command())) for c in commands]
    p.initialized = p.connected = True
    got, have, read, budget = [], [0], [0], 4
    try:
        p.upload(0, 0, DATA, compress=False)
        p.save_field_data(b'LKOB', struct.pack('>Hxx', 0))
        udp._on_ack(have[0], ack_only=True)
        p.save_field_data(b'FTCD', struct.pack('>H4xHH2x', p.transfer.tid, 1396, budget))
        left = [budget]
        for _ in range(200):
            for item in udp.thread_queue.get_waiting():
                if item is not None:
                    udp._send_on_udp_thread(item)
            for raw in udp.sock.sent[read[0]:]:
                if len(raw) > 1400:
                    continue                     # dropped on the way
                q = Packet.from_bytes(raw)
                if q.sequence_number != (have[0] + 1) & 0x7FFF:
                    continue                     # in order only
                have[0] = q.sequence_number
                data = bytes(q.data)
                if data[4:8] == b'FTDa':
                    got.append(data[12:])
                    left[0] -= 1
            read[0] = len(udp.sock.sent)
            clock[0] += 0.08
            udp.last_rx_monotonic = clock[0]
            udp._on_ack(have[0], ack_only=True)
            if sum(map(len, got)) >= len(DATA):
                break
            if left[0] == 0:
                left[0] = budget
                p.save_field_data(b'FTCD', struct.pack('>H4xHH2x', p.transfer.tid, 1396, budget))
            if udp._resend_due is not None and clock[0] >= udp._resend_due:
                udp._resend_unacked()
            while not udp.thread_recv_queue.empty():
                item = udp.thread_recv_queue.get_nowait()
                if isinstance(item, ChunksTakenBack):
                    p._chunks_taken_back(item)
        assert b''.join(got) == DATA
    finally:
        udp.thread_queue.close()


def test_chunks_not_sent_yet_come_back_too(udp, clock):
    # Queued for the UDP thread after the first ones went: they would
    # have gone after them.
    a_stuck_upload(udp, clock, chunks=3)
    udp.thread_queue.put(chunk(3))
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER + 1)
    [taken] = taken_back(udp)
    assert taken.sizes == [1392] * 4
    assert udp.thread_queue.get_waiting() == []


def test_the_protocol_loop_takes_them(proto):
    start(proto, DATA)
    sizes = [len(c) for c in payloads(proto)]
    proto.transport.max_chunk = 1224
    proto.transport.state = UdpProtocol.STATE_ESTABLISHED
    proto.transport.thread_recv_queue.put(ChunksTakenBack(proto.transfer.tid, sizes, 1224))
    proto.loop()
    assert b''.join(payloads(proto)[10:]) == DATA
