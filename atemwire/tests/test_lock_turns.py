# SPDX-License-Identifier: LGPL-3.0-only
"""Two sessions of this package taking turns at the switcher's stores.

bmdwire#64: every session started its transfer ids at 43. The switcher
broadcasts FTDC to every session with the owner's id, so with a still
download in one session and a macro download in another, the still's
session took the macro's FTDC for its own, dropped its task, and ignored
the chunks of the transfer the switcher then started for it (153 on a
Constellation HD), never acknowledging them. The switcher, its transfer
engine held, refused every later request from both with FTDE 1, for good.
Each session now starts at a random id.

bmdwire#63: after a release, the releaser and the session waiting on it
both asked LOCK_COURTESY after the release was announced, 0.3 ms apart on
a Constellation HD, and one of the two transfers was aborted. The turn
after our own release now goes to the waiter: we wait LOCK_HANDOVER more,
while another client has been seen lately (LOCK_QUIET), and the waiter's
grant, announced meanwhile, makes the store theirs.
"""
import struct

import pytest

import atemwire.protocol as protocol
from atemwire.protocol import (
    LOCK_COURTESY,
    LOCK_ECHO_WINDOW,
    LOCK_HANDOVER,
    LOCK_QUIET,
    AtemProtocol,
)


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


def _make():
    p = AtemProtocol(ip='127.0.0.1')
    p.transport.sock.close()
    p.packets = []
    p.send_commands = lambda commands: p.packets.append(list(commands))
    p.transport._send_packet = lambda packet: None
    p.wakes = []
    p.transport.wake_at = p.wakes.append
    p.initialized = True
    p.connected = True
    return p


@pytest.fixture
def proto():
    p = _make()
    try:
        yield p
    finally:
        p.transport.thread_queue.close()


@pytest.fixture
def pair():
    a, b = _make(), _make()
    try:
        yield a, b
    finally:
        a.transport.thread_queue.close()
        b.transport.thread_queue.close()


def names(p):
    return [[type(c).__name__ for c in packet] for packet in p.packets]


def last(p):
    return names(p)[-1] if p.packets else None


def grant(p, store=0):
    p.save_field_data(b'LKOB', struct.pack('>Hxx', store))


def lkst(p, store, state):
    p.save_field_data(b'LKST', struct.pack('>H?B', store, state, 0))


def ftdc(p, tid):
    p.save_field_data(b'FTDC', struct.pack('>HBB', tid, 1, 2))


def requests(p):
    return [n for packet in names(p) for n in packet
            if n in ('TransferDownloadRequestCommand', 'TransferUploadRequestCommand')]


# ---------------------------------------------------------------------------
# bmdwire#64: each session its own transfer ids
# ---------------------------------------------------------------------------

def test_each_session_draws_its_first_transfer_id(monkeypatch):
    drawn = iter([1000, 52000])
    monkeypatch.setattr('atemwire.protocol.random.randrange',
                        lambda lo, hi: next(drawn))
    a, b = _make(), _make()
    try:
        a.download(0xFFFF, 1)
        b.download(0xFFFF, 1)
        assert a.transfer.tid == 1001 and b.transfer.tid == 52001, 'both used to be 43'
    finally:
        a.transport.thread_queue.close()
        b.transport.thread_queue.close()


def test_first_transfer_ids_are_spread_over_the_u16_range():
    ids = [protocol._first_transfer_id() for _ in range(400)]
    assert all(1 <= i <= 0xFFFF for i in ids)
    assert len(set(ids)) > 390


def test_another_sessions_completion_does_not_end_ours(pair, monkeypatch):
    # The switcher's FTDC goes to every session with the owner's id: with
    # ids of their own, B's transfer outlives A's completion.
    a, b = pair
    a.transfer_id, b.transfer_id = 4000, 31000
    a.download(0xFFFF, 1)                        # a macro, in one session
    b.download(0, 5)                             # a still, in the other
    grant(b)
    tid_a = a.transfer.tid
    ftdc(a, tid_a)
    ftdc(b, tid_a)                               # the same broadcast, for B
    assert b.transfer is not None and b.transfer.slot == 5
    assert b.transfer_queue[0], 'B kept its task'
    assert b.locks.get(0) is True, 'and its lock'


def test_transfer_ids_wrap_and_skip_zero(proto):
    proto.transfer_id = 0xFFFF
    proto.download(0xFFFF, 1)
    assert proto.transfer.tid == 1
    ftdc(proto, 1)
    proto.download(0xFFFF, 2)
    assert proto.transfer.tid == 2


# ---------------------------------------------------------------------------
# bmdwire#63: the turn after our release goes to the session waiting on it
# ---------------------------------------------------------------------------

def _seen_each_other(a, b):
    """B downloads one still first, as a media pool watcher does: A sees
    B's lock announced and B's transfer end."""
    b.download(0, 5)
    grant(b)
    lkst(a, 0, True)
    lkst(b, 0, True)
    tid = b.transfer.tid
    ftdc(b, tid)                                 # B's own: B releases
    ftdc(a, tid)                                 # another session's, for A
    lkst(a, 0, False)
    lkst(b, 0, False)


def test_the_waiter_takes_the_turn_after_our_release(pair, clock):
    a, b = pair
    _seen_each_other(a, b)
    clock.t += 1.0
    a.upload(0, 1, b'\x00' * 64, compress=False)
    a.upload(0, 2, b'\x00' * 64, compress=False)
    grant(a)
    lkst(a, 0, True)
    lkst(b, 0, True)                             # A's, for B
    b.download(0, 5)                             # B waits on A
    assert requests(b) == ['TransferDownloadRequestCommand'], 'no request while A holds it'
    tid_a = a.transfer.tid
    ftdc(a, tid_a)                               # A's first upload done
    ftdc(b, tid_a)                               # another session's, for B
    assert last(a) == ['LockCommand'], 'A releases alone'
    lkst(a, 0, False)                            # A's release, announced to both
    lkst(b, 0, False)
    a_asks, b_asks = len(a.packets), len(b.packets)

    clock.t += LOCK_COURTESY
    a._service_yields()
    b._service_yields()
    assert last(b) == ['PartialLockCommand'], 'the waiter asks after the courtesy'
    assert len(a.packets) == a_asks, 'and A, whose release it was, does not ask beside it'

    grant(b)
    lkst(a, 0, True)                             # B's grant, announced
    lkst(b, 0, True)
    clock.t += LOCK_HANDOVER
    a._service_yields()
    assert len(a.packets) == a_asks, 'the store is B\'s now'

    tid_b = b.transfer.tid
    ftdc(b, tid_b)                               # B done: B releases
    ftdc(a, tid_b)
    assert last(b) == ['LockCommand']
    lkst(a, 0, False)
    lkst(b, 0, False)
    clock.t += LOCK_COURTESY
    a._service_yields()
    b._service_yields()
    assert last(a) == ['PartialLockCommand'], 'A asks as the waiter now'
    assert last(b) == ['LockCommand'], 'B, done, asks nothing'
    assert b_asks < len(b.packets)


def test_alone_the_turn_after_our_release_is_not_lengthened(proto, clock):
    proto.download(0, 1)
    proto.download(0, 2)
    grant(proto)
    ftdc(proto, proto.transfer.tid)
    lkst(proto, 0, False)
    clock.t += LOCK_COURTESY
    proto._service_yields()
    assert last(proto) == ['PartialLockCommand'], 'nobody seen: nobody to wait for'


def test_another_client_seen_long_ago_does_not_lengthen_it(proto, clock):
    lkst(proto, 0, True)                         # someone else's lock
    lkst(proto, 0, False)
    clock.t += LOCK_QUIET + 1
    proto.download(0, 1)
    proto.download(0, 2)
    grant(proto)
    ftdc(proto, proto.transfer.tid)
    lkst(proto, 0, False)
    clock.t += LOCK_COURTESY
    proto._service_yields()
    assert last(proto) == ['PartialLockCommand']


def test_a_release_announced_after_the_echo_window_is_another_clients(proto, clock):
    lkst(proto, 0, True)                         # another client, lately
    lkst(proto, 0, False)
    proto.download(0, 1)
    proto.download(0, 2)
    clock.t += LOCK_COURTESY
    proto._service_yields()
    grant(proto)
    ftdc(proto, proto.transfer.tid)              # we release; nothing announced:
    clock.t += LOCK_ECHO_WINDOW * 2              # handed to a waiting request
    lkst(proto, 0, False)                        # its holder lets go
    clock.t += LOCK_COURTESY
    proto._service_yields()
    assert last(proto) == ['PartialLockCommand'], 'we are its waiter, not the releaser'


def test_our_release_echo_with_another_client_seen_waits_the_handover(proto, clock):
    lkst(proto, 0, True)                         # another client, lately
    lkst(proto, 0, False)
    proto.download(0, 1)
    proto.download(0, 2)
    clock.t += LOCK_COURTESY
    proto._service_yields()
    grant(proto)
    ftdc(proto, proto.transfer.tid)
    clock.t += 0.04                              # a frame: our release's own LKST
    lkst(proto, 0, False)
    asked = len(proto.packets)
    clock.t += LOCK_COURTESY
    proto._service_yields()
    assert len(proto.packets) == asked, 'asked with a waiter'
    assert proto.wakes[-1] == pytest.approx(clock.t + LOCK_HANDOVER)
    clock.t += LOCK_HANDOVER
    proto._service_yields()
    assert last(proto) == ['PartialLockCommand'], 'nobody took it: ours again'


def test_a_late_announcement_of_our_own_grant_is_not_another_client(proto, clock):
    # A transfer (or a clear) shorter than a frame is released before the
    # switcher announces its grant: that LKST comes after our release. It
    # is ours, and must not make every later turn wait LOCK_HANDOVER.
    for slot in (1, 2):
        proto.download(0, slot)
        if slot == 1:
            grant(proto)
        else:
            clock.t += LOCK_COURTESY
            proto._service_yields()
            grant(proto)
        clock.t += 0.01
        ftdc(proto, proto.transfer.tid)          # done, released
        clock.t += 0.02
        lkst(proto, 0, True)                     # our grant, announced late
        lkst(proto, 0, False)                    # and our release
    assert proto._others_seen is None
    # The next transfers, a normal length, alone: no handover.
    proto.download(0, 3)
    proto.download(0, 4)
    clock.t += LOCK_COURTESY
    proto._service_yields()
    grant(proto)
    clock.t += 2.0
    ftdc(proto, proto.transfer.tid)
    clock.t += 0.04
    lkst(proto, 0, False)                        # our release's echo
    clock.t += LOCK_COURTESY
    proto._service_yields()
    assert last(proto) == ['PartialLockCommand'], 'waited a handover for nobody'
