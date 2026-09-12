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
from atemwire.messages._dsl import Send, boolean, i16, string, u8, u16, u32  # noqa: F401  (restored upstream commands)


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


class SupersourceBoxPropertiesCommand(Send):
    """``CSBP`` — properties of one of the four boxes of a SuperSource.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Mask
    2      1    u8     SuperSource index
    3      1    u8     Box index
    4      1    bool   Enabled
    5      1    ?      padding
    6      2    u16    Source index
    8      2    i16    Position X [-4800 - 4800]
    10     2    i16    Position Y [-3400 - 3400]
    12     2    u16    Size [70 - 1000]
    14     1    bool   Crop enable
    15     1    ?      padding
    16     2    u16    Crop top [0 - 18000]
    18     2    u16    Crop bottom [0 - 18000]
    20     2    u16    Crop left [0 - 32000]
    22     2    u16    Crop right [0 - 32000]
    ====== ==== ====== ===========

    Mask bits: 0 enabled, 1 source, 2 x, 3 y, 4 size, 5 crop enable,
               6 top, 7 bottom, 8 left, 9 right.
    """
    CODE = 'CSBP'
    SIZE = 24
    MASK_AT = 0
    MASK_TYPE = u16

    index   = u8     (at=2)
    box     = u8     (at=3)
    enabled = boolean(at=4, mask_bit=0)
    source  = u16    (at=6, mask_bit=1)
    x       = i16    (at=8, mask_bit=2)
    y       = i16    (at=10, mask_bit=3)
    size    = u16    (at=12, mask_bit=4)
    masked  = boolean(at=14, mask_bit=5)
    top     = u16    (at=16, mask_bit=6)
    bottom  = u16    (at=18, mask_bit=7)
    left    = u16    (at=20, mask_bit=8)
    right   = u16    (at=22, mask_bit=9)

    def __init__(self, index, box, enabled=None, source=None, x=None, y=None, size=None,
                 masked=None, top=None, bottom=None, left=None, right=None):
        super().__init__(index=index, box=box, enabled=enabled, source=source, x=x, y=y,
                         size=size, masked=masked, top=top, bottom=bottom, left=left, right=right)


class SupersourcePropertiesCommand(Send):
    """``CSSc`` — global SuperSource options (the Art sources and keyer).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask
    1      1    u8     SuperSource index
    2      2    u16    Artwork fill source
    4      2    u16    Artwork key source
    6      1    u8     Artwork layer [0 background, 1 foreground]
    7      1    bool   Premultiplied
    8      2    u16    Keyer clip [0-1000]
    10     2    u16    Keyer gain [0-1000]
    12     1    bool   Keyer invert
    13     3    ?      padding
    ====== ==== ====== ===========

    Mask bits: 0 fill, 1 key, 2 layer, 3 premultiplied, 4 clip, 5 gain,
               6 invert.
    """
    CODE = 'CSSc'
    SIZE = 16
    MASK_AT = 0

    index         = u8     (at=1)
    fill_source   = u16    (at=2, mask_bit=0)
    key_source    = u16    (at=4, mask_bit=1)
    layer         = u8     (at=6, mask_bit=2)
    premultiplied = boolean(at=7, mask_bit=3)
    clip          = u16    (at=8, mask_bit=4)
    gain          = u16    (at=10, mask_bit=5)
    invert        = boolean(at=12, mask_bit=6)

    def __init__(self, index, fill_source=None, key_source=None, layer=None, premultiplied=None,
                 clip=None, gain=None, invert=None):
        super().__init__(index=index, fill_source=fill_source, key_source=key_source, layer=layer,
                         premultiplied=premultiplied, clip=clip, gain=gain, invert=invert)


def set_supersource_box(conn, box, index=0, **props):
    """Set any of enabled / source / x / y / size / masked / top / bottom /
    left / right on box ``box`` of SuperSource ``index``."""
    conn.send(SupersourceBoxPropertiesCommand(index=index, box=int(box), **props))


def set_supersource_properties(conn, index=0, **props):
    """Set any of fill_source / key_source / layer / premultiplied / clip /
    gain / invert on SuperSource ``index``."""
    conn.send(SupersourcePropertiesCommand(index=index, **props))
