# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Audio meter messages — broadcast realtime levels for legacy audio mixer
and Fairlight (per-strip + master).

All three packets carry variable-length / nested data, so parsing is
hand-rolled rather than DSL-declared.

Wire packets (incoming only):
    AMLv — legacy audio mixer levels (master + monitor + per-input)
    FMLv — Fairlight per-strip levels (input / output / dynamics GR / fader)
    FDLv — Fairlight master output levels (input / output / dynamics GR / fader)
"""

import math
import struct

from atemwire.messages._dsl import Recv


class AudioMeterLevelsField(Recv):
    """``AMLv`` — legacy audio mixer levels.

    Level conversion uses log scale: ``20 * log10(value / (128 * 65536))``
    in dB, with -60 floor for zero values.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Number of input channels (N)
    2      2    ?      padding
    4      4    u32    Master left level
    8      4    u32    Master right level
    12     4    u32    Master left peak
    16     4    u32    Master right peak
    20     4    u32    Monitor left level
    24     4    u32    Monitor right level
    28     4    u32    Monitor left peak
    32     4    u32    Monitor right peak
    36     2N   u16[]  Source indices (4-byte aligned)
    ...    16N  u32[]  Per-input (left lvl, right lvl, left peak, right peak)
    ====== ==== ====== ===========
    """
    CODE = 'AMLv'
    PRETTY = 'audio-meter-levels'

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack_from('>H2x 4I 4I', raw, 0)
        self.count = field[0]
        self.master = (
            self._level(field[1]),
            self._level(field[2]),
            self._level(field[3]),
            self._level(field[4]),
        )
        self.monitor = (
            self._level(field[5]),
            self._level(field[6]),
            self._level(field[7]),
            self._level(field[8]),
        )
        self.input = {}
        offset = struct.calcsize('>H2x 4I 4I')
        sources = struct.unpack_from(f'>{self.count}H', raw, offset)
        offset = int(math.ceil((offset + (2 * self.count)) / 4.0) * 4)
        levels = struct.unpack_from(f'>{self.count * 4}I', raw, offset)
        for i in range(0, self.count * 4, 4):
            level = (
                self._level(levels[i]),
                self._level(levels[i + 1]),
                self._level(levels[i + 2]),
                self._level(levels[i + 3]),
            )
            self.input[sources[i // 4]] = level

    @staticmethod
    def _level(value):
        if value == 0:
            return -60
        return math.log10(value / (128 * 65536)) * 20

    def __repr__(self):
        return f'<audio-meter-levels count={self.count}>'


class FairlightMeterLevelsField(Recv):
    """``FMLv`` — Fairlight per-strip realtime levels.

    All level fields are i16 in 0.01 dB units (range -10000..0 → -100..0 dB).
    The expander/compressor/limiter values are gain-reduction (negative
    means reduction applied).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      6    ?      padding
    6      1    u8     is_split (0xff if split into mono, else 0)
    7      1    u8     Subchannel index
    8      2    u16    Source index
    10     2    i16    Input level left
    12     2    i16    Input level right
    14     2    i16    Input peak left
    16     2    i16    Input peak right
    18     2    i16    Expander gain reduction
    20     2    i16    Compressor gain reduction
    22     2    i16    Limiter gain reduction
    24     2    i16    Output level left
    26     2    i16    Output level right
    28     2    i16    Output peak left
    30     2    i16    Output peak right
    32     2    i16    Fader level left
    34     2    i16    Fader level right
    36     2    i16    Fader peak left
    38     2    i16    Fader peak right
    ====== ==== ====== ===========
    """
    CODE = 'FMLv'
    PRETTY = 'fairlight-meter-levels'

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack_from('>6xBBH 15h', raw, 0)
        self.is_split = field[0]
        self.subchannel = field[1]
        self.index = field[2]

        self.strip_id = (
            f'{self.index}.{self.subchannel}'
            if self.is_split == 0xff
            else f'{self.index}.0'
        )

        self.input = (
            self._level(field[3]), self._level(field[4]),
            self._level(field[5]), self._level(field[6]),
        )
        self.expander_gr   = self._level(field[7])
        self.compressor_gr = self._level(field[8])
        self.limiter_gr    = self._level(field[9])
        self.output = (
            self._level(field[10]), self._level(field[11]),
            self._level(field[12]), self._level(field[13]),
        )
        self.level = (
            self._level(field[14]), self._level(field[15]),
            self._level(field[16]), self._level(field[17]),
        )

    @staticmethod
    def _level(value):
        # Wire is i16 in 0.01 dB units. Linear divide is the correct
        # conversion (matches Software Control's readout 1:1). Upstream
        # upstream's exponential decode was wrong for FMLv — produced
        # ~half the dB Software Control showed.
        return value / 100.0

    def __repr__(self):
        return f'<fairlight-meter-levels source={self.strip_id}>'


class FairlightMasterLevelsField(Recv):
    """``FDLv`` — Fairlight master output realtime levels.

    Same wire-unit convention as FMLv (i16 in 0.01 dB units).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    i16    Input level left
    2      2    i16    Input level right
    4      2    i16    Input peak left
    6      2    i16    Input peak right
    8      2    i16    Compressor gain reduction
    10     2    i16    Limiter gain reduction
    12     2    i16    Output level left
    14     2    i16    Output level right
    16     2    i16    Output peak left
    18     2    i16    Output peak right
    20     2    i16    Fader level left
    22     2    i16    Fader level right
    24     2    i16    Fader peak left
    26     2    i16    Fader peak right
    ====== ==== ====== ===========
    """
    CODE = 'FDLv'
    PRETTY = 'fairlight-master-levels'

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack_from('>14h', raw, 0)
        self.input = (
            self._level(field[0]), self._level(field[1]),
            self._level(field[2]), self._level(field[3]),
        )
        self.compressor_gr = self._level(field[4])
        self.limiter_gr    = self._level(field[5])
        self.output = (
            self._level(field[6]), self._level(field[7]),
            self._level(field[8]), self._level(field[9]),
        )
        self.level = (
            self._level(field[10]), self._level(field[11]),
            self._level(field[12]), self._level(field[13]),
        )

    @staticmethod
    def _level(value):
        return value / 100.0

    def __repr__(self):
        return '<fairlight-master-levels>'
