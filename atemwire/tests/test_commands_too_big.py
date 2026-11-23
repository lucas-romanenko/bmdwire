# SPDX-License-Identifier: LGPL-3.0-only
"""Command packets too big for the path go again split (the network
matrix's labels and profile_restore over a path MTU of 1,200 or 576).

A packet of commands can carry up to 1,300 bytes of them (20 labels, a
profile restore). Over a path that drops datagrams that big it never got
through, and the switcher, which takes our packets in order only, took
nothing after it either: 20 labels at MTU 576 never arrived, and a profile
restore over a VPN at MTU 1,200 sat out its macro upload's timeout with
nothing restored.
"""
import struct

import pytest

from atemwire import transport as T
from atemwire.messages import TransferDataCommand
from atemwire.messages.input_video import InputPropertiesCommand
from atemwire.tests.test_chunks_too_big import (  # noqa: F401  (fixtures)
    a_stuck_upload, chunk, clock, limits, reliable, taken_back, time_packet,
    timer_fires, udp)
from atemwire.transport import Packet, UdpProtocol, _commands, _split_commands


def labels(n=20):
    """One packet of ``n`` label commands, 40 bytes each, as
    send_commands packs them."""
    return reliable(b''.join(InputPropertiesCommand(i, label='Input %d' % i, short_label='I%d' % i)
                             .get_command() for i in range(1, n + 1)))


def a_stuck_command_packet(udp, clock, pkt):
    time_packet(udp)
    udp._send_packet_low(reliable(b'\x00\x08TEST'))
    clock[0] += 0.08
    udp._on_ack(1, ack_only=True)
    udp._send_packet_low(pkt)


def reliable_sent(udp):
    return [q for q in (Packet.from_bytes(r) for r in udp.sock.sent)
            if q.flags & UdpProtocol.FLAG_RELIABLE]


def test_a_stuck_packet_of_commands_goes_again_split(udp, clock, limits):
    pkt = labels(30)                              # 1,200 bytes of commands
    a_stuck_command_packet(udp, clock, pkt)
    udp.sock.sent.clear()
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER + 1)
    assert taken_back(udp) == [], 'no upload among them'
    again = [q for q in reliable_sent(udp) if q.sequence_number >= 2
             and not q.flags & UdpProtocol.FLAG_RETRANSMISSION]
    assert len(again) == 2
    assert [q.sequence_number for q in again] == [2, 3], 'from where it stood, in order'
    assert all(len(q.data) <= 952 + UdpProtocol.COMMAND_ROOM for q in again)
    assert b''.join(bytes(q.data) for q in again) == bytes(pkt.data), 'every command, in order'
    assert udp.max_chunk == 952 and limits == {'127.0.0.1': 952}


def test_once_the_size_is_known_command_packets_are_split_as_they_go(udp, clock):
    udp.max_chunk = 952
    udp.sock.sent.clear()
    pkt = labels(30)
    udp._send_on_udp_thread(pkt)
    out = reliable_sent(udp)
    assert len(out) == 2 and all(len(q.data) <= 964 for q in out)
    assert b''.join(bytes(q.data) for q in out) == bytes(pkt.data)


def test_a_packet_of_one_command_is_left_alone(udp, clock):
    # Nothing to split it into: resent as it is, the size unchanged.
    a_stuck_command_packet(udp, clock, reliable(b'\x05\x00\x00\x00CBIG' + bytes(1272)))
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER + 2)
    assert udp.max_chunk is None


def test_a_quiet_link_is_not_a_small_path(udp, clock):
    a_stuck_command_packet(udp, clock, labels(30))
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER + 2, answering=False)
    assert udp.max_chunk is None


def test_chunks_behind_a_stuck_command_packet_go_back_to_their_upload(udp, clock):
    a_stuck_command_packet(udp, clock, labels(30))
    for n in range(3):
        udp._send_packet_low(chunk(n))
    timer_fires(udp, clock, UdpProtocol.TOO_BIG_AFTER + 1)
    [taken] = taken_back(udp)
    assert (taken.transfer, taken.sizes, taken.limit) == (7, [1392] * 3, 952)


def test_commands_split_only_between_commands():
    pkt = labels(5)
    assert _commands(pkt.data) is not None and len(_commands(pkt.data)) == 5
    parts = _split_commands(pkt, 100)
    assert [len(q.data) for q in parts] == [80, 80, 40]
    assert _split_commands(reliable(b'\x00\x08TEST'), 4)[0].data == b'\x00\x08TEST'
    assert _commands(b'\x00\x09TEST') is None, 'a length running past the end'
