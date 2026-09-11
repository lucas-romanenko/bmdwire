# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Camera control packets — broadcast SDI camera control messages routed
through the ATEM. Each packet carries one parameter update (focus,
iris, white balance, color corrector, ...) for a destination camera.

Wire packets (incoming only):
    CCdP — camera control data packet
"""

import struct

from atemwire.messages._dsl import Recv
from atemwire.messages._dsl import Send, boolean, i16, string, u8, u16, u32  # noqa: F401  (restored upstream commands)


class CameraControlDataPacketField(Recv):
    """``CCdP`` — single camera-control parameter update.

    Roughly mirrors the BMD SDI Camera Control Protocol but with bytes
    laid out differently. The 4-byte ``weird`` block at offset 4-15 is
    elements-per-type; the actual data type and element count
    sometimes need overrides for specific (category, parameter) pairs.

    See the original docstring (in field.py.bak / git history) for the
    full command table — this class just preserves the parsing
    byte-for-byte.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Destination (255 = broadcast)
    1      1    u8     Category
    2      1    u8     Parameter
    3      1    u8     Data type
    4      12   ?      Per-element-count weird block
    16     8    ?      Variable data (absent for trigger commands)
    ====== ==== ====== ===========
    """
    CODE = 'CCdP'
    PRETTY = 'camera-control-data-packet'
    KEY_FORMAT = struct.Struct('>BBB')

    # Some (category, parameter) pairs need element-count overrides
    # because the on-wire ``weird`` byte block doesn't match what
    # actually follows.
    _NUM_OVERRIDES = {
        (0, 0): 1, (0, 1): 0, (0, 2): 1, (0, 3): 1, (0, 4): 1, (0, 6): 1,
        (1, 2): 2,
    }

    def __init__(self, raw: bytes):
        self.raw = raw
        (self.destination, self.category, self.parameter,
         self.datatype, *weird) = struct.unpack_from('>4B 4B 4B', raw, 0)

        num_elements = sum(weird)
        if (self.category, self.parameter) in self._NUM_OVERRIDES:
            num_elements = self._NUM_OVERRIDES[(self.category, self.parameter)]
        self.length = num_elements

        self.data = None
        if len(raw) > 16:
            dfmt = '>'
            if self.datatype == 0:    # Boolean
                dfmt += '?' * num_elements
            elif self.datatype == 1:  # Signed byte
                dfmt += 'b' * num_elements
            elif self.datatype == 2:  # Signed short
                dfmt += 'h' * num_elements
            elif self.datatype == 3:  # Signed int
                dfmt += 'i' * num_elements
            elif self.datatype == 4:  # Signed long
                dfmt += 'q' * num_elements
            elif self.datatype == 5:  # UTF-8 (no struct format)
                pass
            elif self.datatype == 128:  # Fixed16
                dfmt += 'h' * num_elements
            self.data = struct.unpack_from(dfmt, raw, 16)
            if self.datatype == 128:
                self.data = self._unpack_fixed16(self.data)

    @staticmethod
    def _unpack_fixed16(raw):
        return [f / (2 ** 11) for f in raw]

    def __repr__(self):
        return (f'<camera-control-data-packet dest={self.destination} '
                f'command={self.category}.{self.parameter} '
                f'type={self.datatype} data={self.data}>')


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


class CameraControlCommand(Send):
    """``CCmd`` — send a Blackmagic SDI camera control command through the
    switcher to an attached camera.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Destination (camera index, 255 = broadcast)
    1      1    u8     Category
    2      1    u8     Parameter
    3      1    bool   Relative adjustment
    4      1    u8     Data type (0 bool, 1 int8, 2 int16, 3 int32, 4 int64, 5 string, 128 fixed16)
    5      11   ?      element counts (the count lands at the type-dependent offset upstream used)
    16     ...  ?      data, padded to 8 bytes
    ====== ==== ====== ===========

    Ported verbatim from upstream, including its count-offset table and the
    padding rule; upstream drove real cameras with it but this fork has not
    re-verified it. ``data`` is copied, never mutated.
    """
    CODE = 'CCmd'
    SIZE = 16

    _COUNT_OFFSET = {0: 2, 1: 2, 2: 2, 3: 4, 4: 2, 5: 2, 128: 4}
    _ELEMENT_FMT = {0: '?', 1: 'b', 2: 'h', 3: 'i', 4: 'q', 5: '', 128: 'h'}

    destination = u8     (at=0)
    category    = u8     (at=1)
    parameter   = u8     (at=2)
    relative    = boolean(at=3)
    datatype    = u8     (at=4)

    def __init__(self, destination, category, parameter, relative=False, datatype=None, data=None):
        super().__init__(destination=destination, category=category, parameter=parameter,
                         relative=bool(relative), datatype=0 if datatype is None else datatype)
        self.data = None if data is None else list(data)

    def get_command(self):
        import struct
        count = len(self.data) if self.data is not None else 0
        buf = bytearray(self.SIZE)
        struct.pack_into('>5B', buf, 0, self.destination, self.category, self.parameter,
                         1 if self.relative else 0, self.datatype)
        buf[5 + self._COUNT_OFFSET[self.datatype]] = count
        payload = bytes(buf)
        if self.data is not None:
            values = list(self.data)
            if self.datatype == 128:
                values = [int(v * (2 ** 11)) for v in values]
                fmt = f'>{count}h'
            elif self.datatype == 5:
                values = [v.encode() if isinstance(v, str) else bytes(v) for v in values]
                fmt = f'>{len(values[0])}s'
            else:
                fmt = f'>{count}{self._ELEMENT_FMT[self.datatype]}'
            packed = struct.pack(fmt, *values)
            packed += b'\0' * (8 - len(packed))
            payload += packed
        header = struct.pack('>H 2x 4s', len(payload) + 8, self.CODE.encode())
        return header + payload


def camera_control(conn, destination, category, parameter, relative=False, datatype=None, data=None):
    """Send one camera control command (see CameraControlCommand)."""
    conn.send(CameraControlCommand(destination, category, parameter, relative=relative,
                                   datatype=datatype, data=data))
