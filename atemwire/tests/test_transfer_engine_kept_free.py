# SPDX-License-Identifier: LGPL-3.0-only
"""A download is never cut off mid-stream, and FTDE 1 is asked again with a
pause (2026-11-04, an ATEM 4 M/E Constellation 4K Plus, a capture of it).

WebATEM gave a still 15 s. A 4K one is 12 MB or more, and at 15 s into it
the timeout's abort released the store lock with the switcher still
sending. Its transfer engine stayed with that transfer, and every later
request got FTDE 1 (try again), which the protocol asked again every
~10 ms with no end: ~900 requests in 13 s, and no still ever again.

An abort now drains a download that is still arriving: its chunks are
acknowledged and dropped, and the lock goes back at its FTDC, as for any
finished transfer (or after DRAIN_STALL with nothing, as before). FTDE 1 is
asked again after TRY_AGAIN_FIRST, doubling to TRY_AGAIN_MAX, and after
TRY_AGAIN_ATTEMPTS the transfer fails and its lock goes back.
"""
import struct

import pytest

from atemwire.protocol import (DRAIN_MAX, DRAIN_STALL, TRY_AGAIN_ATTEMPTS, TRY_AGAIN_FIRST,
                               TRY_AGAIN_MAX, AtemProtocol)


@pytest.fixture
def clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr('atemwire.protocol.time.monotonic', lambda: t[0])
    return t


@pytest.fixture
def proto(clock):
    p = AtemProtocol(ip='127.0.0.1')
    p.transport.sock.close()
    p.packets = []
    p.send_commands = lambda commands: p.packets.append(list(commands))
    p.wakes = []
    p.transport.wake_at = p.wakes.append
    p.initialized = True
    p.connected = True
    try:
        yield p
    finally:
        p.transport.thread_queue.close()


def names(packet):
    return [type(c).__name__ for c in packet]


def sent(p):
    return [names(pk) for pk in p.packets]


def started(p, slot=1):
    """A download granted and requested: FTSU on the wire."""
    p.download(0, slot)
    p.save_field_data(b'LKOB', struct.pack('>Hxx', 0))
    assert sent(p)[-1] == ['TransferDownloadRequestCommand']
    return p.transfer.tid


def chunk(p, tid, data=b'\x01' * 8):
    p.save_field_data(b'FTDa', struct.pack('>HH', tid, len(data)) + data)


def ftde(p, tid, status):
    p.save_field_data(b'FTDE', struct.pack('>HBx', tid, status))


def ftdc(p, tid):
    p.save_field_data(b'FTDC', struct.pack('>HBB', tid, 1, 2))


def test_an_abandoned_download_is_acknowledged_to_its_end(proto, clock):
    tid = started(proto)
    chunk(proto, tid)
    proto.abort_transfers()                      # the caller's timeout
    assert sent(proto)[-1] == ['TransferAckCommand'], 'no lock release mid-stream'
    assert proto.locks.get(0)

    before = len(proto.packets)
    for _ in range(3):
        clock[0] += 1
        chunk(proto, tid)
    assert sent(proto)[before:] == [['TransferAckCommand']] * 3, 'every chunk acknowledged'
    assert proto.transfer_buffer == [], 'and its data dropped'

    ftdc(proto, tid)
    assert sent(proto)[-1] == ['LockCommand'] and proto.packets[-1][0].state is False
    assert proto._draining is None


def test_a_retry_waits_for_the_abandoned_download(proto, clock):
    tid = started(proto)
    chunk(proto, tid)
    proto.abort_transfers()
    proto.download(0, 1)                         # the caller tries again
    assert not any(n == ['TransferDownloadRequestCommand'] for n in sent(proto)[2:])
    ftdc(proto, tid)                             # the old one ends
    clock[0] += 1
    proto.save_field_data(b'LKST', struct.pack('>H?B', 0, False, 0))
    clock[0] += 1
    proto._service_yields()
    assert sent(proto)[-1] == ['PartialLockCommand'], 'then the retry asks'


def test_a_download_that_stopped_arriving_gives_the_lock_back(proto, clock):
    tid = started(proto)
    chunk(proto, tid)
    proto.abort_transfers()
    clock[0] += DRAIN_STALL
    proto._service_yields()
    assert sent(proto)[-1] == ['LockCommand'] and proto.packets[-1][0].state is False
    assert proto._draining is None


def test_a_drain_is_capped(proto, clock):
    tid = started(proto)
    chunk(proto, tid)
    proto.abort_transfers()
    while clock[0] < 1000.0 + DRAIN_MAX:
        clock[0] += 1
        chunk(proto, tid)
    proto._service_yields()
    assert sent(proto)[-1] == ['LockCommand']


def test_an_abort_before_any_data_releases_at_once(proto):
    started(proto)
    proto.abort_transfers()
    assert sent(proto)[-1] == ['LockCommand'] and proto.packets[-1][0].state is False
    assert proto._draining is None


def test_an_ftde_ends_the_drain(proto):
    tid = started(proto)
    chunk(proto, tid)
    proto.abort_transfers()
    ftde(proto, tid, 2)
    assert sent(proto)[-1] == ['LockCommand']
    assert proto._draining is None


def test_try_again_is_asked_again_after_a_doubling_pause(proto, clock):
    tid = started(proto)
    pauses = []
    for attempt in range(1, 8):
        before = len(proto.packets)
        ftde(proto, tid, 1)
        proto._service_yields()
        assert len(proto.packets) == before, 'not asked again at once'
        due = proto._try_again_due
        pauses.append(round(due - clock[0], 4))
        clock[0] = due
        proto._service_yields()
        assert sent(proto)[-1] == ['TransferDownloadRequestCommand']
        tid = proto.transfer.tid
    assert pauses == [min(TRY_AGAIN_MAX, TRY_AGAIN_FIRST * 2 ** i) for i in range(7)]


def test_try_again_gives_up_and_frees_the_store(proto, clock):
    tid = started(proto)
    errors = []
    proto.on('file-transfer-error', errors.append)
    for _ in range(TRY_AGAIN_ATTEMPTS):
        ftde(proto, tid, 1)
        clock[0] = proto._try_again_due
        proto._service_yields()
        tid = proto.transfer.tid
    ftde(proto, tid, 1)                          # one refusal too many
    assert len(errors) == 1
    assert proto.transfer is None
    assert sent(proto)[-1] == ['LockCommand'] and proto.packets[-1][0].state is False
    assert not proto.transfer_queue.get(0)
