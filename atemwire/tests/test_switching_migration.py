# SPDX-License-Identifier: LGPL-3.0-only
"""Migration tests for the ``switching`` feature.

Phase 3 commit 1 (proof-of-concept): operations + readers for the
switching feature moved from ``atemwire.operations`` / ``atemwire.state``
(both deleted in the 2026-05-14 shim-removal close-out) into
``atemwire.messages.switching``.

This file covers:

  - Each wrapper emits the right wire bytes for the corresponding
    Send class (Send → wire round-trip).
  - Each reader navigates mixerstate to the right field (mixerstate
    → display round-trip).

Switching has no scaled fields — every wire field is identity (struct
pack/unpack of an int). That's why this commit is the
proof-of-concept: no scale= work, only the move plumbing. Later
commits with scaled fields add round-trip tests for those.

The +8 header offset: ``Send.get_command()`` returns 8-byte header
+ payload. Tests read offsets relative to the payload (raw[8:]).
"""

import struct


from atemwire.messages.switching import (
    aux_source, auto, cut, preview_source, program_source, set_aux_output,
    set_preview, set_program
)


# ---------------------------------------------------------------------------
# Stand-in for an ATEM connection — captures the bytes a wrapper emits.
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd.get_command())


# ---------------------------------------------------------------------------
# Wrappers — Send → wire
# ---------------------------------------------------------------------------

def test_set_program_emits_program_input_command():
    """set_program(conn, source=5) → wire bytes for CPgI me=0 source=5."""
    conn = _FakeConn()
    set_program(conn, source=5)
    assert len(conn.sent) == 1
    payload = conn.sent[0][8:]
    assert payload[0] == 0                                  # me index
    assert struct.unpack_from('>H', payload, 2)[0] == 5     # source


def test_set_preview_emits_preview_input_command():
    conn = _FakeConn()
    set_preview(conn, source=9, me=1)
    payload = conn.sent[0][8:]
    assert payload[0] == 1
    assert struct.unpack_from('>H', payload, 2)[0] == 9


def test_cut_and_auto_emit_correct_packets():
    conn = _FakeConn()
    cut(conn)
    auto(conn)
    assert conn.sent[0][4:8] == b'DCut'
    assert conn.sent[1][4:8] == b'DAut'


def test_set_aux_output_includes_mask_byte():
    """AuxSourceCommand has a fixed mask byte of 1 at offset 0 — the
    only Send in switching that uses the mask byte."""
    conn = _FakeConn()
    set_aux_output(conn, aux=2, source=42)
    payload = conn.sent[0][8:]
    assert payload[0] == 1                                  # mask (always 1)
    assert payload[1] == 2                                  # aux index
    assert struct.unpack_from('>H', payload, 2)[0] == 42    # source


def test_wrappers_coerce_json_inputs_to_int():
    """JSON fields arrive as Python ints already, but the dispatch
    table sometimes passes strings (e.g. when WebSocket payloads stash
    numbers as strings). The wrappers wrap inputs in ``int()`` so the
    Send class gets the right type for ``struct.pack``."""
    conn = _FakeConn()
    set_program(conn, source='7')
    assert struct.unpack_from('>H', conn.sent[0][8:], 2)[0] == 7


# ---------------------------------------------------------------------------
# Readers — mixerstate → display
# ---------------------------------------------------------------------------

class _PrgI:
    """Stand-in for ProgramBusInputField. The reader looks up
    ``.source`` on the entry."""
    def __init__(self, source):
        self.source = source


def test_program_source_reads_from_mixerstate():
    mx = {'program-bus-input': {0: _PrgI(3), 1: _PrgI(7)}}
    assert program_source(mx, 0) == 3
    assert program_source(mx, 1) == 7


def test_preview_source_reads_from_mixerstate():
    mx = {'preview-bus-input': {0: _PrgI(11)}}
    assert preview_source(mx, 0) == 11


def test_aux_source_reads_from_mixerstate():
    mx = {'aux-output-source': {0: _PrgI(101), 1: _PrgI(102), 2: _PrgI(103)}}
    assert aux_source(mx, 0) == 101
    assert aux_source(mx, 2) == 103


def test_readers_default_zero_when_mixerstate_empty():
    """Handshake window: ``mx`` is empty, readers must not raise.
    Match the prior ``safe_int(_kv(...), 0)`` behavior."""
    assert program_source({}) == 0
    assert preview_source({}) == 0
    assert aux_source({}, 0) == 0


def test_readers_default_zero_when_index_missing():
    """Entry exists but the requested me/aux index isn't populated."""
    mx = {'program-bus-input': {0: _PrgI(5)}}
    assert program_source(mx, me=2) == 0   # me=2 not in dict
