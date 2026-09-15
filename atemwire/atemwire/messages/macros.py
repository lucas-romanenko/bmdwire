# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Macro messages — record / replay / sleep + state notifications.

Wire packets:
    MAct — outgoing, perform action on a macro slot (run / stop / record / etc.)
    MSRc — outgoing, begin recording into a slot. Variable-length payload
            (name + description) so this is hand-encoded.
    MSlp — outgoing, record a sleep step during a recording session
    MPrp — incoming, slot metadata (name, description, used, invalid). Variable
            length, hand-decoded.
    MRcS — incoming, record-status flag + active slot
    MRPr — incoming, live playback status (running / waiting / looping / index).
            Bit-packed flag bytes, hand-decoded.
"""

import struct

from atemwire.state import decode_name, safe_bool, safe_int
from atemwire.messages._dsl import Recv, Send, boolean, u8, u16, u32


# Action codes for ``MAct``. Moved here from atemwire.operations per
# Phase 1 decision 5; the operations.py shim re-exports them so
# existing callers (notably atemwire.macrotransfer) keep working.
MACRO_ACTION_RUN = 0
MACRO_ACTION_STOP_RUN = 1
MACRO_ACTION_STOP_RECORD = 2
MACRO_ACTION_INSERT_USER_WAIT = 3
MACRO_ACTION_CONTINUE = 4


# -----------------------------------------------------------------------------
# Outgoing — action / record / sleep
# -----------------------------------------------------------------------------


class MacroActionCommand(Send):
    """``MAct`` — perform a special action on a macro slot.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Macro slot index (0xFFFF when no slot)
    2      1    u8     Action
    3      1    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'MAct'
    SIZE = 4

    ACTION_RUN              = 0
    ACTION_STOP             = 1
    ACTION_STOP_RECORD      = 2
    ACTION_INSERT_USER_WAIT = 3
    ACTION_CONTINUE         = 4
    ACTION_DELETE           = 5

    NO_SLOT = 0xFFFF  # use as ``index`` for "stop recording" / "stop running"

    index  = u16(at=0)
    action = u8 (at=2)

    def __init__(self, *, index, action):
        super().__init__(index=index, action=action)


class MacroRecordCommand(Send):
    """``MSRc`` — begin recording into a macro slot.

    Variable-length payload (name + description bytes) doesn't fit a
    static field layout, so this overrides ``get_command`` directly.
    See ``atemwire/docs/MACRO_FORMAT.md`` for the full protocol overview.

    Wire layout (verified live against ATEM 1 M/E Constellation HD,
    2026-04-28):

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Macro slot index
    2      2    u16    Name length (bytes)
    4      2    u16    Description length (bytes)
    6      N    str[]  Name (UTF-8, no NUL terminator)
    6+N    M    str[]  Description (UTF-8)
    ====== ==== ====== ===========

    Header is six bytes; trailing payload is padded to a 4-byte align
    with NULs.
    """
    CODE = 'MSRc'

    def __init__(self, index, name, description=''):
        self.index = index
        self.name = name
        self.description = description

    def get_command(self) -> bytes:
        name = (self.name or '').encode('utf-8')
        description = (self.description or '').encode('utf-8')
        data = struct.pack('>HHH', self.index, len(name), len(description))
        data += name
        data += description
        rem = len(data) % 4
        if rem:
            data += b'\x00' * (4 - rem)
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


class MacroSleepCommand(Send):
    """``MSlp`` — record a sleep/wait of N frames into the active recording.

    Only meaningful between MSRc (start record) and MAct action 0x02
    (stop record).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      4    u32    Frame count (BE)
    ====== ==== ====== ===========
    """
    CODE = 'MSlp'
    SIZE = 4

    frames = u32(at=0)

    def __init__(self, frames):
        super().__init__(frames=int(frames))


# -----------------------------------------------------------------------------
# Incoming — slot metadata / record status / play status
# -----------------------------------------------------------------------------


class MacroPropertiesField(Recv):
    """``MPrp`` — metadata about a stored macro slot.

    Variable-length payload (name + description) so the parsing is
    hand-rolled; the trailing ``name`` and ``description`` byte strings
    are at offsets driven by their length fields.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Macro slot index
    2      1    bool   Is used
    3      1    bool   Has invalid commands
    4      2    u16    Name length
    6      2    u16    Description length
    8      N    char[] Name
    8+N    M    char[] Description
    ====== ==== ====== ===========
    """
    CODE = 'MPrp'
    PRETTY = 'macro-properties'
    KEY_FORMAT = struct.Struct('>H')

    def __init__(self, raw: bytes):
        self.raw = raw
        (self.index, self.is_used, self.is_invalid,
         name_len, desc_len) = struct.unpack_from('>H ?? H H', raw, 0)
        self.name, self.description = struct.unpack_from(
            f'>{name_len}s {desc_len}s', raw, 8)

    def __repr__(self):
        return (f'<macro-properties: index={self.index} '
                f'used={self.is_used} name={self.name}>')


class MacroRecordStatusField(Recv):
    """``MRcS`` — recording-state flag (drives the red record-border in
    Software Control) + currently-recording slot.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    bool   Is recording
    1      1    ?      padding
    2      2    u16    Macro slot index
    ====== ==== ====== ===========
    """
    CODE = 'MRcS'
    PRETTY = 'macro-record-status'

    is_recording = boolean(at=0)
    index        = u16    (at=2)

    def __repr__(self):
        return (f'<macro-status: recording={self.is_recording} '
                f'index={self.index}>')


class MacroPlayStatusField(Recv):
    """``MRPr`` — live macro playback status.

    Two flag bytes carry packed booleans (running / waiting / looping)
    so the parsing is hand-rolled.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     bit 0 = running, bit 1 = waiting
    1      1    u8     bit 0 = is_looping
    2      2    u16    Slot index (0xFFFF = idle)
    ====== ==== ====== ===========
    """
    CODE = 'MRPr'
    PRETTY = 'macro-play-status'
    IDLE = 0xFFFF

    def __init__(self, raw: bytes):
        self.raw = raw
        flags0, flags1, index = struct.unpack_from('>BBH', raw, 0)
        self.running = bool(flags0 & 0x01)
        self.waiting = bool(flags0 & 0x02)
        self.is_looping = bool(flags1 & 0x01)
        self.index = index

    def __repr__(self):
        idx = 'idle' if self.index == self.IDLE else self.index
        return (f'<macro-play-status: running={self.running} '
                f'waiting={self.waiting} looping={self.is_looping} '
                f'index={idx}>')


# =============================================================================
# Operations — verb wrappers around MacroActionCommand + record/sleep
# =============================================================================
#
# All operations are bucket B by Phase 1 decision 3: the wrappers
# encode caller-specific semantics (1-indexed UI → 0-indexed wire,
# specific action constants per verb) rather than just constructing
# a Send. Keeping them as named functions makes the dispatch table
# read as verbs ("execute_macro", "macro_stop") rather than raw
# MacroActionCommand(...) builders.


def execute_macro(conn, macro_number):
    """Frontend sends 1-indexed ``macro_number``; atemwire expects
    0-indexed. The -1 offset is a known footgun — wrapper keeps it
    captive so the dispatch lambda doesn't have to re-implement it."""
    conn.send(MacroActionCommand(
        index=int(macro_number) - 1, action=MACRO_ACTION_RUN,
    ))


def macro_run(conn, slot):
    """Trigger playback of macro at ``slot`` (0-indexed). Use
    ``execute_macro`` from the dispatch layer where args arrive
    1-indexed."""
    conn.send(MacroActionCommand(
        index=int(slot), action=MACRO_ACTION_RUN,
    ))


def macro_stop(conn):
    """Stop a currently-running macro. index=0xFFFF means 'whatever
    is playing right now'."""
    conn.send(MacroActionCommand(
        index=0xFFFF, action=MACRO_ACTION_STOP_RUN,
    ))


def macro_start_recording(conn, slot, name, description=''):
    """Begin recording into ``slot`` (0-indexed). Subsequent control
    commands are captured by the ATEM until ``macro_stop_recording``
    is called."""
    conn.send(MacroRecordCommand(
        index=int(slot),
        name=str(name or ''),
        description=str(description or ''),
    ))


def macro_stop_recording(conn):
    """Stop the active recording and persist the macro. The ATEM
    responds with ``MPrp`` for the slot once the macro is saved."""
    conn.send(MacroActionCommand(
        index=0xFFFF, action=MACRO_ACTION_STOP_RECORD,
    ))


def macro_sleep(conn, frames):
    """Record a wait of ``frames`` display frames into the active
    recording. Has no effect when no recording is in progress."""
    conn.send(MacroSleepCommand(frames=int(frames)))


# =============================================================================
# Readers
# =============================================================================


def macro_entry(mx, idx):
    """Return {is_used, name} for the given macro slot, or defaults
    if MPrp hasn't arrived for that slot."""
    nodes = mx.get('macro-properties') or {}
    node = nodes.get(idx)
    if node is None:
        return {'is_used': False, 'name': ''}
    return {
        'is_used': safe_bool(getattr(node, 'is_used', False), False),
        'name': decode_name(getattr(node, 'name', b''), ''),
    }


def macro_play_running(mx):
    node = mx.get('macro-play-status')
    return safe_bool(
        getattr(node, 'running', False), False) if node else False


def macro_play_index(mx):
    """Currently-playing macro index (0xFFFF means idle)."""
    node = mx.get('macro-play-status')
    return safe_int(
        getattr(node, 'index', 0xFFFF), 0xFFFF) if node else 0xFFFF
