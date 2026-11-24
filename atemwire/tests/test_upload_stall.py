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
    p.transport = types.SimpleNamespace(_acked_through=50, local_sequence_number=50, wakes=[], reconnects=[])
    p.transport.wake_at = p.transport.wakes.append
    p.transport.reconnect = p.transport.reconnects.append
    p.initialized = True
    p._carried_uploads = []
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


def test_no_progress_for_upload_stall_starts_it_again_in_a_new_session(proto, clock):
    stall(proto, clock, P.UPLOAD_STALL - 0.01)
    assert proto.transport.reconnects == [] and proto.transport.wakes, 'waits, and wakes to look again'
    clock[0] += 0.01
    proto._service_upload_stall()
    [why] = proto.transport.reconnects
    assert 'stalled' in why and '4000 of 10000 bytes cut' in why
    assert ('upload-retrying', 0, 3, 1, P.UPLOAD_RETRIES) in proto.events
    assert proto.released == [], 'not the same session: there the lock got no answer'


def test_whatever_moves_while_its_data_goes_is_progress(proto, clock):
    proto._service_upload_stall()
    for step in range(10):
        clock[0] += P.UPLOAD_STALL - 1
        [lambda: setattr(proto.transport, '_acked_through', proto.transport._acked_through + 1),
         lambda: setattr(proto.transfer, 'offset', proto.transfer.offset + 1396),
         lambda: setattr(proto, '_grants', proto._grants + 1)][step % 3]()
        proto._service_upload_stall()
    assert proto.transport.reconnects == [] and proto.failed == []


def test_our_requests_acknowledged_while_it_waits_for_its_lock_are_not_progress(proto, clock):
    # The hardware bench, 15:00 to 15:55: started again, the lock asked for
    # and acknowledged, never granted; the acknowledgements kept the stall
    # timer from ever running out, no second retry, no error.
    proto.transfer = None
    proto.locks = {0: False}
    proto.transfer_requested = False
    proto._service_upload_stall()
    for _ in range(int(P.UPLOAD_STALL)):
        clock[0] += 1
        proto.transport._acked_through += 1      # our lock request, again
        proto._service_upload_stall()
    assert len(proto.transport.reconnects) == 1
    assert 'waiting for its lock' in proto.transport.reconnects[0]


def test_after_upload_retries_it_fails_with_the_reason(proto, clock):
    for _ in range(P.UPLOAD_RETRIES):
        stall(proto, clock, P.UPLOAD_STALL)
    stall(proto, clock, P.UPLOAD_STALL)
    assert len(proto.transport.reconnects) == P.UPLOAD_RETRIES
    [failed] = proto.failed
    assert failed.status == 'stalled' and failed.transfer == 7 and 'fresh starts' in failed.message
    assert proto.transfer_queue[0] == [], 'off its queue'


def test_the_lock_held_by_another_client_waits_longer_but_not_for_ever(proto, clock):
    proto.transfer = None
    proto.locks = {0: False}
    proto.transfer_requested = False
    proto._foreign_held = {0}
    stall(proto, clock, P.UPLOAD_STALL * 5)
    assert proto.transport.reconnects == [] and proto.failed == []
    clock[0] += P.LOCK_WAIT_MAX
    proto._service_upload_stall()
    assert len(proto.transport.reconnects) == 1


def test_uploads_are_carried_over_a_lost_session_and_asked_for_again(proto, clock):
    proto.transfer.metadata_sent = True
    other = TransferTask(0, 4, upload=True)
    proto.transfer_queue[0].append(other)
    proto._carry_uploads_over()
    assert proto._carried_uploads == [proto.transfer, other]
    assert proto.transfer.offset == 0 and proto.transfer.tid is None and not proto.transfer.metadata_sent
    proto.transfer_queue = {}                    # the lane reset
    proto.transfer = None
    proto._requeue_carried_uploads()
    assert [t.slot for t in proto.transfer_queue[0]] == [3, 4] and proto.triggered == [0]


def test_a_session_that_never_comes_back_fails_the_upload_in_the_end(proto, clock):
    proto._carry_uploads_over()
    proto.transfer_queue = {}
    proto.transfer = None
    proto.initialized = False
    for _ in range(P.UPLOAD_RETRIES + 1):
        stall(proto, clock, P.UPLOAD_STALL)
    [failed] = proto.failed
    assert 'waiting for a new session' in failed.message and proto._carried_uploads == []


def test_a_download_is_left_to_its_caller(proto, clock):
    proto.transfer.upload = False
    stall(proto, clock, P.UPLOAD_STALL * 5)
    assert proto.transport.reconnects == [] and proto.failed == []
