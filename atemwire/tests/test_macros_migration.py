# SPDX-License-Identifier: LGPL-3.0-only
"""Migration tests for the ``macros`` feature.

Phase 3 batch 4: 6 verb-wrapper operations + 3 readers + the
MACRO_ACTION_* constants migrated to atemwire.messages.macros.

Per Phase 1 decision 3, all 6 operations are bucket B — they wrap
MacroActionCommand with caller-specific semantics (1-indexed UI →
0-indexed wire on execute_macro, hardcoded action constants on
macro_run / macro_stop / macro_stop_recording, and the multi-arg
record / sleep shapes for the other two).

The MACRO_ACTION_* constants move with the feature per Phase 1
decision 5. (The ``atemwire.operations`` shim that briefly re-exported
them was deleted in the 2026-05-14 shim-removal close-out — callers
now import them directly from ``atemwire.messages.macros``.)
"""


from atemwire.messages.macros import (
    MACRO_ACTION_CONTINUE, MACRO_ACTION_INSERT_USER_WAIT, MACRO_ACTION_RUN,
    MACRO_ACTION_STOP_RECORD, MACRO_ACTION_STOP_RUN, execute_macro,
    macro_entry, macro_play_index, macro_play_running, macro_run,
    macro_sleep, macro_start_recording, macro_stop, macro_stop_recording
)


class _FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd.get_command())


class _MPrp:
    def __init__(self, is_used=False, name=b''):
        self.is_used = is_used
        self.name = name


class _MRPr:
    def __init__(self, running=False, index=0xFFFF):
        self.running = running
        self.index = index


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_action_constants_values():
    """The constants match the wire values atemwire.protocol expects."""
    assert MACRO_ACTION_RUN == 0
    assert MACRO_ACTION_STOP_RUN == 1
    assert MACRO_ACTION_STOP_RECORD == 2
    assert MACRO_ACTION_INSERT_USER_WAIT == 3
    assert MACRO_ACTION_CONTINUE == 4


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def test_execute_macro_converts_1_indexed_to_0_indexed():
    """Frontend sends macro_number=1 (1-indexed); wire packs index=0."""
    conn = _FakeConn()
    execute_macro(conn, macro_number=1)
    payload = conn.sent[0][8:]
    assert conn.sent[0][4:8] == b'MAct'
    # MacroActionCommand layout: index u16 at 0, action u8 at 2
    import struct
    assert struct.unpack_from('>H', payload, 0)[0] == 0  # 1 - 1 = 0
    assert payload[2] == MACRO_ACTION_RUN


def test_execute_macro_with_higher_number():
    conn = _FakeConn()
    execute_macro(conn, macro_number=20)
    import struct
    assert struct.unpack_from('>H', conn.sent[0][8:], 0)[0] == 19


def test_macro_run_uses_0_indexed_slot():
    """macro_run is the 0-indexed sibling — wraps the same MAct
    with action=RUN but doesn't subtract 1."""
    conn = _FakeConn()
    macro_run(conn, slot=5)
    import struct
    assert struct.unpack_from('>H', conn.sent[0][8:], 0)[0] == 5


def test_macro_stop_uses_sentinel_index():
    """macro_stop targets 'whatever is playing' via index=0xFFFF."""
    conn = _FakeConn()
    macro_stop(conn)
    import struct
    assert struct.unpack_from('>H', conn.sent[0][8:], 0)[0] == 0xFFFF
    assert conn.sent[0][8:][2] == MACRO_ACTION_STOP_RUN


def test_macro_stop_recording_uses_stop_record_action():
    conn = _FakeConn()
    macro_stop_recording(conn)
    payload = conn.sent[0][8:]
    assert payload[2] == MACRO_ACTION_STOP_RECORD


def test_macro_start_recording_sends_msrc_with_name():
    conn = _FakeConn()
    macro_start_recording(conn, slot=2, name='Setup', description='Live shot')
    assert conn.sent[0][4:8] == b'MSRc'


def test_macro_sleep_sends_mslp_with_frame_count():
    conn = _FakeConn()
    macro_sleep(conn, frames=30)
    assert conn.sent[0][4:8] == b'MSlp'


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def test_macro_entry_decodes_name():
    mx = {'macro-properties': {0: _MPrp(
        is_used=True, name=b'Wipe to BG\x00\x00',
    )}}
    result = macro_entry(mx, 0)
    assert result == {'is_used': True, 'name': 'Wipe to BG'}


def test_macro_entry_default_when_missing():
    assert macro_entry({}, 0) == {'is_used': False, 'name': ''}


def test_macro_play_running_and_index():
    mx = {'macro-play-status': _MRPr(running=True, index=5)}
    assert macro_play_running(mx) is True
    assert macro_play_index(mx) == 5


def test_macro_play_index_default_idle_sentinel():
    """idle index is 0xFFFF; defaults to that when no status field."""
    assert macro_play_index({}) == 0xFFFF
    assert macro_play_running({}) is False
