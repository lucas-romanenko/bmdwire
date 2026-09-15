# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Upstream keyer (USK) — on-air toggle, type / fill / cut sources, mask
crop, the four key-type-specific property blocks (luma, chroma /
advanced chroma, pattern, DVE), and the keyframe machinery for the DVE
flying key.

Wire packets (outgoing):
    CKOn — set on-air state
    CKeF — fill source
    CKeC — cut (key) source
    CKTp — keyer type + fly enable
    CKDV — DVE properties (size / position / rotation / border / shadow / mask / rate)
    CKMs — mask crop properties
    CKPt — pattern key properties
    CACC — advanced chroma colorpicker
    CACK — advanced chroma keyer
    CKLm — luma key properties
    SFKF — flying key keyframe set (A/B)
    RFlK — flying key run command (A/B/Full/Infinite)

Wire packets (incoming):
    KeOn — current on-air state
    KeBP — base properties (type / sources / mask)
    KeDV — DVE properties
    KePt — pattern key properties
    KeFS — flying key state (A/B set, at-keyframe flags)
    KeLm — luma key properties
    KACk — advanced chroma keyer state
    KACC — advanced chroma colorpicker state
"""

import colorsys
import struct

from atemwire.state import (
    _kv, display_fps, percent_from_thousandths, percent_to_tenths,
    percent_to_thousandths, percent_from_unit, safe_bool, safe_float,
    safe_int, size_thousandths, unit_from_thousandths,
    unit_to_thousandths, value_from_tenths, value_from_thousandths,
    value_to_thousandths,
)
from atemwire.helpers import keyframe_constant, parse_rate
from atemwire.messages._dsl import Recv, Send, boolean, i16, i32, u8, u16, u32


# =============================================================================
# Outgoing
# =============================================================================


class KeyOnAirCommand(Send):
    """``CKOn`` — set on-air state for one upstream keyer.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Keyer index
    2      1    bool   Enabled
    3      1    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'CKOn'
    SIZE = 4

    index   = u8     (at=0)
    keyer   = u8     (at=1)
    enabled = boolean(at=2)

    def __init__(self, index, keyer, enabled):
        super().__init__(index=index, keyer=keyer, enabled=enabled)


class KeyFillCommand(Send):
    """``CKeF`` — set USK fill source.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Keyer index
    2      2    u16    Source index
    ====== ==== ====== ===========
    """
    CODE = 'CKeF'
    SIZE = 4

    index  = u8 (at=0)
    keyer  = u8 (at=1)
    source = u16(at=2)

    def __init__(self, index, keyer, source):
        super().__init__(index=index, keyer=keyer, source=source)


class KeyCutCommand(Send):
    """``CKeC`` — set USK cut (key) source.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Keyer index
    2      2    u16    Source index
    ====== ==== ====== ===========
    """
    CODE = 'CKeC'
    SIZE = 4

    index  = u8 (at=0)
    keyer  = u8 (at=1)
    source = u16(at=2)

    def __init__(self, index, keyer, source):
        super().__init__(index=index, keyer=keyer, source=source)


class KeyTypeCommand(Send):
    """``CKTp`` — set USK type (Luma / Chroma / Pattern / DVE) + fly flag.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (bit 0 type, bit 1 fly_enabled)
    1      1    u8     M/E index
    2      1    u8     Keyer index
    3      1    u8     Keyer type
    4      1    bool   Fly enabled
    5      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'CKTp'
    SIZE = 8
    MASK_AT = 0

    LUMA    = 0
    CHROMA  = 1
    PATTERN = 2
    DVE     = 3

    index       = u8     (at=1)
    keyer       = u8     (at=2)
    type        = u8     (at=3, mask_bit=0)
    fly_enabled = boolean(at=4, mask_bit=1)

    def __init__(self, index, keyer, type=None, fly_enabled=None):
        super().__init__(index=index, keyer=keyer, type=type,
                         fly_enabled=fly_enabled)


class KeyPropertiesDveCommand(Send):
    """``CKDV`` — set DVE-key properties.

    Big mask — 26 bits over a u32 mask field at offset 0. Most fields
    are scaled int values matching the on-wire format; the helper
    ``set_border_color_rgb`` provides RGB → HLS conversion.

    Mask bits: 0 size_x, 1 size_y, 2 pos_x, 3 pos_y, 4 rotation,
               5 border_enabled, 6 shadow_enabled, 7 border_bevel,
               8 outer_width, 9 inner_width, 10 outer_softness,
               11 inner_softness, 12 bevel_softness, 13 bevel_position,
               14 border_opacity, 15 hue, 16 saturation, 17 luma,
               18 light angle, 19 light altitude, 20 mask_enabled,
               21 mask_top, 22 mask_bottom, 23 mask_left, 24 mask_right,
               25 rate.
    """
    CODE = 'CKDV'
    SIZE = 64
    MASK_AT = 0
    MASK_TYPE = u32  # 26 mask bits — bits 8-25 live above the low byte

    index                 = u8     (at=4)
    keyer                 = u8     (at=5)
    size_x                = i32    (at=8,  mask_bit=0)
    size_y                = i32    (at=12, mask_bit=1)
    pos_x                 = i32    (at=16, mask_bit=2)
    pos_y                 = i32    (at=20, mask_bit=3)
    rotation              = i32    (at=24, mask_bit=4)
    border_enabled        = boolean(at=28, mask_bit=5)
    shadow_enabled        = boolean(at=29, mask_bit=6)
    border_bevel_enabled  = u8     (at=30, mask_bit=7)
    outer_width           = u16    (at=32, mask_bit=8)
    inner_width           = u16    (at=34, mask_bit=9)
    outer_softness        = u8     (at=36, mask_bit=10)
    inner_softness        = u8     (at=37, mask_bit=11)
    bevel_softness        = u8     (at=38, mask_bit=12)
    bevel_position        = u8     (at=39, mask_bit=13)
    border_opacity        = u8     (at=40, mask_bit=14)
    border_hue            = u16    (at=42, mask_bit=15)
    border_saturation     = u16    (at=44, mask_bit=16)
    border_luma           = u16    (at=46, mask_bit=17)
    angle                 = u16    (at=48, mask_bit=18)
    altitude              = u8     (at=50, mask_bit=19)
    mask_enabled          = boolean(at=51, mask_bit=20)
    mask_top              = i16    (at=52, mask_bit=21)
    mask_bottom           = i16    (at=54, mask_bit=22)
    mask_left             = i16    (at=56, mask_bit=23)
    mask_right            = i16    (at=58, mask_bit=24)
    rate                  = u8     (at=60, mask_bit=25)

    def __init__(self, index, keyer, **kwargs):
        super().__init__(index=index, keyer=keyer, **kwargs)

    def set_border_color_rgb(self, red, green, blue):
        h, l, s = colorsys.rgb_to_hls(red, green, blue)
        self.border_hue = int(h * 3590)
        self.border_saturation = int(s * 1000)
        self.border_luma = int(l * 1000)


class KeyPropertiesMaskCommand(Send):
    """``CKMs`` — set USK mask crop (rectangular).

    Wire format drafted from the DkeyMaskCommand template + KeBP receive
    layout (2026-04-21). Not Wireshark-validated yet.

    Mask bits: 0 enabled, 1 top, 2 bottom, 3 left, 4 right.
    """
    CODE = 'CKMs'
    SIZE = 12
    MASK_AT = 0

    index   = u8     (at=1)
    keyer   = u8     (at=2)
    enabled = boolean(at=3, mask_bit=0)
    top     = i16    (at=4, mask_bit=1)
    bottom  = i16    (at=6, mask_bit=2)
    left    = i16    (at=8, mask_bit=3)
    right   = i16    (at=10, mask_bit=4)

    def __init__(self, index, keyer, enabled=None, top=None, bottom=None,
                 left=None, right=None):
        super().__init__(index=index, keyer=keyer, enabled=enabled,
                         top=top, bottom=bottom, left=left, right=right)


class KeyPropertiesPatternCommand(Send):
    """``CKPt`` — set USK pattern-key parameters.

    Mask bits: 0 pattern, 1 size, 2 symmetry, 3 softness,
               4 position_x, 5 position_y, 6 invert_pattern.

    Validated against PyATEMMax reference implementation — wire format
    is exact.
    """
    CODE = 'CKPt'
    SIZE = 16
    MASK_AT = 0

    index          = u8     (at=1)
    keyer          = u8     (at=2)
    pattern        = u8     (at=3,  mask_bit=0)
    size           = u16    (at=4,  mask_bit=1)
    symmetry       = u16    (at=6,  mask_bit=2)
    softness       = u16    (at=8,  mask_bit=3)
    position_x     = u16    (at=10, mask_bit=4)
    position_y     = u16    (at=12, mask_bit=5)
    invert_pattern = boolean(at=14, mask_bit=6)

    def __init__(self, index, keyer, pattern=None, size=None, symmetry=None,
                 softness=None, position_x=None, position_y=None,
                 invert_pattern=None):
        super().__init__(index=index, keyer=keyer, pattern=pattern,
                         size=size, symmetry=symmetry, softness=softness,
                         position_x=position_x, position_y=position_y,
                         invert_pattern=invert_pattern)


class KeyPropertiesAdvancedChromaColorpickerCommand(Send):
    """``CACC`` — advanced chroma colorpicker state.

    Mask bits: 0 cursor, 1 preview, 2 x, 3 y, 4 size,
               5 Y, 6 Cb, 7 Cr.
    """
    CODE = 'CACC'
    SIZE = 20
    MASK_AT = 0

    index   = u8     (at=1)
    keyer   = u8     (at=2)
    cursor  = boolean(at=3,  mask_bit=0)
    preview = boolean(at=4,  mask_bit=1)
    x       = i16    (at=6,  mask_bit=2)
    y       = i16    (at=8,  mask_bit=3)
    size    = u16    (at=10, mask_bit=4)
    Y       = u16    (at=12, mask_bit=5)
    Cb      = i16    (at=14, mask_bit=6)
    Cr      = i16    (at=16, mask_bit=7)

    def __init__(self, index, keyer, cursor=None, preview=None,
                 x=None, y=None, size=None, Y=None, Cb=None, Cr=None):
        super().__init__(index=index, keyer=keyer, cursor=cursor,
                         preview=preview, x=x, y=y, size=size,
                         Y=Y, Cb=Cb, Cr=Cr)


class KeyPropertiesAdvancedChromaCommand(Send):
    """``CACK`` — advanced chroma keyer state.

    Mask bits: 0 foreground, 1 background, 2 key_edge, 3 spill,
               4 flare, 5 brightness, 6 contrast, 7 saturation,
               8 red, 9 green, 10 blue. (u16 BE mask, bits 8-10 in
               the high byte.)
    """
    CODE = 'CACK'
    SIZE = 28
    MASK_AT = 0
    MASK_TYPE = u16  # bits 8-10 (red, green, blue) live in the high byte

    index       = u8 (at=2)
    keyer       = u8 (at=3)
    foreground  = u16(at=4,  mask_bit=0)
    background  = u16(at=6,  mask_bit=1)
    key_edge    = u16(at=8,  mask_bit=2)
    spill       = u16(at=10, mask_bit=3)
    flare       = u16(at=12, mask_bit=4)
    brightness  = i16(at=14, mask_bit=5)
    contrast    = i16(at=16, mask_bit=6)
    saturation  = u16(at=18, mask_bit=7)
    red         = i16(at=20, mask_bit=8)
    green       = i16(at=22, mask_bit=9)
    blue        = i16(at=24, mask_bit=10)

    def __init__(self, index, keyer, foreground=None, background=None,
                 key_edge=None, spill=None, flare=None, brightness=None,
                 contrast=None, saturation=None, red=None, green=None,
                 blue=None):
        super().__init__(index=index, keyer=keyer, foreground=foreground,
                         background=background, key_edge=key_edge,
                         spill=spill, flare=flare, brightness=brightness,
                         contrast=contrast, saturation=saturation,
                         red=red, green=green, blue=blue)


class KeyPropertiesLumaCommand(Send):
    """``CKLm`` — luma key properties.

    Mask bits: 0 premultiplied, 1 clip, 2 gain, 3 invert_key.
    """
    CODE = 'CKLm'
    SIZE = 12
    MASK_AT = 0

    index         = u8     (at=1)
    keyer         = u8     (at=2)
    premultiplied = boolean(at=3, mask_bit=0)
    clip          = u16    (at=4, mask_bit=1)
    gain          = u16    (at=6, mask_bit=2)
    invert_key    = boolean(at=8, mask_bit=3)

    def __init__(self, index, keyer, premultiplied=None, clip=None,
                 gain=None, invert_key=None):
        super().__init__(index=index, keyer=keyer,
                         premultiplied=premultiplied, clip=clip,
                         gain=gain, invert_key=invert_key)


class KeyerKeyframeSetCommand(Send):
    """``SFKF`` — set the A or B keyframe of the flying-key DVE to the
    current DVE state.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Keyer index
    2      1    u8     Keyframe (A=1, B=2)
    3      1    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'SFKF'
    SIZE = 4

    index    = u8(at=0)
    keyer    = u8(at=1)
    keyframe = u8(at=2)

    def __init__(self, index, keyer, keyframe):
        # Accept either the int keyframe constant (1=A, 2=B, 3=Full, … as
        # helpers.keyframe_constant returns and set_keyer_fly_keyframe passes)
        # or the legacy 'A'/'B' string. The old ``1 if keyframe == 'A' else 2``
        # coerced every int to 2, so set_keyer_fly_keyframe could only ever
        # store keyframe B — keyframe A was unreachable.
        if isinstance(keyframe, str):
            kf = 1 if keyframe.upper() == 'A' else 2
        else:
            kf = int(keyframe)
        super().__init__(index=index, keyer=keyer, keyframe=kf)


class KeyerKeyframeRunCommand(Send):
    """``RFlK`` — run the flying key to A / B / Full / Infinite.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (bit 1 = direction is set)
    1      1    u8     M/E index
    2      1    u8     Keyer index
    3      1    ?      padding
    4      1    u8     Run-to (1=A, 2=B, 3=Full, 4=Infinite)
    5      1    u8     Infinite-run direction index
    6      2    ?      padding
    ====== ==== ====== ===========

    Pass ``run_to`` as an int (1..4) or one of the strings ``'A'``,
    ``'B'``, ``'Full'``, ``'Infinite'``. ``direction`` is only
    meaningful when ``run_to=Infinite`` — leave it ``None`` for
    A/B/Full runs.
    """
    CODE = 'RFlK'
    SIZE = 8
    MASK_AT = 0

    _RUN_TO = {'A': 1, 'B': 2, 'Full': 3, 'Infinite': 4}

    index     = u8(at=1)
    keyer     = u8(at=2)
    run_to    = u8(at=4)
    direction = u8(at=5, mask_bit=1)

    def __init__(self, index, keyer, run_to=None, direction=None):
        if isinstance(run_to, str):
            run_to = self._RUN_TO[run_to]
        super().__init__(index=index, keyer=keyer,
                         run_to=run_to, direction=direction)


# =============================================================================
# Incoming
# =============================================================================


class KeyOnAirField(Recv):
    """``KeOn`` — current on-air state for one upstream keyer.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Keyer index
    2      1    bool   On-air
    3      1    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'KeOn'
    PRETTY = 'key-on-air'
    KEY_FORMAT = struct.Struct('>BB')

    index   = u8     (at=0)
    keyer   = u8     (at=1)
    enabled = boolean(at=2)

    def __repr__(self):
        return (f'<key-on-air: me={self.index}, keyer={self.keyer}, '
                f'enabled={self.enabled}>')


class KeyPropertiesBaseField(Recv):
    """``KeBP`` — USK base properties (type + sources + mask crop).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Keyer index
    2      1    u8     Type (Luma/Chroma/Pattern/DVE)
    3      1    bool   Enabled (on-air)
    4      1    ?      padding
    5      1    bool   Fly enabled
    6      2    u16    Fill source
    8      2    u16    Key source
    10     1    bool   Mask enabled
    11     1    ?      padding
    12     2    i16    Mask top
    14     2    i16    Mask bottom
    16     2    i16    Mask left
    18     2    i16    Mask right
    ====== ==== ====== ===========
    """
    CODE = 'KeBP'
    PRETTY = 'key-properties-base'
    KEY_FORMAT = struct.Struct('>BB')

    index        = u8     (at=0)
    keyer        = u8     (at=1)
    type         = u8     (at=2)
    enabled      = boolean(at=3)
    fly_enabled  = boolean(at=5)
    fill_source  = u16    (at=6)
    key_source   = u16    (at=8)
    mask_enabled = boolean(at=10)
    mask_top     = i16    (at=12)
    mask_bottom  = i16    (at=14)
    mask_left    = i16    (at=16)
    mask_right   = i16    (at=18)

    def __repr__(self):
        return (f'<key-properties-base me={self.index}, key={self.keyer}, '
                f'type={self.type}>')


class KeyPropertiesDveField(Recv):
    """``KeDV`` — DVE-key properties (size / position / rotation /
    border / shadow / mask / rate).

    Note the scale / round-trip contract on border_hue/saturation/luma:
    WRITE (operations.py ×10) + parser (this Recv ÷10) + READ
    (state.py degrees passthrough) must multiply to 1. Don't change
    individual divisors without auditing the whole chain.
    """
    CODE = 'KeDV'
    PRETTY = 'key-properties-dve'
    KEY_FORMAT = struct.Struct('>BB')

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack('>BBxx 5i ??Bx HH BBBBBx 4HB? 4hB 3x', raw)
        self.index = field[0]
        self.keyer = field[1]

        self.size_x = field[2]
        self.size_y = field[3]
        self.pos_x = field[4]
        self.pos_y = field[5]
        self.rotation = field[6]

        self.border_enabled = field[7]
        self.shadow_enabled = field[8]
        self.border_bevel = field[9]

        self.border_outer_width = field[10]
        self.border_inner_width = field[11]

        self.border_outer_softness = field[12]
        self.border_inner_softness = field[13]
        self.border_bevel_softness = field[14]
        self.border_bevel_position = field[15]
        self.border_opacity = field[16]

        # See operations.py:798 banner — DVE border SCALE CONTRACT.
        self.border_hue = field[17] / 10.0
        self.border_saturation = field[18] / 1000.0
        self.border_luma = field[19] / 1000.0
        self.light_angle = field[20]
        self.light_altitude = field[21]
        self.mask_enabled = field[22]

        self.mask_top = field[23]
        self.mask_bottom = field[24]
        self.mask_left = field[25]
        self.mask_right = field[26]
        self.rate = field[27]

    def get_border_color_rgb(self):
        return colorsys.hls_to_rgb(
            self.border_hue / 360.0, self.border_luma, self.border_saturation,
        )

    def __repr__(self):
        return f'<key-properties-dve me={self.index}, key={self.keyer}>'


class KeyPropertiesPatternField(Recv):
    """``KePt`` — USK pattern-key properties.

    ``invert`` is bit 0 of an otherwise unused u8 at offset 14.
    """
    CODE = 'KePt'
    PRETTY = 'key-properties-pattern'
    KEY_FORMAT = struct.Struct('>BB')

    index      = u8 (at=0)
    keyer      = u8 (at=1)
    pattern    = u8 (at=2)
    size       = u16(at=4)
    symmetry   = u16(at=6)
    softness   = u16(at=8)
    position_x = u16(at=10)
    position_y = u16(at=12)
    _invert_b  = u8 (at=14)

    def __init__(self, raw: bytes):
        super().__init__(raw)
        self.invert = bool(self._invert_b & 0x01)

    def __repr__(self):
        return (f'<key-properties-pattern me={self.index} keyer={self.keyer} '
                f'pattern={self.pattern}>')


class KeyPropertiesFlyField(Recv):
    """``KeFS`` — flying-key state. Tolerant decoder for the trailing
    bitfield bytes; some firmwares omit them on certain queries.
    """
    CODE = 'KeFS'
    PRETTY = 'key-properties-fly'
    KEY_FORMAT = struct.Struct('>BB')

    def __init__(self, raw: bytes):
        self.raw = raw
        self.index = raw[0] if len(raw) > 0 else 0
        self.keyer = raw[1] if len(raw) > 1 else 0
        self.is_a_set = bool(raw[2] & 0x01) if len(raw) > 2 else False
        self.is_b_set = bool(raw[3] & 0x01) if len(raw) > 3 else False
        self.run_to_infinite_index = raw[5] if len(raw) > 5 else 0
        flag6 = raw[6] if len(raw) > 6 else 0
        self.at_keyframe_a = bool(flag6 & 0x01)
        self.at_keyframe_b = bool(flag6 & 0x02)
        self.at_keyframe_full = bool(flag6 & 0x04)
        self.at_keyframe_infinite = bool(flag6 & 0x08)

    def __repr__(self):
        return f'<key-properties-fly me={self.index} keyer={self.keyer}>'


class KeyPropertiesFlyKeyframeField(Recv):
    """``KKFP`` — flying-key keyframe geometry (one packet per stored
    keyframe A/B per keyer). 52 bytes, big-endian.

    Keyed per (me, keyer, keyframe) so keyframes A and B both persist —
    without the ``KEY_FORMAT`` below the protocol stored each packet under
    the raw code and the last one clobbered the rest.

    Geometry hardware-validated against USK1/keyer0 (Constellation HD):
    keyframe A = sizeX 370 sizeY 360 posX -13984 posY 0; keyframe B = same
    size/posX, posY 13000. Scales mirror ``KeyPropertiesDveField``
    (size/pos ×1000, rotation ×10, border widths ×100, hue ÷10, sat/luma
    ÷1000, light angle ×10, mask ×1000). Bytes 3 and 33 and 43 are
    uninitialised pads (they differ A↔B with no geometry change).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Keyer index
    2      1    u8     Keyframe (1 = A, 2 = B)
    3      1    ?      padding (uninitialised)
    4      4    u32    Size X (×1000)
    8      4    u32    Size Y (×1000)
    12     4    i32    Position X (×1000)
    16     4    i32    Position Y (×1000)
    20     4    i32    Rotation (×10)
    24     2    u16    Border outer width (×100)
    26     2    u16    Border inner width (×100)
    28     1    u8     Border outer softness
    29     1    u8     Border inner softness
    30     1    u8     Border bevel softness
    31     1    u8     Border bevel position
    32     1    u8     Border opacity
    33     1    ?      padding (uninitialised)
    34     2    u16    Border hue (×10)
    36     2    u16    Border saturation (×1000)
    38     2    u16    Border luma (×1000)
    40     2    u16    Light source direction (×10)
    42     1    u8     Light source altitude
    43     1    ?      padding (uninitialised)
    44     2    i16    Mask top (×1000)
    46     2    i16    Mask bottom (×1000)
    48     2    i16    Mask left (×1000)
    50     2    i16    Mask right (×1000)
    ====== ==== ====== ===========
    """
    CODE = 'KKFP'
    PRETTY = 'key-properties-fly-keyframe'
    KEY_FORMAT = struct.Struct('>BBB')

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack('>BBBx IIii i HH BBBBB x HHHH B x hhhh', raw)
        self.index = field[0]
        self.keyer = field[1]
        self.keyframe = field[2]          # 1 = A, 2 = B
        self.size_x = field[3]
        self.size_y = field[4]
        self.pos_x = field[5]
        self.pos_y = field[6]
        self.rotation = field[7]
        self.border_outer_width = field[8]
        self.border_inner_width = field[9]
        self.border_outer_softness = field[10]
        self.border_inner_softness = field[11]
        self.border_bevel_softness = field[12]
        self.border_bevel_position = field[13]
        self.border_opacity = field[14]
        # Mirror KeyPropertiesDveField's hue/sat/luma scaling so the reader
        # below stays identical to usk_dve.
        self.border_hue = field[15] / 10.0
        self.border_saturation = field[16] / 1000.0
        self.border_luma = field[17] / 1000.0
        self.light_angle = field[18]
        self.light_altitude = field[19]
        self.mask_top = field[20]
        self.mask_bottom = field[21]
        self.mask_left = field[22]
        self.mask_right = field[23]

    def __repr__(self):
        kf = {1: 'A', 2: 'B'}.get(self.keyframe, self.keyframe)
        return (f'<key-properties-fly-keyframe me={self.index} '
                f'keyer={self.keyer} keyframe={kf}>')


class KeyPropertiesLumaField(Recv):
    """``KeLm`` — USK luma-key properties.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Keyer index
    2      1    bool   Premultiplied
    3      1    ?      padding
    4      2    u16    Clip
    6      2    u16    Gain
    8      1    bool   Key inverted
    9      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'KeLm'
    PRETTY = 'key-properties-luma'
    KEY_FORMAT = struct.Struct('>BB')

    index         = u8     (at=0)
    keyer         = u8     (at=1)
    premultiplied = boolean(at=2)
    clip          = u16    (at=4)
    gain          = u16    (at=6)
    key_inverted  = boolean(at=8)

    def __repr__(self):
        return f'<key-properties-luma me={self.index}, key={self.keyer}>'


class KeyPropertiesAdvancedChromaField(Recv):
    """``KACk`` — advanced chroma keyer state."""
    CODE = 'KACk'
    PRETTY = 'key-properties-advanced-chroma'
    KEY_FORMAT = struct.Struct('>BB')

    index          = u8 (at=0)
    keyer          = u8 (at=1)
    foreground     = u16(at=2)
    background     = u16(at=4)
    key_edge       = u16(at=6)
    spill_suppress = u16(at=8)
    flare_suppress = u16(at=10)
    brightness     = i16(at=12)
    contrast       = i16(at=14)
    saturation     = u16(at=16)
    red            = i16(at=18)
    green          = i16(at=20)
    blue           = i16(at=22)

    def __repr__(self):
        return (f'<key-properties-advanced-chroma me={self.index}, '
                f'key={self.keyer}>')


class KeyPropertiesAdvancedChromaColorpickerField(Recv):
    """``KACC`` — advanced chroma colorpicker state.

    Y/Cb/Cr have non-linear conversions to display 0..1 range; preserve
    the original arithmetic via custom ``__init__``.
    """
    CODE = 'KACC'
    PRETTY = 'key-properties-advanced-chroma-colorpicker'
    KEY_FORMAT = struct.Struct('>BB')

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack('>BB?? hhH HHH', raw)
        self.index = field[0]
        self.keyer = field[1]
        self.cursor = field[2]
        self.preview = field[3]

        self.x = field[4]
        self.y = field[5]
        self.size = field[6]

        self.Y = (field[7] - 625) / 8544
        self.Cb = (field[8] - 5000) / 5000
        self.Cr = (field[9] - 5000) / 5000

    def get_rgb(self):
        r = self.Y + (self.Cr * 1.5748)
        g = self.Y + (self.Cb * -0.1873) + (self.Cr * -0.4681)
        b = self.Y + (self.Cb * 1.8556)
        r = max(0, min(1, r))
        g = max(0, min(1, g))
        b = max(0, min(1, b))
        return r, g, b

    def __repr__(self):
        return (f'<key-properties-advanced-chroma-colorpicker me={self.index}, '
                f'key={self.keyer}>')


# =============================================================================
# Operations + readers — base + luma (sub-commit 5a)
# =============================================================================
#
# The upstream_keyer batch lands in 6 sub-commits following the per-key-type
# sub-groupings: 5a base + luma, 5b chroma, 5c dve, 5d pattern, 5e mask,
# 5f fly. Each sub-commit gains the wrappers and readers for its group.

# Helper: rate parser shared by USK DVE rate (sub-commit 5c). Defined here
# rather than per-sub-phase to avoid duplicating across the file.


def _resolve_rate(conn, rate_str):
    return parse_rate(rate_str, display_fps(conn.mixerstate))


# -----------------------------------------------------------------------------
# Operations — base (on-air / type / fill / cut)
# -----------------------------------------------------------------------------


def set_usk_on_air(conn, keyer, enabled, me=0):
    """Absolute USK on-air set. Use ``toggle_usk`` for a flip."""
    conn.send(KeyOnAirCommand(
        index=me, keyer=int(keyer), enabled=bool(enabled),
    ))


def toggle_usk(conn, keyer, me=0):
    """Flip USK on-air state. Compound RMW: reads current on-air,
    delegates to ``set_usk_on_air`` with the inverted value."""
    current = usk_on_air(conn.mixerstate, me, int(keyer))
    set_usk_on_air(conn, keyer=keyer, enabled=not current, me=me)


def set_usk_type(conn, keyer, key_type, me=0):
    conn.send(KeyTypeCommand(
        index=me, keyer=int(keyer), type=int(key_type),
    ))


def set_usk_fill_source(conn, keyer, source, me=0):
    conn.send(KeyFillCommand(
        index=me, keyer=int(keyer), source=int(source),
    ))


def set_usk_key_source(conn, keyer, source, me=0):
    conn.send(KeyCutCommand(
        index=me, keyer=int(keyer), source=int(source),
    ))


# -----------------------------------------------------------------------------
# Operations — luma key
# -----------------------------------------------------------------------------


def set_usk_luma_clip(conn, keyer, clip, me=0):
    conn.send(KeyPropertiesLumaCommand(
        index=me, keyer=int(keyer), clip=percent_to_tenths(clip),
    ))


def set_usk_luma_gain(conn, keyer, gain, me=0):
    conn.send(KeyPropertiesLumaCommand(
        index=me, keyer=int(keyer), gain=percent_to_tenths(gain),
    ))


def set_usk_luma_invert(conn, keyer, invert, me=0):
    conn.send(KeyPropertiesLumaCommand(
        index=me, keyer=int(keyer), invert_key=bool(invert),
    ))


def set_usk_luma_pre_multiplied(conn, keyer, pre_multiplied, me=0):
    conn.send(KeyPropertiesLumaCommand(
        index=me, keyer=int(keyer), premultiplied=bool(pre_multiplied),
    ))


def configure_usk_luma(conn, keyer, *, clip=None, gain=None,
                      pre_multiplied=None, invert=None, me=0):
    """Set multiple USK luma fields atomically in one packet.

    Each kwarg is optional; only kwargs that are not None are included
    in the CKLm packet's field mask, leaving other fields on the ATEM
    unchanged.

    Scale fix (2026-06-02): clip/gain now use ``percent_to_tenths``
    (×10, clamp 0..1000) — consistent with the single setters
    ``set_usk_luma_clip`` / ``set_usk_luma_gain``. The wire field is
    u16 0..1000 (tenths of a percent), so e.g. clip=22 → wire 220 =
    22.0%. Previously this used ``percent_to_thousandths`` (×100), which
    sent 2200 → the ATEM clamped to 1000 = 100%, so any value ≳10%
    landed at max (e.g. ``configure_usk_luma(clip=22, gain=30)``)."""
    conn.send(KeyPropertiesLumaCommand(
        index=me, keyer=int(keyer),
        premultiplied=None if pre_multiplied is None else bool(pre_multiplied),
        clip=None if clip is None else percent_to_tenths(clip),
        gain=None if gain is None else percent_to_tenths(gain),
        invert_key=None if invert is None else bool(invert),
    ))


# -----------------------------------------------------------------------------
# Readers — base + luma
# -----------------------------------------------------------------------------


def usk_on_air(mx, me, k):
    return safe_bool(_kv(mx, 'key-on-air', me, k, attr='enabled'), False)


def usk_type(mx, me, k):
    return safe_int(_kv(mx, 'key-properties-base', me, k, attr='type'), 0)


def usk_fill_source(mx, me, k):
    return safe_int(
        _kv(mx, 'key-properties-base', me, k, attr='fill_source'), 0)


def usk_key_source(mx, me, k):
    return safe_int(
        _kv(mx, 'key-properties-base', me, k, attr='key_source'), 0)


def usk_luma(mx, me, k):
    """Bucket B (structured 4-field dict). ATEM emits clip/gain wire in
    tenths of percent (0..1000 = 0..100%); value_from_tenths handles
    the scale. Default-empty dict when key-properties-luma hasn't
    arrived for this keyer."""
    node = _kv(mx, 'key-properties-luma', me, k)
    if node is None:
        return {
            'luma_clip': 0.0, 'luma_gain': 0.0,
            'luma_invert': False, 'luma_pre_multiplied': False,
        }
    return {
        'luma_clip': value_from_tenths(getattr(node, 'clip', 0), 0.0),
        'luma_gain': value_from_tenths(getattr(node, 'gain', 0), 0.0),
        'luma_invert': safe_bool(getattr(node, 'key_inverted', False), False),
        'luma_pre_multiplied': safe_bool(
            getattr(node, 'premultiplied', False), False),
    }


# =============================================================================
# Chroma — advanced chroma + colorpicker + color correction (sub-commit 5b)
# =============================================================================
#
# All five chroma key fields (foreground/background/key_edge/spill/
# flare_suppression) and the six color-correction fields (brightness/
# contrast/saturation/red/green/blue) share a ×1000 wire scale (display
# unit × 1000 → wire). Earlier code used percent_to_thousandths (×100)
# which was off by 10×; fix verified empirically against ATEM Software
# Control 2026-05-04 (Bug B).

# Sample size spec range — clamped on writes so an out-of-spec
# operator-typed value snaps to the smallest visible cursor box.

_CHROMA_SAMPLE_SIZE_WIRE_MIN = 620
_CHROMA_SAMPLE_SIZE_WIRE_MAX = 9925


# -----------------------------------------------------------------------------
# Operations — advanced chroma (KACk)
# -----------------------------------------------------------------------------


def set_usk_chroma_foreground(conn, keyer, foreground, me=0):
    conn.send(KeyPropertiesAdvancedChromaCommand(
        index=me, keyer=int(keyer),
        foreground=value_to_thousandths(foreground)))


def set_usk_chroma_background(conn, keyer, background, me=0):
    conn.send(KeyPropertiesAdvancedChromaCommand(
        index=me, keyer=int(keyer),
        background=value_to_thousandths(background)))


def set_usk_chroma_key_edge(conn, keyer, key_edge, me=0):
    conn.send(KeyPropertiesAdvancedChromaCommand(
        index=me, keyer=int(keyer),
        key_edge=value_to_thousandths(key_edge)))


def set_usk_chroma_spill(conn, keyer, spill, me=0):
    conn.send(KeyPropertiesAdvancedChromaCommand(
        index=me, keyer=int(keyer),
        spill=value_to_thousandths(spill)))


def set_usk_chroma_flare_suppression(conn, keyer, flare_suppression, me=0):
    conn.send(KeyPropertiesAdvancedChromaCommand(
        index=me, keyer=int(keyer),
        flare=value_to_thousandths(flare_suppression)))


# -----------------------------------------------------------------------------
# Operations — color correction (also on KACk)
# -----------------------------------------------------------------------------


def set_usk_chroma_brightness(conn, keyer, brightness, me=0):
    conn.send(KeyPropertiesAdvancedChromaCommand(
        index=me, keyer=int(keyer),
        brightness=value_to_thousandths(brightness)))


def set_usk_chroma_contrast(conn, keyer, contrast, me=0):
    conn.send(KeyPropertiesAdvancedChromaCommand(
        index=me, keyer=int(keyer),
        contrast=value_to_thousandths(contrast)))


def set_usk_chroma_saturation(conn, keyer, saturation, me=0):
    conn.send(KeyPropertiesAdvancedChromaCommand(
        index=me, keyer=int(keyer),
        saturation=value_to_thousandths(saturation)))


def set_usk_chroma_red(conn, keyer, red, me=0):
    conn.send(KeyPropertiesAdvancedChromaCommand(
        index=me, keyer=int(keyer),
        red=value_to_thousandths(red)))


def set_usk_chroma_green(conn, keyer, green, me=0):
    conn.send(KeyPropertiesAdvancedChromaCommand(
        index=me, keyer=int(keyer),
        green=value_to_thousandths(green)))


def set_usk_chroma_blue(conn, keyer, blue, me=0):
    conn.send(KeyPropertiesAdvancedChromaCommand(
        index=me, keyer=int(keyer),
        blue=value_to_thousandths(blue)))


# -----------------------------------------------------------------------------
# Operations — chroma colorpicker (KACC)
# -----------------------------------------------------------------------------


def set_usk_chroma_sample(conn, keyer, enabled, me=0):
    conn.send(KeyPropertiesAdvancedChromaColorpickerCommand(
        index=me, keyer=int(keyer), cursor=bool(enabled)))


def set_usk_chroma_sample_position(conn, keyer, x, y, me=0):
    """Frontend sends normalized 0..1; atemwire expects raw signed wire units.

    Per the CACC packet spec: cursor X is i16 ±16000 (16:9 frame width
    proportion), cursor Y is i16 ±9000 (frame height). Bucket B
    because of the bias+scale conversion and the dual-output shape."""
    x_raw = int(round((float(x) - 0.5) * 32000))
    y_raw = int(round((float(y) - 0.5) * 18000))
    conn.send(KeyPropertiesAdvancedChromaColorpickerCommand(
        index=me, keyer=int(keyer), x=x_raw, y=y_raw))


def set_usk_chroma_sample_size(conn, keyer, size, me=0):
    """Sample size: ATEM Software Control renders ``wire / 10000`` of frame
    width. Operator-settable wire range is 620..9925 (smallest visible
    cursor up to nearly-full-frame). We accept a 0..1 frontend value
    and clamp to the spec range so a typed 5% snaps to the smallest
    valid wire. Bucket B (asymmetric clamp)."""
    wire = int(round(float(size) * 10000))
    wire = max(_CHROMA_SAMPLE_SIZE_WIRE_MIN,
               min(_CHROMA_SAMPLE_SIZE_WIRE_MAX, wire))
    conn.send(KeyPropertiesAdvancedChromaColorpickerCommand(
        index=me, keyer=int(keyer), size=wire))


def set_usk_chroma_preview(conn, keyer, preview, me=0):
    conn.send(KeyPropertiesAdvancedChromaColorpickerCommand(
        index=me, keyer=int(keyer), preview=bool(preview)))


def set_usk_chroma_sampled_color(conn, keyer, *, y, cb, cr, me=0):
    """Write the sampled key colour (CACC Y/Cb/Cr) directly.

    The live UI never calls this — the ATEM samples Y/Cb/Cr from the
    video under the cursor. Profile restore needs it so the saved chroma
    key reproduces without the original video present. ``y`` / ``cb`` /
    ``cr`` are 0..1 unit (wire ÷ 10000), the inverse of the value
    ``usk_chroma`` stores under ``chroma_sampled_color['*_raw'] / 10000``.

    NOTE: if the cursor is left enabled and pointed at live video, the
    ATEM may re-sample and overwrite these. Restore order (size/position
    before colour) and the operator's keyer config determine whether the
    written colour sticks."""
    conn.send(KeyPropertiesAdvancedChromaColorpickerCommand(
        index=me, keyer=int(keyer),
        Y=int(round(float(y) * 10000)),
        Cb=int(round(float(cb) * 10000)),
        Cr=int(round(float(cr) * 10000))))


# -----------------------------------------------------------------------------
# Reader — usk_chroma (large aggregator across KACk + KACC)
# -----------------------------------------------------------------------------


def usk_chroma(mx, me, k):
    """Advanced chroma (KACk) + colorpicker (KACC). atemwire doesn't split
    these into PyATEMMax's "legacy + advanced" dual surface — there's
    just the advanced data, so legacy-named keys we still need to emit
    for the frontend get sensible defaults.

    Wire scales (per earlier Bug B fix 2026-05-04, verified against
    ATEM Software Control):
        foreground / background / key_edge / spill / flare → ×1000
        brightness / contrast / saturation / red / green / blue → ×1000

    places=3 on the unit values preserves 1-decimal-percent precision
    through the unit→percent multiplication that happens on the
    frontend display (e.g. wire 837 → unit 0.837 → percent 83.7; if
    we rounded to 2 places we'd get unit 0.84 → percent 84.0)."""
    adv = _kv(mx, 'key-properties-advanced-chroma', me, k)
    cp = _kv(mx, 'key-properties-advanced-chroma-colorpicker', me, k)

    data = {
        'chroma_hue': 0.0, 'chroma_gain': 0.0, 'chroma_lift': 0.0,
        'chroma_narrow': False, 'chroma_y_suppress': 0.0,
        'chroma_foreground': 0.0, 'chroma_background': 0.0,
        'chroma_key_edge': 0.0,
        'chroma_spill': 0.0, 'chroma_flare_suppression': 0.0,
        'chroma_sample': False, 'chroma_preview': False,
        'chroma_sample_size': 0.1,
        'chroma_sample_position': {'x': 0.5, 'y': 0.5},
        'chroma_sampled_color': {
            'y_raw': 0, 'cb_raw': 5000, 'cr_raw': 5000,
            'y': 0.0, 'cb': 0.0, 'cr': 0.0,
        },
        'chroma_brightness': 0.0, 'chroma_contrast': 0.0,
        'chroma_saturation': 1.0,
        'chroma_red': 0.0, 'chroma_green': 0.0, 'chroma_blue': 0.0,
    }

    if adv is not None:
        data['chroma_foreground'] = value_from_thousandths(
            getattr(adv, 'foreground', 0), 0.0, 3)
        data['chroma_background'] = value_from_thousandths(
            getattr(adv, 'background', 0), 0.0, 3)
        data['chroma_key_edge'] = value_from_thousandths(
            getattr(adv, 'key_edge', 0), 0.0, 3)
        data['chroma_spill'] = value_from_thousandths(
            getattr(adv, 'spill_suppress', 0), 0.0, 3)
        data['chroma_flare_suppression'] = value_from_thousandths(
            getattr(adv, 'flare_suppress', 0), 0.0, 3)
        data['chroma_brightness'] = value_from_thousandths(
            getattr(adv, 'brightness', 0), 0.0, 3)
        data['chroma_contrast'] = value_from_thousandths(
            getattr(adv, 'contrast', 0), 0.0, 3)
        data['chroma_saturation'] = value_from_thousandths(
            getattr(adv, 'saturation', 1000), 1.0, 3)
        data['chroma_red'] = value_from_thousandths(
            getattr(adv, 'red', 0), 0.0, 3)
        data['chroma_green'] = value_from_thousandths(
            getattr(adv, 'green', 0), 0.0, 3)
        data['chroma_blue'] = value_from_thousandths(
            getattr(adv, 'blue', 0), 0.0, 3)

    if cp is not None:
        data['chroma_sample'] = safe_bool(
            getattr(cp, 'cursor', False), False)
        data['chroma_preview'] = safe_bool(
            getattr(cp, 'preview', False), False)
        size_raw = safe_int(getattr(cp, 'size', 1000), 1000)
        data['chroma_sample_size'] = round(size_raw / 10000.0, 4)
        # x/y wire is signed i16. Per CACC spec: X ±16000 (16:9 frame
        # width proportion), Y ±9000 (frame height). Map to 0..1 unit
        # by (wire + half_range) / full_range. Symmetric to
        # set_usk_chroma_sample_position above.
        x_raw = safe_int(getattr(cp, 'x', 0), 0)
        y_raw = safe_int(getattr(cp, 'y', 0), 0)
        data['chroma_sample_position'] = {
            'x': round(max(0.0, min(1.0, (x_raw + 16000) / 32000.0)), 4),
            'y': round(max(0.0, min(1.0, (y_raw + 9000) / 18000.0)), 4),
        }
        Y = safe_float(getattr(cp, 'Y', 0.0), 0.0, 4)
        Cb = safe_float(getattr(cp, 'Cb', 0.0), 0.0, 4)
        Cr = safe_float(getattr(cp, 'Cr', 0.0), 0.0, 4)
        data['chroma_sampled_color'] = {
            'y_raw': safe_int(getattr(cp, 'Y', 0) * 8544 + 625, 0),
            'cb_raw': safe_int(getattr(cp, 'Cb', 0) * 5000 + 5000, 5000),
            'cr_raw': safe_int(getattr(cp, 'Cr', 0) * 5000 + 5000, 5000),
            'y': Y, 'cb': Cb, 'cr': Cr,
        }

    return data


# =============================================================================
# DVE — position, size, rotation, border, mask, light, shadow, rate
# (sub-commit 5c)
# =============================================================================
#
# Wire units per KeyPropertiesDveCommand packet layout:
#   pos_x          — i32 ×1000 of display, clamped [-16000, 16000]
#   pos_y          — i32 ×1000 of display, clamped [-9000, 9000]
#   size_x/size_y  — u16 ×1000 of display (0..2 unit → 0..2000 wire)
#   rotation       — u16 ×10 of degrees (0..3599 wire → 0..359.9°)
#   border_*_width — u16 ×100 of display (0..1600 wire ↔ 0..16.00 display)
#   border_*_softness / bevel_softness / bevel_position / opacity — u8 0..100
#   angle (shadow direction) — u16 0..3590 (×10 degrees)
#   altitude       — u8 10..100
#   mask_top / bottom — i16 ±9000 (display ±9.0 × 1000)
#   mask_left / right — i16 ±16000 (display ±16.0 × 1000)
#   rate           — u8 frames
#
# ---------------------------------------------------------------------------
# DVE border hue / saturation / luma — SCALE CONTRACT (DO NOT BREAK)
# ---------------------------------------------------------------------------
# All three setters use WRITE ×10. Round-trip closure depends on three
# different factors at three different sites that must multiply to 1:
#
#   site                           hue       sat       luma
#   ──────────────────────────────────────────────────────────
#   WRITE (here)                   ×10       ×10       ×10
#   atemwire field parser            ÷10       ÷1000     ÷1000     ← field.py
#   READ                           identity  ×100      ×100      ← below
#                                  ───────   ───────   ───────
#   net (must = 1)                 1         1         1
#
# WHY THE ASYMMETRY: atemwire's parser already normalizes sat/luma to a
# 0..1 unit scalar (÷1000). state-side ``percent_from_unit`` then
# converts unit→percent (×100). Hue's parser delivers degrees directly
# (÷10), so the reader passes through. The WRITE ×10 is matched to the
# parser, NOT to other percent fields like wipe softness (which is
# wire ×100). DO NOT "fix" the WRITE to ×100 to match other percent
# helpers — sat and luma will silently scale by 10×.

# -----------------------------------------------------------------------------
# Operations — position / size / rotation
# -----------------------------------------------------------------------------


# Fly/DVE position wire field is i32 (×1000 of display). On-screen DVE sits
# within ~±16000 (x) / ±9000 (y), but fly keyframes position the key far
# off-screen — the profile save observed wire -30000. Restore values are
# ATEM-sourced (read back as wire/1000) so they're valid by construction;
# clamp only to the i32 field capacity to avoid a struct overflow, never to
# an on-screen range. Live control stays bounded by the UI slider, so the
# wide cap is safe there too.
_DVE_POS_WIRE_MIN = -(2 ** 31)
_DVE_POS_WIRE_MAX = 2 ** 31 - 1


def set_usk_dve_position_x(conn, keyer, position_x, me=0):
    """Wire is i32, ×1000 of display. Clamped only to the i32 field range
    (see ``_DVE_POS_WIRE_*``) so off-screen fly positions survive restore."""
    val = max(_DVE_POS_WIRE_MIN,
              min(_DVE_POS_WIRE_MAX, int(round(float(position_x) * 1000))))
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer), pos_x=val,
    ))


def set_usk_dve_position_y(conn, keyer, position_y, me=0):
    """Wire is i32, ×1000 of display. Clamped only to the i32 field range —
    see ``set_usk_dve_position_x``."""
    val = max(_DVE_POS_WIRE_MIN,
              min(_DVE_POS_WIRE_MAX, int(round(float(position_y) * 1000))))
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer), pos_y=val,
    ))


def set_usk_dve_size_x(conn, keyer, size_x, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer), size_x=size_thousandths(size_x),
    ))


def set_usk_dve_size_y(conn, keyer, size_y, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer), size_y=size_thousandths(size_y),
    ))


def set_usk_dve_rotation(conn, keyer, rotation, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        rotation=int(round(float(rotation) * 10)),
    ))


# -----------------------------------------------------------------------------
# Operations — border (enabled + 11 fields)
# -----------------------------------------------------------------------------


def set_usk_dve_border_enabled(conn, keyer, enabled, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer), border_enabled=bool(enabled),
    ))


def set_usk_dve_border_hue(conn, keyer, hue, me=0):
    """See the SCALE CONTRACT banner above. ×10 WRITE matches the parser."""
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        border_hue=int(round(float(hue) * 10)),
    ))


def set_usk_dve_border_saturation(conn, keyer, saturation, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        border_saturation=int(round(float(saturation) * 10)),
    ))


def set_usk_dve_border_luma(conn, keyer, luma, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        border_luma=int(round(float(luma) * 10)),
    ))


def set_usk_dve_border_opacity(conn, keyer, opacity, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        border_opacity=int(round(float(opacity))),
    ))


def set_usk_dve_border_outer_width(conn, keyer, outer_width, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        outer_width=int(round(float(outer_width) * 100)),
    ))


def set_usk_dve_border_inner_width(conn, keyer, inner_width, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        inner_width=int(round(float(inner_width) * 100)),
    ))


def set_usk_dve_border_outer_softness(conn, keyer, outer_softness, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        outer_softness=int(round(float(outer_softness))),
    ))


def set_usk_dve_border_inner_softness(conn, keyer, inner_softness, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        inner_softness=int(round(float(inner_softness))),
    ))


def set_usk_dve_border_bevel_softness(conn, keyer, bevel_softness, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        bevel_softness=int(round(float(bevel_softness))),
    ))


def set_usk_dve_border_bevel_position(conn, keyer, bevel_position, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        bevel_position=int(round(float(bevel_position))),
    ))


# -----------------------------------------------------------------------------
# Operations — DVE mask + light + shadow + rate
# -----------------------------------------------------------------------------


def set_usk_dve_masked(conn, keyer, masked, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer), mask_enabled=bool(masked),
    ))


def set_usk_dve_top(conn, keyer, top, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        mask_top=int(round(float(top) * 1000)),
    ))


def set_usk_dve_bottom(conn, keyer, bottom, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        mask_bottom=int(round(float(bottom) * 1000)),
    ))


def set_usk_dve_left(conn, keyer, left, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        mask_left=int(round(float(left) * 1000)),
    ))


def set_usk_dve_right(conn, keyer, right, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        mask_right=int(round(float(right) * 1000)),
    ))


def set_usk_dve_light_direction(conn, keyer, direction, me=0):
    """Light angle wire is u16 0..3590 (×10 of degrees)."""
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        angle=int(round(float(direction) * 10)),
    ))


def set_usk_dve_light_altitude(conn, keyer, altitude, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        altitude=int(round(float(altitude))),
    ))


def set_usk_dve_shadow(conn, keyer, shadow, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer), shadow_enabled=bool(shadow),
    ))


def set_usk_dve_rate(conn, keyer, rate_str, me=0):
    conn.send(KeyPropertiesDveCommand(
        index=me, keyer=int(keyer),
        rate=_resolve_rate(conn, rate_str),
    ))


# -----------------------------------------------------------------------------
# Reader — usk_dve (large aggregator)
# -----------------------------------------------------------------------------


def usk_dve(mx, me, k):
    """DVE block reader. Aggregates 26 fields out of key-properties-dve
    with per-field scale (pos/size ÷1000, rotation ÷10, border hue
    identity-from-parser, sat/luma percent_from_unit, widths
    percent_from_thousandths, ...). See the DVE SCALE CONTRACT banner
    near the top of this section.

    Default-shape return when key-properties-dve hasn't arrived
    keeps build_full_state stable through the handshake window."""
    node = _kv(mx, 'key-properties-dve', me, k)
    if node is None:
        return {
            'dve_position_x': 0.0, 'dve_position_y': 0.0,
            'dve_size_x': 1.0, 'dve_size_y': 1.0, 'dve_rotation': 0.0,
            'dve_masked': False, 'dve_top': 0.0, 'dve_bottom': 0.0,
            'dve_left': 0.0, 'dve_right': 0.0,
            'dve_border_enabled': False, 'dve_border_hue': 0.0,
            'dve_border_saturation': 0.0, 'dve_border_luma': 0.0,
            'dve_border_opacity': 100.0,
            'dve_border_outer_width': 0.0, 'dve_border_inner_width': 0.0,
            'dve_border_outer_softness': 0.0, 'dve_border_inner_softness': 0.0,
            'dve_border_bevel_type': 0, 'dve_border_bevel_position': 0.0,
            'dve_border_bevel_softness': 0.0,
            'dve_light_direction': 0.0, 'dve_light_altitude': 0.0,
            'dve_shadow': False, 'dve_rate': 25.0,
        }
    return {
        'dve_position_x': safe_float(getattr(node, 'pos_x', 0) / 1000.0, 0.0, 3),
        'dve_position_y': safe_float(getattr(node, 'pos_y', 0) / 1000.0, 0.0, 3),
        'dve_size_x': safe_float(getattr(node, 'size_x', 0) / 1000.0, 1.0, 3),
        'dve_size_y': safe_float(getattr(node, 'size_y', 0) / 1000.0, 1.0, 3),
        'dve_rotation': safe_float(getattr(node, 'rotation', 0) / 10.0, 0.0, 2),
        'dve_masked': safe_bool(getattr(node, 'mask_enabled', False), False),
        'dve_top': safe_float(getattr(node, 'mask_top', 0) / 1000.0, 0.0, 3),
        'dve_bottom': safe_float(getattr(node, 'mask_bottom', 0) / 1000.0, 0.0, 3),
        'dve_left': safe_float(getattr(node, 'mask_left', 0) / 1000.0, 0.0, 3),
        'dve_right': safe_float(getattr(node, 'mask_right', 0) / 1000.0, 0.0, 3),
        'dve_border_enabled': safe_bool(
            getattr(node, 'border_enabled', False), False),
        # DVE border SCALE CONTRACT — READ here is identity for hue
        # (parser already produced degrees) and percent_from_unit (×100)
        # for sat/luma (parser produced 0..1 unit).
        'dve_border_hue': safe_float(getattr(node, 'border_hue', 0.0), 0.0, 2),
        'dve_border_saturation': percent_from_unit(
            getattr(node, 'border_saturation', 0.0), 0.0),
        'dve_border_luma': percent_from_unit(
            getattr(node, 'border_luma', 0.0), 0.0),
        'dve_border_opacity': safe_float(
            getattr(node, 'border_opacity', 0), 100.0),
        # Border width wire is hundredths of the displayed value (wire
        # 0..1600 ↔ display 0..16.00).
        'dve_border_outer_width': percent_from_thousandths(
            getattr(node, 'border_outer_width', 0), 0.0),
        'dve_border_inner_width': percent_from_thousandths(
            getattr(node, 'border_inner_width', 0), 0.0),
        'dve_border_outer_softness': safe_float(
            getattr(node, 'border_outer_softness', 0), 0.0),
        'dve_border_inner_softness': safe_float(
            getattr(node, 'border_inner_softness', 0), 0.0),
        'dve_border_bevel_type': safe_int(getattr(node, 'border_bevel', 0), 0),
        'dve_border_bevel_position': safe_float(
            getattr(node, 'border_bevel_position', 0), 0.0),
        'dve_border_bevel_softness': safe_float(
            getattr(node, 'border_bevel_softness', 0), 0.0),
        # light_angle wire is degrees × 10 (range 0..3590 for 0..359°).
        'dve_light_direction': safe_float(
            getattr(node, 'light_angle', 0), 0.0) / 10.0,
        'dve_light_altitude': safe_float(
            getattr(node, 'light_altitude', 0), 0.0),
        'dve_shadow': safe_bool(
            getattr(node, 'shadow_enabled', False), False),
        'dve_rate': safe_float(getattr(node, 'rate', 25), 25.0),
    }


# =============================================================================
# Pattern (sub-commit 5d)
# =============================================================================
#
# Wire units for KeyPropertiesPatternCommand (CKPt):
#   size / symmetry / softness:   u16 0..10000 (frontend 0..100 → ×100)
#   position_x / position_y:      u16 0..10000 (frontend 0..1.0 → ×10000)
#
# RESOLVED / NON-BUG — set_usk_pattern_size pattern-size scale.
# Investigated 2026-06-10 and confirmed self-consistent vs ATEM Software
# Control; do NOT "fix" this to ×10. Detail below:
#
#   set_usk_pattern_size / _symmetry / _softness all use
#   percent_to_thousandths (×100). The wire field is u16 0..10000, so
#   percent values 0..100 produce wire 0..10000 — which looks correct.
#   BUT the read side at usk_pattern below uses percent_from_thousandths
#   (÷100), which symmetrically maps wire 0..10000 back to display
#   0..100 percent. The round-trip is self-consistent and externally
#   matches ATEM Software Control's display for these three fields.
#
#   The "10× workaround" name refers to a historical state
#   where the write side was ×10 of the eventual wire ×100, and the
#   read side compensated with another ×10. Both sides now use ×100
#   directly. Preserving the current behaviour is the rule; no fix
#   needed at this site.

# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def set_usk_pattern_style(conn, keyer, pattern, me=0):
    conn.send(KeyPropertiesPatternCommand(
        index=me, keyer=int(keyer), pattern=int(pattern),
    ))


def set_usk_pattern_size(conn, keyer, size, me=0):
    """Frontend 0..100 percent → wire 0..10000 (×100). See the LATENT
    BUG banner above for the workaround-history context."""
    conn.send(KeyPropertiesPatternCommand(
        index=me, keyer=int(keyer),
        size=percent_to_thousandths(size),
    ))


def set_usk_pattern_symmetry(conn, keyer, symmetry, me=0):
    conn.send(KeyPropertiesPatternCommand(
        index=me, keyer=int(keyer),
        symmetry=percent_to_thousandths(symmetry),
    ))


def set_usk_pattern_softness(conn, keyer, softness, me=0):
    conn.send(KeyPropertiesPatternCommand(
        index=me, keyer=int(keyer),
        softness=percent_to_thousandths(softness),
    ))


def set_usk_pattern_invert(conn, keyer, invert, me=0):
    conn.send(KeyPropertiesPatternCommand(
        index=me, keyer=int(keyer), invert_pattern=bool(invert),
    ))


def set_usk_pattern_position_x(conn, keyer, position_x, me=0):
    """Position is 0..1 unit → 0..10000 wire (×10000)."""
    conn.send(KeyPropertiesPatternCommand(
        index=me, keyer=int(keyer),
        position_x=unit_to_thousandths(position_x),
    ))


def set_usk_pattern_position_y(conn, keyer, position_y, me=0):
    conn.send(KeyPropertiesPatternCommand(
        index=me, keyer=int(keyer),
        position_y=unit_to_thousandths(position_y),
    ))


# -----------------------------------------------------------------------------
# Reader
# -----------------------------------------------------------------------------


def usk_pattern(mx, me, k):
    """Pattern key state from key-properties-pattern. Bucket B
    (structured 7-field dict). Defaults preserve the legacy shape
    PyATEMMax exposed: size/symmetry default 50.0, softness 0.0,
    positions 0.5."""
    node = _kv(mx, 'key-properties-pattern', me, k)
    if node is None:
        return {
            'pattern_style': 0, 'pattern_size': 50.0,
            'pattern_symmetry': 50.0, 'pattern_softness': 0.0,
            'pattern_invert': False,
            'pattern_position_x': 0.5, 'pattern_position_y': 0.5,
        }
    return {
        'pattern_style': safe_int(getattr(node, 'pattern', 0), 0),
        'pattern_size': percent_from_thousandths(
            getattr(node, 'size', 0), 50.0),
        'pattern_symmetry': percent_from_thousandths(
            getattr(node, 'symmetry', 0), 50.0),
        'pattern_softness': percent_from_thousandths(
            getattr(node, 'softness', 0), 0.0),
        'pattern_invert': safe_bool(getattr(node, 'invert', False), False),
        'pattern_position_x': unit_from_thousandths(
            getattr(node, 'position_x', 5000), 0.5),
        'pattern_position_y': unit_from_thousandths(
            getattr(node, 'position_y', 5000), 0.5),
    }


# =============================================================================
# Mask — KeyPropertiesMaskCommand / CKMs (sub-commit 5e)
# =============================================================================
#
# USK mask wire is fixed-point ×1000 (e.g. 9000 = 9.000), same scale as
# DVE mask and DSK mask. Per-field setters write ×1000 explicitly via
# int(round(value * 1000)); no clamp at the wrapper layer (the wire is
# i16 ±32767 which comfortably covers the operator-facing ±9 / ±16
# display ranges).

# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def set_usk_mask_enabled(conn, keyer, enabled, me=0):
    conn.send(KeyPropertiesMaskCommand(
        index=me, keyer=int(keyer), enabled=bool(enabled),
    ))


def set_usk_mask_top(conn, keyer, top, me=0):
    conn.send(KeyPropertiesMaskCommand(
        index=me, keyer=int(keyer),
        top=int(round(float(top) * 1000)),
    ))


def set_usk_mask_bottom(conn, keyer, bottom, me=0):
    conn.send(KeyPropertiesMaskCommand(
        index=me, keyer=int(keyer),
        bottom=int(round(float(bottom) * 1000)),
    ))


def set_usk_mask_left(conn, keyer, left, me=0):
    conn.send(KeyPropertiesMaskCommand(
        index=me, keyer=int(keyer),
        left=int(round(float(left) * 1000)),
    ))


def set_usk_mask_right(conn, keyer, right, me=0):
    conn.send(KeyPropertiesMaskCommand(
        index=me, keyer=int(keyer),
        right=int(round(float(right) * 1000)),
    ))


# -----------------------------------------------------------------------------
# Reader
# -----------------------------------------------------------------------------


def usk_mask(mx, me, k):
    """Mask parameters live on key-properties-base (PyATEMMax put them
    on the keyer object). Wire is fixed-point ×1000 (e.g. 9000 = 9.000),
    same scale as DVE mask and DSK mask."""
    node = _kv(mx, 'key-properties-base', me, k)
    if node is None:
        return {
            'mask_enabled': False, 'mask_top': 0.0, 'mask_bottom': 0.0,
            'mask_left': 0.0, 'mask_right': 0.0,
        }
    return {
        'mask_enabled': safe_bool(getattr(node, 'mask_enabled', False), False),
        'mask_top': safe_float(getattr(node, 'mask_top', 0) / 1000.0, 0.0, 3),
        'mask_bottom': safe_float(getattr(node, 'mask_bottom', 0) / 1000.0, 0.0, 3),
        'mask_left': safe_float(getattr(node, 'mask_left', 0) / 1000.0, 0.0, 3),
        'mask_right': safe_float(getattr(node, 'mask_right', 0) / 1000.0, 0.0, 3),
    }


# =============================================================================
# Flying key — keyframe machinery (sub-commit 5f)
# =============================================================================
#
# The flying key uses two outgoing packets:
#   SFKF — store the current USK DVE state as keyframe A or B
#   RFlK — run the keyer's DVE state toward a named keyframe (A/B/Full/Infinite)
#
# Keyframe names ('a' / 'b' / 'full' / 'runToInfinite') map to ATEM wire
# constants via the ``keyframe_constant`` helper in atemwire.helpers.
# Bucket B for all four ops (string→enum lookup with explicit raise).

# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def set_usk_fly_enabled(conn, keyer, enabled, me=0):
    """Toggle fly mode on a USK. Bucket A — the fly_enabled bit lives on
    the KeyType packet (same Send as set_usk_type) and is masked
    independently."""
    conn.send(KeyTypeCommand(
        index=me, keyer=int(keyer), fly_enabled=bool(enabled),
    ))


def set_keyer_fly_keyframe(conn, keyer, keyframe, me=0):
    """Store the current USK DVE state as the named keyframe (a/b/full)."""
    kf = keyframe_constant(keyframe)
    if kf is None:
        raise ValueError(f"unknown keyframe {keyframe!r}")
    conn.send(KeyerKeyframeSetCommand(
        index=me, keyer=int(keyer), keyframe=kf,
    ))


def run_flying_key_keyframe(conn, keyer, keyframe, me=0):
    """Run USK flight to a named keyframe (a/b/full). Direction only
    meaningful for run-to-infinite; this verb omits it."""
    kf = keyframe_constant(keyframe)
    if kf is None:
        raise ValueError(f"unknown keyframe {keyframe!r}")
    conn.send(KeyerKeyframeRunCommand(
        index=me, keyer=int(keyer), run_to=kf,
    ))


def run_flying_key_infinite_direction(conn, keyer, direction, me=0):
    """Run USK flight toward 'infinite' in the given numeric direction.
    direction is a wire constant (1=TL, 2=top, 3=TR, 4=L, 5=center,
    6=R, 7=BL, 8=bottom, 9=BR) — caller supplies it directly."""
    conn.send(KeyerKeyframeRunCommand(
        index=me, keyer=int(keyer),
        run_to=keyframe_constant('runToInfinite'),
        direction=int(direction),
    ))


# -----------------------------------------------------------------------------
# Reader
# -----------------------------------------------------------------------------


def usk_fly(mx, me, k):
    """Fly state for one keyer. Combines key-properties-fly (keyframe
    storage + position state) with key-properties-base.fly_enabled
    (the enable flag — same Recv that holds type/sources, hence
    needing two reads). Bucket B (multi-key aggregator)."""
    node = _kv(mx, 'key-properties-fly', me, k)
    base = _kv(mx, 'key-properties-base', me, k)
    fly_enabled = safe_bool(
        getattr(base, 'fly_enabled', False), False) if base else False

    if node is None:
        return {
            'fly_enabled': fly_enabled,
            'fly_keyframe_a_stored': False, 'fly_keyframe_b_stored': False,
            'fly_is_at_keyframe_a': False, 'fly_is_at_keyframe_b': False,
            'fly_is_at_keyframe_full': False,
            'fly_is_at_keyframe_infinite': False,
        }
    return {
        'fly_enabled': fly_enabled,
        'fly_keyframe_a_stored': safe_bool(
            getattr(node, 'is_a_set', False), False),
        'fly_keyframe_b_stored': safe_bool(
            getattr(node, 'is_b_set', False), False),
        'fly_is_at_keyframe_a': safe_bool(
            getattr(node, 'at_keyframe_a', False), False),
        'fly_is_at_keyframe_b': safe_bool(
            getattr(node, 'at_keyframe_b', False), False),
        'fly_is_at_keyframe_full': safe_bool(
            getattr(node, 'at_keyframe_full', False), False),
        'fly_is_at_keyframe_infinite': safe_bool(
            getattr(node, 'at_keyframe_infinite', False), False),
    }


def usk_fly_keyframe(mx, me, k, which='A'):
    """Geometry of fly keyframe A or B for one keyer, or ``None`` when that
    keyframe isn't stored. Reads ``key-properties-fly-keyframe`` (KKFP),
    keyed per (me, keyer, keyframe). Scales mirror ``usk_dve`` so the values
    drop straight into a ``<KeyFrameA>`` element (size/pos ÷1000, rotation
    ÷10, border widths ÷100, hue passthrough degrees, sat/luma ×100 from
    unit, light dir ÷10, mask ÷1000)."""
    kf_id = 1 if str(which).upper() == 'A' else 2
    by_me = mx.get('key-properties-fly-keyframe', {}) or {}
    node = ((by_me.get(me, {}) or {}).get(k, {}) or {}).get(kf_id)
    if node is None:
        return None
    return {
        'size_x': safe_float(getattr(node, 'size_x', 0) / 1000.0, 0.0, 3),
        'size_y': safe_float(getattr(node, 'size_y', 0) / 1000.0, 0.0, 3),
        'pos_x': safe_float(getattr(node, 'pos_x', 0) / 1000.0, 0.0, 3),
        'pos_y': safe_float(getattr(node, 'pos_y', 0) / 1000.0, 0.0, 3),
        'rotation': safe_float(getattr(node, 'rotation', 0) / 10.0, 0.0, 2),
        'border_outer_width': percent_from_thousandths(
            getattr(node, 'border_outer_width', 0), 0.0),
        'border_inner_width': percent_from_thousandths(
            getattr(node, 'border_inner_width', 0), 0.0),
        'border_outer_softness': safe_int(
            getattr(node, 'border_outer_softness', 0), 0),
        'border_inner_softness': safe_int(
            getattr(node, 'border_inner_softness', 0), 0),
        'border_bevel_softness': safe_int(
            getattr(node, 'border_bevel_softness', 0), 0),
        'border_bevel_position': safe_int(
            getattr(node, 'border_bevel_position', 0), 0),
        'border_opacity': safe_int(getattr(node, 'border_opacity', 100), 100),
        'border_hue': safe_float(getattr(node, 'border_hue', 0.0), 0.0, 2),
        'border_saturation': percent_from_unit(
            getattr(node, 'border_saturation', 0.0), 0.0),
        'border_luma': percent_from_unit(
            getattr(node, 'border_luma', 0.0), 0.0),
        'light_direction': safe_float(
            getattr(node, 'light_angle', 0) / 10.0, 0.0, 2),
        'light_altitude': safe_int(getattr(node, 'light_altitude', 0), 0),
        'mask_top': safe_float(getattr(node, 'mask_top', 0) / 1000.0, 0.0, 3),
        'mask_bottom': safe_float(
            getattr(node, 'mask_bottom', 0) / 1000.0, 0.0, 3),
        'mask_left': safe_float(getattr(node, 'mask_left', 0) / 1000.0, 0.0, 3),
        'mask_right': safe_float(
            getattr(node, 'mask_right', 0) / 1000.0, 0.0, 3),
    }
