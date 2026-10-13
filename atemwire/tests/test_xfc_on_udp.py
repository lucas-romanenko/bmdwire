# SPDX-License-Identifier: LGPL-3.0-only
"""``*XFC`` on a UDP session is ignored (bmdwire#14).

``*XFC`` (``TransferCompleteField``) is the OpenSwitcher TCP relay's
transfer-complete event; no ATEM sends it over UDP, and the ``tcp://``
transport isn't shipped. Its handler indexed ``transfer_queue[store]``
anyway. With nothing queued for the store that raised ``KeyError``, which
escaped ``loop()`` and lost every field after it in the datagram; with a
transfer queued it popped that transfer and could raise a spurious
``upload-done``. A short ``*XFC`` raised in the parser before the handler.

These put the datagram on the real transport's receive queue and pump
``loop()`` once.
"""
import struct

import pytest

from atemwire._transfer import TransferTask
from atemwire.protocol import AtemProtocol
from atemwire.transport import Packet, UdpProtocol


def _field(code, payload):
    return struct.pack('>H2x4s', 8 + len(payload), code) + payload


XFC = _field(b'*XFC', struct.pack('>HH?3x', 0, 3, True))
SHORT_XFC = _field(b'*XFC', b'\x00\x00')
PRGI = _field(b'PrgI', struct.pack('>BxH', 0, 5))


@pytest.fixture
def proto():
    p = AtemProtocol('127.0.0.1')
    t = p.transport
    t.state = UdpProtocol.STATE_ESTABLISHED
    t.enable_ack = True
    t.had_traffic = True
    p.uploads_done = []
    p.on('upload-done', lambda store, slot: p.uploads_done.append((store, slot)))
    try:
        yield p
    finally:
        t.sock.close()
        t.thread_queue.close()


def _deliver(p, *fields):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.session = 0x8001
    pkt.data = b''.join(fields)
    p.transport.thread_recv_queue.put(Packet.from_bytes(pkt.to_bytes()))
    p.loop()


@pytest.mark.parametrize('xfc', [XFC, SHORT_XFC], ids=['xfc', 'short-xfc'])
def test_the_rest_of_the_packet_is_kept(proto, xfc):
    _deliver(proto, xfc, PRGI)
    assert proto.mixerstate['program-bus-input'][0].source == 5, (
        'the fields after *XFC in the same datagram must still be stored'
    )
    assert proto.uploads_done == []
    assert proto.transfer_queue == {}
    assert '*XFC' not in proto.mixerstate
    assert 'transfer-complete' not in proto.mixerstate


def test_a_queued_transfer_is_left_alone(proto):
    task = TransferTask(0, 3, upload=True)
    proto.transfer_queue[0] = [task]
    _deliver(proto, XFC, PRGI)
    assert proto.transfer_queue[0] == [task], '*XFC must not pop a real transfer'
    assert proto.uploads_done == [], 'nor raise upload-done for it'
    assert proto.mixerstate['program-bus-input'][0].source == 5


def test_save_field_data_does_not_raise(proto):
    # The issue's direct form.
    proto.save_field_data(b'*XFC', struct.pack('>HH?3x', 0, 3, True))
    proto.save_field_data(b'PrgI', struct.pack('>BxH', 0, 5))
    assert proto.transfer_queue == {}
    assert proto.mixerstate['program-bus-input'][0].source == 5


def test_the_tcp_branch_tolerates_an_empty_queue(proto):
    # The TCP-relay path still handles *XFC, but without a KeyError when
    # nothing is queued for the store. _is_tcp_transport goes by class name.
    TcpProtocol = type('TcpProtocol', (), {})
    real = proto.transport
    proto.transport = TcpProtocol()
    try:
        proto.save_field_data(b'*XFC', struct.pack('>HH?3x', 0, 3, True))
    finally:
        proto.transport = real
    assert proto.uploads_done == [(0, 3)]
    assert proto.transfer_queue == {0: []}
