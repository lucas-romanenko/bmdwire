# SPDX-License-Identifier: LGPL-3.0-only
"""Migration tests for the ``input_video`` feature.

Phase 3 batch 4: 2 operations + 3 readers + VIDEO_MODE_NAMES constant
migrated from atemwire.operations / atemwire.state to
atemwire.messages.input_video.

Ops:
  set_input_label   (bucket B — multi-field optional-kwarg)
  set_video_mode    (bucket B — enum table lookup)

Readers (all bucket B aggregators):
  input_label       (2-field dict)
  all_input_sources (iterates input-properties → sorted list)
  renamable_inputs  (partitions by port_type into 3 buckets)

The VIDEO_MODE_NAMES table moves with the feature per Phase 1 decision
5. The macrotransfer importer is re-routed to atemwire.messages.input_video.
"""

import pytest

from atemwire.messages.input_video import (
    all_input_sources, input_label, renamable_inputs, set_input_label,
    set_video_mode
)


class _FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd.get_command())


class _InPr:
    def __init__(self, name=b'', short_name=b'', port_type=0,
                 available_aux=False, available_key_source=False,
                 available_multiview=False):
        self.name = name
        self.short_name = short_name
        self.port_type = port_type
        self.available_aux = available_aux
        self.available_key_source = available_key_source
        self.available_multiview = available_multiview


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def test_set_input_label_packs_cinl():
    conn = _FakeConn()
    set_input_label(conn, source=1, long_name='Cam One', short_name='C1')
    assert conn.sent[0][4:8] == b'CInL'


def test_set_input_label_with_only_short_name():
    """Optional kwargs: only short_name set → wire mask should reflect
    that only one field is being written."""
    conn = _FakeConn()
    set_input_label(conn, source=2, short_name='Rt')
    assert conn.sent[0][4:8] == b'CInL'


def test_set_video_mode_with_string_name():
    """String form looks up VIDEO_MODE_NAMES; '1080p60' → 27."""
    conn = _FakeConn()
    set_video_mode(conn, mode='1080p60')
    payload = conn.sent[0][8:]
    assert conn.sent[0][4:8] == b'CVdM'
    assert payload[0] == 27


def test_set_video_mode_with_integer():
    """Integer form bypasses the table — caller supplied the enum value."""
    conn = _FakeConn()
    set_video_mode(conn, mode=12)  # 1080p50
    payload = conn.sent[0][8:]
    assert payload[0] == 12


def test_set_video_mode_unknown_string_raises():
    conn = _FakeConn()
    with pytest.raises(ValueError, match='unknown video mode'):
        set_video_mode(conn, mode='not_a_mode')


def test_set_video_mode_aliases():
    """Multiple string names can map to the same wire value (e.g. NTSC
    vs 525i5994 both → 0)."""
    conn = _FakeConn()
    set_video_mode(conn, mode='NTSC')
    set_video_mode(conn, mode='525i5994')
    assert conn.sent[0][8] == 0
    assert conn.sent[1][8] == 0


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def test_input_label_decodes_bytes_to_str():
    """Bytes name/short_name → decoded UTF-8 strings."""
    mx = {'input-properties': {1: _InPr(
        name=b'Camera One\x00\x00\x00', short_name=b'CM1\x00',
    )}}
    result = input_label(mx, 1)
    assert result == {'long': 'Camera One', 'short': 'CM1'}


def test_input_label_default_when_missing():
    assert input_label({}, 1) == {'long': '', 'short': ''}


def test_all_input_sources_sorted_by_id():
    mx = {'input-properties': {
        3: _InPr(name=b'C3', port_type=0),
        1: _InPr(name=b'C1', port_type=0, available_aux=True),
        2: _InPr(name=b'C2', port_type=0),
    }}
    result = all_input_sources(mx)
    assert [r['source'] for r in result] == [1, 2, 3]
    assert result[0]['available_aux'] is True
    assert result[1]['long'] == 'C2'


def test_all_input_sources_empty_when_no_inputs():
    assert all_input_sources({}) == []


def test_renamable_inputs_partitions_by_port_type():
    """port_type 0 → inputs; 128/129/131 → outputs; 3/4/5 → media.
    Internal fixed sources (BLACK / BARS / KEY_MASK) excluded."""
    mx = {'input-properties': {
        1: _InPr(name=b'Cam1', port_type=0),
        2: _InPr(name=b'Cam2', port_type=0),
        2001: _InPr(name=b'Color1', port_type=3),  # COLOR gen
        3010: _InPr(name=b'MP1', port_type=4),     # MEDIAPLAYER
        10010: _InPr(name=b'AUX1', port_type=129),  # AUX_OUTPUT
        7001: _InPr(name=b'Pgm', port_type=128),    # ME_OUTPUT
        0: _InPr(name=b'Black', port_type=2),       # PORT_BLACK — excluded
    }}
    result = renamable_inputs(mx)
    assert [e['source'] for e in result['inputs']] == [1, 2]
    assert [e['source'] for e in result['outputs']] == [7001, 10010]
    assert [e['source'] for e in result['media']] == [2001, 3010]


def test_renamable_inputs_excludes_unrecognized_port_types():
    """A port_type that's neither 0 / 128 / 129 / 131 / 3 / 4 / 5 is
    silently excluded (PORT_BLACK = 2 is the canonical case)."""
    mx = {'input-properties': {
        99: _InPr(name=b'Unknown', port_type=255),
    }}
    result = renamable_inputs(mx)
    assert result == {'inputs': [], 'outputs': [], 'media': []}


def test_macrotransfer_reroutes_to_input_video_module():
    """macrotransfer's _e_video_mode encodes the videoMode attribute via
    VIDEO_MODE_NAMES. Verify the import re-route still works."""
    from atemwire.macrotransfer.switching import _e_video_mode
    # Encoder packs a 4-byte payload: [mode_int, 0, 0, 0].
    encoded = _e_video_mode({'videoMode': '1080p60'})
    assert encoded == bytes([27, 0, 0, 0])
