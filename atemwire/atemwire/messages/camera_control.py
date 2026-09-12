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
from atemwire.messages._dsl import Send, boolean, tail, u8  # noqa: F401  (restored upstream commands)


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
    5      2    ?      unknown
    7      1    u8     Element count for bool / int8 / int16 / int64 / string
    8      1    ?      unknown
    9      1    u8     Element count for int32 / fixed16
    10     6    ?      unknown
    16     ...  bytes  Data, packed per type, zero-padded to 8 bytes
    ====== ==== ====== ===========

    The layout, the two count positions and the padding rule are upstream's
    (it drove real cameras with them; this fork has not re-verified them).
    ``__init__`` turns the Python values into wire bytes: fixed16 is
    ``int(v * 2**11)`` as i16, strings are UTF-8, everything else is the
    big-endian struct type. The caller's list is never mutated.
    """
    CODE = 'CCmd'
    SIZE = 16

    _COUNT_AT_9 = {3, 128}
    _ELEMENT_FMT = {0: '?', 1: 'b', 2: 'h', 3: 'i', 4: 'q', 128: 'h'}

    destination = u8     (at=0)
    category    = u8     (at=1)
    parameter   = u8     (at=2)
    relative    = boolean(at=3)
    datatype    = u8     (at=4)
    count_at_7  = u8     (at=7)
    count_at_9  = u8     (at=9)
    data        = tail   (pad_to=8)

    def __init__(self, destination, category, parameter, relative=False, datatype=None, data=None):
        datatype = 0 if datatype is None else datatype
        count = len(data) if data is not None else 0
        super().__init__(
            destination=destination, category=category, parameter=parameter,
            relative=bool(relative), datatype=datatype,
            count_at_9=count if datatype in self._COUNT_AT_9 else None,
            count_at_7=None if datatype in self._COUNT_AT_9 else count,
            data=None if data is None else self._encode(datatype, list(data)),
        )

    @classmethod
    def _encode(cls, datatype, values):
        import struct
        if datatype == 5:
            parts = [v.encode() if isinstance(v, str) else bytes(v) for v in values]
            return b''.join(struct.pack(f'>{len(p)}s', p) for p in parts)
        if datatype == 128:
            values = [int(v * (2 ** 11)) for v in values]
        return struct.pack(f'>{len(values)}{cls._ELEMENT_FMT[datatype]}', *values)


def camera_control(conn, destination, category, parameter, relative=False, datatype=None, data=None):
    """Send one camera control command (see CameraControlCommand)."""
    conn.send(CameraControlCommand(destination, category, parameter, relative=relative,
                                   datatype=datatype, data=data))
