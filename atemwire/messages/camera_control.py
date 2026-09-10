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
