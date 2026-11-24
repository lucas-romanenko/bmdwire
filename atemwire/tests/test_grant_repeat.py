# SPDX-License-Identifier: LGPL-3.0-only
"""A budget (FTCD) that comes again before the last one has left this
process is the same budget, not a new one.

The hardware bench, 2026-11-24 (Constellation HD, fw 2.30): three grants
of 768 chunks for one transfer, 42 ms apart, just before a dropout; all
three were counted, the switcher was sent chunks it had not granted, and
after the dropout it granted nothing more. A grant can only follow
chunks the switcher has: while chunks of the last budget are still
waiting here, it cannot have them.
"""
import struct

import pytest

from atemwire.protocol import AtemProtocol
from atemwire.transport import Packet, UdpProtocol


@pytest.fixture
def proto():
    p = AtemProtocol(ip='127.0.0.1')
    p.transport.sock.close()
    p.packets = []
    p.send_commands = lambda commands: p.packets.append(list(commands))
    p.chunks = []
    p.transport._send_packet = p.chunks.append
    p.initialized = True
    p.connected = True
    p.upload(0, 4, bytes(1392 * 20), compress=False, name='x')
    p.save_field_data(b'LKOB', struct.pack('>Hxx', 0))
    try:
        yield p
    finally:
        p.transport.thread_queue.close()


def waiting(p):
    return sum(1 for item in list(p.transport.thread_queue.queue) if item is not None)


def budget(p, count):
    p.save_field_data(b'FTCD', struct.pack('>H4xHH2x', p.transfer.tid, 1396, count))


def test_a_budget_again_before_the_last_left_is_not_counted(proto):
    proto.transport._send_packet = lambda pkt: proto.transport.thread_queue.put(pkt)  # still waiting
    budget(proto, 4)
    assert waiting(proto) == 4
    budget(proto, 4)                             # the same budget, again
    assert waiting(proto) == 4, 'no chunk it did not grant'
    assert proto.transfer.offset == 4 * 1392


def test_a_budget_after_the_last_left_is_a_new_one(proto):
    budget(proto, 4)
    budget(proto, 4)
    assert len(proto.chunks) == 8 and proto.transfer.offset == 8 * 1392


def test_a_wake_up_marker_is_not_a_chunk_waiting(proto):
    budget(proto, 4)
    proto.transport.thread_queue.put(None)       # wake_at's marker
    budget(proto, 4)
    assert len(proto.chunks) == 8
