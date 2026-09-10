# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
SuperSource — multi-box compositor available on larger ATEMs (Production
4k, Constellation, etc.). Up to 4 boxes per supersource, each with its
own source / position / size / mask.

Wire packets (incoming only):
    SSrc — supersource element (overall fill / key / clip / gain config)
    SSBP — per-box config (source / position / size / mask)
"""

import struct

from atemwire.messages._dsl import Recv


class SupersourcePropertiesField(Recv):
    """``SSrc`` — supersource element configuration.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Index
    1      1    ?      padding
    2      2    u16    Fill source
    4      2    u16    Key source
    6      1    u8     Layer
    7      1    bool   Premultiplied
    8      2    u16    Clip
    10     2    u16    Gain
    12     1    bool   Inverted
    13     3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'SSrc'
    PRETTY = 'supersource-properties'
    KEY_FORMAT = struct.Struct('>B')

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack('>BxH HB? HH ?xxx', raw)
        self.index         = field[0]
        self.fill_source   = field[1]
        self.key_source    = field[2]
        self.layer         = field[3]
        self.premultiplied = field[4]
        self.clip          = field[5]
        self.gain          = field[6]
        self.inverted      = field[7]

    def __repr__(self):
        return (f'<supersource-properties index={self.index} '
                f'fill={self.fill_source} key={self.key_source}>')


class SupersourceBoxPropertiesField(Recv):
    """``SSBP`` — config for a single SuperSource box.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     SuperSource index
    1      1    u8     Box index
    2      1    bool   Enabled
    3      1    ?      padding
    4      2    u16    Source index
    6      2    i16    X position
    8      2    i16    Y position
    10     2    u16    Size
    12     1    bool   Mask enabled
    13     1    ?      padding
    14     2    u16    Mask top
    16     2    u16    Mask bottom
    18     2    u16    Mask left
    20     2    u16    Mask right
    22     2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'SSBP'
    PRETTY = 'supersource-box-properties'
    KEY_FORMAT = struct.Struct('>BB')

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack('>BB?xH hhH ?x HHHH 2x', raw)
        self.index       = field[0]
        self.box         = field[1]
        self.enabled     = field[2]
        self.source      = field[3]
        self.x           = field[4]
        self.y           = field[5]
        self.size        = field[6]
        self.masked      = field[7]
        self.mask_top    = field[8]
        self.mask_bottom = field[9]
        self.mask_left   = field[10]
        self.mask_right  = field[11]

    def __repr__(self):
        return (f'<supersource-box-properties index={self.index}, '
                f'box={self.box}, source={self.source}, x={self.x}, '
                f'y={self.y}, size={self.size}>')

