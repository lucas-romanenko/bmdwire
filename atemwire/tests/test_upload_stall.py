# SPDX-License-Identifier: LGPL-3.0-only
"""An upload is done only when the switcher says so; one that stops moving
is started again, then failed.

The hardware bench, 2026-11-24 (Constellation HD, fw 2.30, both units):
the cable out for 3 s in the middle of a still upload. Afterwards
everything was acknowledged on both sides, the switcher answered every
second and never granted the next budget: the still sat at 47 % until the
caller gave up. With no packet of ours newly acknowledged, no chunk cut
and no budget granted for UPLOAD_STALL, and no word from the switcher
that it is done, the upload now starts again from the beginning under a
new transfer, UPLOAD_RETRIES times at most ('upload-retrying'), then fails
('file-transfer-error', status 'stalled'). In every phase: the bench's
first fresh start asked for the lock and was never granted it, and
nothing watched that; only while the switcher says another client holds
the lock is the limit longer (LOCK_WAIT_MAX).
"""
import types

import pytest

from atemwire import protocol as P
from atemwire._transfer import TransferTask
from atemwire.protocol import AtemProtocol


@pytest.fixture
def clock(monkeypatch):
    t = [100.0]
    monkeypatch.setattr('atemwire.protocol.time.monotonic', lambda: t[0])
    return t


@pytest.fixture
def proto(clock):
    p = AtemProtocol.__new__(AtemProtocol)
    task = TransferTask(0, 3, upload=True)
    task.data = bytes(10000)
    task.offset = 4000
    task.tid = 7
    p.transfer = task
    p.transfer_queue = {0: [task]}
    p.transfer_budget = types.SimpleNamespace(count=8, size=1396)   # it may say anything
    p.transfer_requested = True
    p.transfer_buffer = []
    p.transfer_buffer_bytes = 0
    p.locks = {0: True}
    p._upload_mark = p._upload_mark_at = None
    p._grants = 4
    p.transport = types.SimpleNamespace(_acked_through=50, local_sequence_number=50, wakes=[])
    p.transport.wake_at = p.transport.wakes.append
    p.log = types.SimpleNamespace(warning=lambda *a: None, error=lambda *a: None,
                                  info=lambda *a: None, debug=lambda *a: None)
    p.events = []
    p._raise = lambda *a: p.events.append(a)
    p.released = []
    p._release_then_continue = lambda store, request_next=True: p.released.append(store)
    p._foreign_held = set()
    p._lock_requested = {}
    p._try_again_due = None
    p.triggered = []
    p._transfer_trigger = lambda store, retry=False: p.triggered.append(store)
    p.failed = []
    p._raise = lambda *a: (p.events.append(a), a[0] == 'file-transfer-error' and p.failed.append(a[1]))
    return p


def stall(proto, clock, seconds):
    proto._service_upload_stall()
    clock[0] += seconds
    proto._service_upload_stall()


def test_no_progress_for_upload_stall_starts_it_again(proto, clock):
    stall(proto, clock, P.UPLOAD_STALL - 0.01)
    assert proto.released == [] and proto.transport.wakes, 'waits, and wakes to look again'
    clock[0] += 0.01
    proto._service_upload_stall()
    assert proto.released == [0], 'its lock given back, the same task asked for again'
    assert not proto.transfer_requested and proto.transfer_budget is None
    assert ('upload-retrying', 0, 3, 1, P.UPLOAD_RETRIES) in proto.events
    assert proto.transfer_queue[0] == [proto.transfer], 'still first in the queue'


def test_whatever_moves_is_progress(proto, clock):
    proto._service_upload_stall()
    for step in range(10):
        clock[0] += P.UPLOAD_STALL - 1
        [lambda: setattr(proto.transport, '_acked_through', proto.transport._acked_through + 1),
         lambda: setattr(proto.transfer, 'offset', proto.transfer.offset + 1396),
         lambda: setattr(proto, '_grants', proto._grants + 1)][step % 3]()
        proto._service_upload_stall()
    assert proto.released == [] and proto.failed == []


def test_after_upload_retries_it_fails_with_the_reason(proto, clock):
    for _ in range(P.UPLOAD_RETRIES):
        stall(proto, clock, P.UPLOAD_STALL)
        proto.transfer_requested = True            # asked for again
    stall(proto, clock, P.UPLOAD_STALL)
    assert len(proto.released) == P.UPLOAD_RETRIES + 1, 'and its lock given back at the end'
    [failed] = proto.failed
    assert failed.status == 'stalled' and failed.transfer == 7 and 'fresh starts' in failed.message
    assert proto.transfer_queue[0] == [], 'off its queue'


def test_a_fresh_start_never_granted_its_lock_is_retried_then_failed(proto, clock):
    # The bench, 15:00 and 15:20: started again, the lock asked for and
    # never granted, nothing more for 114 s, no error.
    proto.transfer = None                          # given back, waiting again
    proto.locks = {0: False}
    proto.transfer_requested = False
    for _ in range(P.UPLOAD_RETRIES):
        stall(proto, clock, P.UPLOAD_STALL)
    assert proto.triggered == [0] * P.UPLOAD_RETRIES, 'asked for again'
    stall(proto, clock, P.UPLOAD_STALL)
    [failed] = proto.failed
    assert 'waiting for its lock' in failed.message


def test_the_lock_held_by_another_client_waits_longer_but_not_for_ever(proto, clock):
    proto.transfer = None
    proto.locks = {0: False}
    proto.transfer_requested = False
    proto._foreign_held = {0}
    stall(proto, clock, P.UPLOAD_STALL * 5)
    assert proto.triggered == [] and proto.failed == []
    clock[0] += P.LOCK_WAIT_MAX
    proto._service_upload_stall()
    assert proto.triggered == [0]


def test_a_download_is_left_to_its_caller(proto, clock):
    proto.transfer.upload = False
    stall(proto, clock, P.UPLOAD_STALL * 5)
    assert proto.released == [] and proto.failed == []
