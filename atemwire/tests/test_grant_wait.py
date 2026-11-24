# SPDX-License-Identifier: LGPL-3.0-only
"""An upload the switcher stops granting budgets to is started again.

The hardware bench, 2026-11-24 (Constellation HD, fw 2.30, macOS): the
cable out for 3 s in the middle of a still upload. Afterwards everything
was acknowledged on both sides, the switcher answered every second, and
it never sent the next budget (FTCD): the still sat at 47 % until the
caller gave up, on both units. With every chunk of the last budget
acknowledged and nothing of ours outstanding, the upload now waits
GRANT_WAIT for a budget at most, then starts again from the beginning
under a new transfer (its lock given back and asked for again),
GRANT_RESTARTS times at most.
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
    task.metadata_sent = True
    p.transfer = task
    p.transfer_queue = {0: [task]}
    p.transfer_budget = None
    p.transfer_requested = True
    p.transfer_buffer = []
    p.transfer_buffer_bytes = 0
    p.locks = {0: True}
    p._grant_idle_since = None
    p.transport = types.SimpleNamespace(_acked_through=50, local_sequence_number=50,
                                        send_queue=[], _held=[], wakes=[])
    p.transport.wake_at = p.transport.wakes.append
    p.log = types.SimpleNamespace(warning=lambda *a: None, info=lambda *a: None, debug=lambda *a: None)
    p.events = []
    p._raise = lambda *a: p.events.append(a)
    p.released = []
    p._release_then_continue = lambda store, request_next=True: p.released.append(store)
    return p


def test_no_budget_for_grant_wait_with_everything_acknowledged_starts_it_again(proto, clock):
    proto._service_grant_wait()
    assert proto.released == [] and proto.transport.wakes, 'waits, and wakes to look again'
    clock[0] += P.GRANT_WAIT - 0.01
    proto._service_grant_wait()
    assert proto.released == []
    clock[0] += 0.01
    proto._service_grant_wait()
    assert proto.released == [0], 'its lock given back, the same task asked for again'
    assert proto.transfer_requested is False
    assert proto.transfer.grant_restarts == 1
    assert ('upload-restarted', 0, 3) in proto.events
    assert proto.transfer_queue[0] == [proto.transfer], 'still first in the queue'


def test_packets_of_ours_outstanding_are_not_a_stall(proto, clock):
    proto.transport.local_sequence_number = 60     # 10 not yet acknowledged
    for _ in range(5):
        proto._service_grant_wait()
        clock[0] += P.GRANT_WAIT
    assert proto.released == []


def test_a_budget_in_hand_is_not_a_stall(proto, clock):
    proto.transfer_budget = types.SimpleNamespace(count=10, size=1396)
    proto._service_grant_wait()
    clock[0] += P.GRANT_WAIT * 3
    proto._service_grant_wait()
    assert proto.released == []


def test_started_again_grant_restarts_times_at_most(proto, clock):
    for _ in range(P.GRANT_RESTARTS + 3):
        proto._service_grant_wait()
        clock[0] += P.GRANT_WAIT
        proto._service_grant_wait()
    assert len(proto.released) == P.GRANT_RESTARTS


def test_the_macro_store_and_a_finished_upload_are_left_alone(proto, clock):
    proto.transfer.offset = len(proto.transfer.data)  # all cut and sent
    proto._service_grant_wait()
    clock[0] += P.GRANT_WAIT * 2
    proto._service_grant_wait()
    assert proto.released == []
