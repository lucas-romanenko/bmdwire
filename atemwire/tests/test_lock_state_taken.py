# SPDX-License-Identifier: LGPL-3.0-only
"""``LKST`` reporting a lock taken is stored and raised (bmdwire#15).

The ``lock-state`` branch returned early on ``state=True``, so a lock taken
(ours or another client's) left nothing in ``mixerstate`` and fired no event;
``mixerstate['lock-state']`` only ever showed the last release, and nobody
could see the media store held. Both states are now stored (bare, the
latest wins) and raise ``change:lock-state``. The transfer machinery's
reactions stay release-only: a lock taken never touches ``locks``, never
clears a pending request, and never triggers a transfer.
"""
import struct

import pytest

from atemwire._transfer import TransferTask
from atemwire.protocol import AtemProtocol


def _lkst(store, state):
    return struct.pack('>H?B', store, state, 0)


@pytest.fixture
def proto():
    p = AtemProtocol('127.0.0.1')
    p.sent = []
    p.send_commands = p.sent.append          # nothing reaches the wire
    p.seen = []
    p.on('change:lock-state', p.seen.append)
    try:
        yield p
    finally:
        p.transport.sock.close()
        p.transport.thread_queue.close()


def test_lock_taken_is_stored_and_raised(proto):
    proto.save_field_data(b'LKST', _lkst(0, True))
    assert proto.mixerstate['lock-state'].store == 0
    assert proto.mixerstate['lock-state'].state is True
    assert len(proto.seen) == 1 and proto.seen[0].state is True


def test_lock_taken_does_not_touch_the_transfer_machinery(proto):
    # Someone else takes the store while we have a download queued and a
    # lock request out: nothing is sent, nothing forgotten, nothing granted.
    proto.transfer_queue[0] = [TransferTask(0, 3)]
    proto._lock_requested[0] = 123.0
    proto.save_field_data(b'LKST', _lkst(0, True))
    assert proto.sent == []
    assert proto.locks == {}
    assert proto._lock_requested == {0: 123.0}
    assert proto.transfer_requested is False


def test_our_own_lock_stays_ours(proto):
    proto.locks[0] = True
    proto.save_field_data(b'LKST', _lkst(0, True))
    assert proto.locks == {0: True}
    assert proto.mixerstate['lock-state'].state is True


def test_a_release_announced_while_we_hold_the_store_is_our_own_late(proto):
    # The switcher announces lock changes once a frame: the release of the
    # frame we gave back arrives after the LKOB for the request we sent
    # with it (ASC's restore capture, 2026-10-15). It must not cost us the
    # new grant, or we skip the next release and ask as the holder, which
    # the switcher ignores.
    proto.transfer_queue[0] = [TransferTask(0, 3)]
    proto.locks[0] = True
    proto.save_field_data(b'LKST', _lkst(0, False))
    assert proto.locks == {0: True}
    assert proto.sent == [], 'no pounce: we hold it'
    assert proto.mixerstate['lock-state'].state is False, 'still stored, as announced'
    assert [c.state for c in proto.seen] == [False]


def test_taken_then_released(proto):
    proto.transfer_queue[0] = [TransferTask(0, 3)]
    proto.save_field_data(b'LKST', _lkst(0, True))
    assert proto.sent == []
    proto.save_field_data(b'LKST', _lkst(0, False))
    assert proto.mixerstate['lock-state'].state is False
    assert [c.state for c in proto.seen] == [True, False]
    # The release still pounces: the queued download asks for the lock.
    assert len(proto.sent) == 1
    assert [type(c).__name__ for c in proto.sent[0]] == ['PartialLockCommand']
