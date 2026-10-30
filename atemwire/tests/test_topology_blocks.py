# SPDX-License-Identifier: LGPL-3.0-only
"""The DVE, stinger and SuperSource block counts in ``_top``, and when they
are unknown.

``_top`` moved its block counts once. Protocol 2.30 (firmware 8.1.1) put a
multiviewer count at offset 6, so the DVE byte went from offset 8 to 9.
atem-connection's TopologyCommand reads it that way, and needs at least 18
bytes before 2.30.

The 2.30 payloads below are real: one captured from a 1 M/E Constellation HD
(_ver 2.30), whose spec sheet lists 1 DVE, 1 stinger and no SuperSource, and
seven from the table in upstream pyatem's TopologyField docstring. The
pre-2.30 payloads are built from atem-connection's offsets; there is no
capture of one.

A count is None, never 0, when it can't be known: a frontend greys the
controls a 0 says the switcher lacks, so a wrong 0 is worse than no answer.
"""
import logging
import struct
from types import SimpleNamespace

import pytest

from atemwire.messages.system_info import (
    TOPOLOGY_LAYOUTS, FirmwareVersionField, TopologyField,
    topology_block_counts, topology_diagnostic,
)
from atemwire.protocol import AtemProtocol
from atemwire.state import build_full_state
from atemwire.testing import FakeAtemProtocol
from atemwire.transport import Packet, UdpProtocol

# 1 M/E Constellation HD, _ver 2.30 (2026-10-30).
CONSTELLATION_HD = bytes.fromhex(
    '01 20 01 06 06 02 01 00 0a 01 01 00 00 02 0a 01 00 00 01 00 00 01 01 01 00 00 00 00')
UNKNOWN = {'dves': None, 'stingers': None, 'superSources': None}


def _ver(major, minor):
    return FirmwareVersionField(struct.pack('>HH', major, minor))


def _mx(raw, version=(2, 30)):
    mx = {'topology': TopologyField(raw)}
    if version is not None:
        mx['firmware-version'] = _ver(*version)
    return mx


def _old(length):
    """A pre-2.30 payload: me 2, sources 30, dsk 2, aux 6, mix-minus 0,
    media players 2, serial ports 1, HyperDecks 4, DVE 3, stinger 2,
    SuperSource 1 (distinct, so a read one byte off can't pass)."""
    return bytes([2, 30, 2, 6, 0, 2, 1, 4, 3, 2, 1]).ljust(length, b'\0')


# --- the layouts --------------------------------------------------------------

def test_the_constellation_hd_capture():
    mx = _mx(CONSTELLATION_HD)
    assert topology_block_counts(mx) == {'dves': 1, 'stingers': 1, 'superSources': 0}
    top = mx['topology']           # the class's 2.30 offsets agree on this unit
    assert (top.multiviewers, top.hyperdecks, top.dve, top.stingers, top.supersources) == (1, 10, 1, 1, 0)


# Upstream pyatem's TopologyField table, under its own column labels: the
# 28-byte payload per model, and the DVE, stinger and SuperSource counts at 9,
# 10 and 11. "Prod 4k" (a Production Studio 4K, by the table) reports no DVE.
UPSTREAM_PYATEM_TABLE = {
    'Atem Mini': ([1, 14, 1, 1, 0, 1, 0, 0, 4, 1, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 1, 1,
                   0x20, 3, 0xe8], (1, 0, 0)),
    'Mini Pro': ([1, 15, 1, 1, 0, 1, 1, 0, 4, 1, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 1, 1,
                       0x2f, 108, 0x69], (1, 0, 0)),
    '1M/E 4k': ([1, 31, 2, 3, 0, 2, 1, 1, 4, 1, 1, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1,
                                    1, 0, 0, 0, 0x20, 0, 0], (1, 1, 0)),
    'Prod 4k': ([1, 24, 2, 1, 0, 2, 1, 0, 4, 0, 0, 0, 1, 0, 0, 0, 0, 1, 1, 0, 0, 1, 0,
                              0, 0, 0, 0, 0], (0, 0, 0)),
    'TVS HD': ([1, 24, 2, 1, 4, 2, 1, 1, 4, 1, 0, 0, 1, 1, 4, 0, 0, 0, 1, 1, 1, 1, 0,
                              0, 0, 0x10, 0, 0], (1, 0, 0)),
    'CHD 2ME': ([2, 57, 2, 12, 12, 2, 2, 0, 4, 1, 1, 1, 0, 2, 20, 1, 0, 0, 1, 0, 0, 1,
                                1, 1, 0, 0, 0, 0], (1, 1, 1)),
    'TVS4K8': ([1, 35, 2, 10, 10, 2, 1, 1, 10, 1, 1, 1, 0, 2, 8, 1, 0, 0, 1, 1, 1, 1,
                               1, 0, 0, 0, 0, 0], (1, 1, 1)),
}


@pytest.mark.parametrize('model', sorted(UPSTREAM_PYATEM_TABLE))
def test_the_models_in_upstream_pyatems_table(model):
    payload, (dves, stingers, supersources) = UPSTREAM_PYATEM_TABLE[model]
    assert len(payload) == 28
    assert topology_block_counts(_mx(bytes(payload))) == {
        'dves': dves, 'stingers': stingers, 'superSources': supersources}


@pytest.mark.parametrize('version, length', [
    ((2, 22), 18), ((2, 27), 20), ((2, 28), 24), ((2, 29), 24),
])
def test_before_2_30_the_counts_are_one_byte_earlier(version, length):
    mx = _mx(_old(length), version)
    assert topology_block_counts(mx) == {'dves': 3, 'stingers': 2, 'superSources': 1}
    # The class reads every layout at the 2.30 offsets, one byte late here.
    top = mx['topology']
    assert (top.hyperdecks, top.dve, top.stingers) == (3, 2, 1)


@pytest.mark.parametrize('version, length', [((2, 30), 28), ((2, 32), 28), ((2, 30), 32)])
def test_later_versions_and_longer_payloads_keep_the_2_30_layout(version, length):
    raw = CONSTELLATION_HD.ljust(length, b'\0')
    assert topology_block_counts(_mx(raw, version)) == {'dves': 1, 'stingers': 1, 'superSources': 0}


def test_a_zero_is_a_count():
    # A Mini-shaped switcher: one DVE, no stinger, no SuperSource.
    raw = bytes([1, 24, 1, 1, 0, 1, 1, 0, 0, 1, 0, 0]).ljust(28, b'\0')
    assert topology_block_counts(_mx(raw)) == {'dves': 1, 'stingers': 0, 'superSources': 0}
    assert topology_block_counts(_mx(bytes(28))) == {'dves': 0, 'stingers': 0, 'superSources': 0}


def test_the_layout_table_is_newest_first():
    versions = [since for since, _length, _offset in TOPOLOGY_LAYOUTS]
    assert versions == sorted(versions, reverse=True)


# --- unknown ------------------------------------------------------------------

@pytest.mark.parametrize('mx, why', [
    ({}, 'no _top'),
    ({'firmware-version': _ver(2, 30)}, 'no _top'),
    (_mx(CONSTELLATION_HD, version=None), 'no _ver'),
    ({'topology': TopologyField(CONSTELLATION_HD), 'firmware-version': SimpleNamespace()}, 'no _ver'),
    ({'topology': SimpleNamespace(dve=1, stingers=1, supersources=0),
      'firmware-version': _ver(2, 30)}, 'no _top payload'),
    (_mx(_old(20), (2, 21)), 'protocol 2.21 is older than any known _top layout'),
    (_mx(CONSTELLATION_HD, (3, 0)), 'protocol 3.0 is not 2.x'),
    (_mx(CONSTELLATION_HD[:24], (2, 30)), '24 bytes, the 2.30 layout has 28'),
    (_mx(_old(16), (2, 29)), '16 bytes, the 2.22 layout has 18'),
], ids=['nothing', 'no-top', 'no-ver', 'ver-without-numbers', 'top-without-bytes',
        'too-old', 'protocol-3', 'short-2.30', 'short-2.29'])
def test_unknown_is_none_never_zero(mx, why):
    assert topology_block_counts(mx) == UNKNOWN
    assert topology_diagnostic(mx).endswith(f'block counts unknown ({why})')


# --- the snapshot -------------------------------------------------------------

def _fake(mixerstate):
    fake = FakeAtemProtocol('192.0.2.10')
    fake.connect()
    fake.last_run_macro_index = -1
    fake.mixerstate.update(mixerstate)
    return fake


def test_build_full_state_carries_the_counts():
    topo = build_full_state(_fake(_mx(CONSTELLATION_HD)))['topology']
    assert (topo['dves'], topo['stingers'], topo['superSources']) == (1, 1, 0)


def test_build_full_state_says_unknown_without_ver():
    topo = build_full_state(_fake(_mx(CONSTELLATION_HD, version=None)))['topology']
    assert {k: topo[k] for k in UNKNOWN} == UNKNOWN
    assert topo['meCount'] == 1       # the rest of _top is still read


# --- the log line at connect --------------------------------------------------

@pytest.fixture
def proto():
    p = AtemProtocol('127.0.0.1')
    p.send_commands = lambda commands: None     # nothing reaches the wire
    try:
        yield p
    finally:
        p.transport.sock.close()
        p.transport.thread_queue.close()


def _field(code, payload):
    return struct.pack('>H2x4s', 8 + len(payload), code) + payload


def _packet(*fields):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.session = 0x8001
    pkt.data = b''.join(fields)
    return Packet.from_bytes(pkt.to_bytes())


def _connect_with(proto, *fields):
    """A state dump of ``fields`` (plus a video mode and InCm), then the
    packet after it, which makes the transport report the session ready."""
    t = proto.transport
    t.state = UdpProtocol.STATE_ESTABLISHED
    t.enable_ack = True
    t.had_traffic = True
    events = []
    proto.on('connected', lambda: events.append('connected'))
    t.thread_recv_queue.put(_packet(*fields, _field(b'VidM', bytes([10, 0, 0, 0])),
                                    _field(b'InCm', b'\x01\x00\x00\x00')))
    t.thread_recv_queue.put(_packet(_field(b'Time', bytes(8))))
    for _ in range(3):
        if t.thread_recv_queue.empty() and t._stashed_packet is None:
            break
        proto.loop()
    assert events == ['connected']


def _topology_lines(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records
            if r.getMessage().startswith('topology at connect')]


def test_the_counts_are_logged_at_connect(proto, caplog):
    with caplog.at_level(logging.INFO, logger='AtemProtocol'):
        _connect_with(proto, _field(b'_ver', struct.pack('>HH', 2, 30)),
                      _field(b'_top', CONSTELLATION_HD))
    assert _topology_lines(caplog) == [(
        logging.INFO,
        'topology at connect: protocol 2.30, _top 28 bytes: '
        + CONSTELLATION_HD.hex()
        + ' -> dves=1 stingers=1 superSources=0 (DVE at offset 9)')]


def test_unknown_counts_are_logged_with_the_reason(proto, caplog):
    with caplog.at_level(logging.INFO, logger='AtemProtocol'):
        _connect_with(proto, _field(b'_top', CONSTELLATION_HD))
    assert _topology_lines(caplog) == [(
        logging.INFO,
        'topology at connect: protocol ?, _top 28 bytes: '
        + CONSTELLATION_HD.hex() + ' -> block counts unknown (no _ver)')]


def test_a_failing_description_never_costs_the_connect(proto, caplog, monkeypatch):
    def boom(mx):
        raise RuntimeError('no')
    monkeypatch.setattr('atemwire.protocol.topology_diagnostic', boom)
    with caplog.at_level(logging.INFO, logger='AtemProtocol'):
        _connect_with(proto, _field(b'_top', CONSTELLATION_HD))     # asserts 'connected'
    assert _topology_lines(caplog) == []
