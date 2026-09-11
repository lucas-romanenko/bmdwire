# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Recording state messages — disk inventory, encoder settings, current
recording status, and elapsed-time counter.

Wire packets (incoming only):
    RTMD — recording disk attached
    RMSu — recording settings (filename, disk slots)
    RMTS — recording status flags + remaining time
    RTMR — recording duration (HH:MM:SS:FF)
"""

from atemwire.messages._dsl import Recv, boolean, i32, string, u8, u16, u32
from atemwire.messages._dsl import Send, boolean, i16, string, u8, u16, u32  # noqa: F401  (restored upstream commands)


class RecordingDiskField(Recv):
    """``RTMD`` — info about an attached recording disk.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      4    u32    Disk index
    4      4    u32    Recording time available (seconds)
    8      2    u16    Status bitfield
    10     64   str    Volume name
    74     2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'RTMD'
    PRETTY = 'recording-disk'

    index          = u32   (at=0)
    time_available = u32   (at=4)
    status         = u16   (at=8)
    volumename     = string(at=10, size=64)

    def __init__(self, raw: bytes):
        super().__init__(raw)
        # Status bitfield. Bit 0 is currently unmapped — upstream
        # ships a redundant ``is_attached = bit 0 then bit 1`` shape; we
        # keep the bit-1 mapping (the effective one in upstream) and drop
        # the dead overwrite. If we ever identify what bit 0 represents,
        # add it under its own name.
        self.is_attached = self.status & 1 << 1 > 0
        self.is_ready = self.status & 1 << 2 > 0
        self.is_recording = self.status & 1 << 3 > 0
        self.is_deleted = self.status & 1 << 5 > 0

    def __repr__(self):
        return (f'<recording-disk disk={self.index} label={self.volumename} '
                f'status={self.status} available={self.time_available}>')


class RecordingSettingsField(Recv):
    """``RMSu`` — stream recorder settings (filename, disk slot routing).

    ``disk1`` / ``disk2`` are -1 on the wire when no disk is selected;
    the constructor normalizes that to ``None``.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      128  str    Output filename
    128    4    i32    Disk slot 1 index (or -1)
    132    4    i32    Disk slot 2 index (or -1)
    136    1    bool   Trigger recording on cameras
    137    3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'RMSu'
    PRETTY = 'recording-settings'

    filename           = string (at=0,   size=128)
    _disk1             = i32    (at=128)
    _disk2             = i32    (at=132)
    record_in_cameras  = boolean(at=136)

    def __init__(self, raw: bytes):
        super().__init__(raw)
        self.disk1 = self._disk1 if self._disk1 != -1 else None
        self.disk2 = self._disk2 if self._disk2 != -1 else None

    def __repr__(self):
        return (f'<recording-settings filename={self.filename} '
                f'disk1={self.disk1} disk2={self.disk2} '
                f'in-camera={self.record_in_cameras}>')


class RecordingStatusField(Recv):
    """``RTMS`` — recording status flags + total time available.

    Upstream's field class had ``CODE = 'RMTS'`` but the actual wire
    code (per the dispatch table that's been working for years) is
    ``RTMS``. Aligned here so our CODE-based registry can find it.

    ``time_available`` is -1 on the wire when no disk is selected;
    normalized to ``None``.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Status bitfield
    2      2    ?      padding
    4      4    i32    Time available (seconds)
    ====== ==== ====== ===========
    """
    CODE = 'RTMS'
    PRETTY = 'recording-status'

    status          = u16(at=0)
    _time_available = i32(at=4)

    def __init__(self, raw: bytes):
        super().__init__(raw)
        self.time_available = (self._time_available
                               if self._time_available != -1 else None)
        self.is_recording      = self.status & 1 << 0 > 0
        self.is_stopping       = self.status & 1 << 7 > 0
        self.disk_full         = self.status & 1 << 2 > 0
        self.disk_error        = self.status & 1 << 3 > 0
        self.disk_unformatted  = self.status & 1 << 4 > 0
        self.has_dropped       = self.status & 1 << 5 > 0

    def __repr__(self):
        return (f'<recording-status status={self.status} '
                f'time-available={self.time_available}>')


class RecordingDurationField(Recv):
    """``RTMR`` — current recording duration (HH:MM:SS:FF) + dropped flag.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Hours
    1      1    u8     Minutes
    2      1    u8     Seconds
    3      1    u8     Frames
    4      1    bool   Has dropped frames
    5      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'RTMR'
    PRETTY = 'recording-duration'

    hours              = u8     (at=0)
    minutes            = u8     (at=1)
    seconds            = u8     (at=2)
    frames             = u8     (at=3)
    has_dropped_frames = boolean(at=4)

    def __repr__(self):
        drop = ' dropped-frames' if self.has_dropped_frames else ''
        return (f'<recording-duration {self.hours}:{self.minutes}:'
                f'{self.seconds}:{self.frames}{drop}>')


# -----------------------------------------------------------------------------
# Restored upstream commands (0.15, 2026-09-11)
#
# These Send classes existed in upstream pyatem and were dropped from the
# fork because nothing exercised them. They are back, declared in the DSL
# with the exact byte layout of upstream's struct.pack strings and pinned
# byte-for-byte in tests/test_restored_upstream_commands.py. They have NOT
# been re-verified against a switcher in this fork; treat them as upstream
# did, and Wireshark-check before relying on a write on your model.
# -----------------------------------------------------------------------------


class RecordingSettingsSetCommand(Send):
    """``CRMS`` — stream-recorder settings (Record settings of the Output
    menu).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (bit 0 filename, 1 disk1, 2 disk2, 3 record in cameras)
    1      128  str    Filename
    132    4    u32    Disk 1 id
    136    4    u32    Disk 2 id
    140    1    bool   Record in all cameras
    141    3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'CRMS'
    SIZE = 144
    MASK_AT = 0

    filename         = string (at=1, size=128)
    disk1            = u32    (at=132, mask_bit=1)
    disk2            = u32    (at=136, mask_bit=2)
    record_in_camera = boolean(at=140, mask_bit=3)

    def __init__(self, filename=None, disk1=None, disk2=None, record_in_camera=None):
        super().__init__(filename=filename, disk1=disk1, disk2=disk2,
                         record_in_camera=record_in_camera)

    def get_command(self):
        # ``string`` fields carry no mask bit in the DSL; filename is bit 0.
        raw = bytearray(super().get_command())
        if self.filename is not None:
            raw[8] |= 1 << 0
        return bytes(raw)


class RecorderStatusCommand(Send):
    """``RcTM`` — start or stop the stream recorder (REC in the Output menu).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    bool   Recording
    1      3    ?      unknown
    ====== ==== ====== ===========
    """
    CODE = 'RcTM'
    SIZE = 4

    recording = boolean(at=0)

    def __init__(self, recording):
        super().__init__(recording=recording)


def set_recording_settings(conn, filename=None, disk1=None, disk2=None, record_in_camera=None):
    conn.send(RecordingSettingsSetCommand(filename=filename, disk1=disk1, disk2=disk2,
                                          record_in_camera=record_in_camera))


def start_recording(conn):
    conn.send(RecorderStatusCommand(True))


def stop_recording(conn):
    conn.send(RecorderStatusCommand(False))
