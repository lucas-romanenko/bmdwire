# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Color generators (1 and 2) — solid-colour video sources usable as USK fill,
AUX, etc. Configured via hue / saturation / luma; ATEM stores them as fixed
integer scales on the wire and reports current state via ``ColV``.

Wire packets:
    CClV — outgoing, set color generator parameters (mask byte gates which
            of the three params are being written).
    ColV — incoming, current color generator state.
"""

import colorsys

import struct

from atemwire.state import percent_from_unit, safe_float
from atemwire.messages._dsl import Recv, Send, u8, u16


class ColorGeneratorCommand(Send):
    """``CClV`` — set hue / saturation / luma of one of the two color generators.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Set mask (bit 0 hue, bit 1 sat, bit 2 luma)
    1      1    u8     Color generator index
    2      2    u16    Hue (wire 0..3599, display 0.0..359.9°)
    4      2    u16    Saturation (wire 0..1000, display 0.0..1.0)
    6      2    u16    Luma (wire 0..1000, display 0.0..1.0)
    ====== ==== ====== ===========
    """
    CODE = 'CClV'
    SIZE = 8
    MASK_AT = 0

    index       = u8 (at=1)
    hue         = u16(at=2, mask_bit=0, scale=10)
    saturation  = u16(at=4, mask_bit=1, scale=1000)
    luma        = u16(at=6, mask_bit=2, scale=1000)

    def __init__(self, index, hue=None, saturation=None, luma=None):
        super().__init__(index=index, hue=hue,
                         saturation=saturation, luma=luma)

    @classmethod
    def from_rgb(cls, index, red, green, blue):
        """Convert an RGB triplet to ATEM's HLS values and build a command."""
        h, l, s = colorsys.rgb_to_hls(red, green, blue)
        return cls(index, hue=h * 359, saturation=s, luma=l)


class ColorGeneratorField(Recv):
    """``ColV`` — current color generator state.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Color generator index
    1      1    ?      padding
    2      2    u16    Hue (wire 0..3599, display 0.0..359.9°)
    4      2    u16    Saturation (wire 0..1000, display 0.0..1.0)
    6      2    u16    Luma (wire 0..1000, display 0.0..1.0)
    ====== ==== ====== ===========
    """
    CODE = 'ColV'
    PRETTY = 'color-generator'
    KEY_FORMAT = struct.Struct('>B')

    index       = u8 (at=0)
    hue         = u16(at=2, scale=10)
    saturation  = u16(at=4, scale=1000)
    luma        = u16(at=6, scale=1000)

    def get_rgb(self):
        return colorsys.hls_to_rgb(self.hue / 360.0, self.luma, self.saturation)

    def __repr__(self):
        return (f'<color-generator: index={self.index}, hue={self.hue} '
                f'saturation={self.saturation} luma={self.luma}>')


# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def set_color_generator_hue(conn, generator, hue):
    """Hue arrives as degrees 0..359.9; ColorGeneratorCommand declares
    ``scale=10`` so the Send packs ``int(round(hue * 10))`` into the
    wire u16 (0..3599) automatically."""
    conn.send(ColorGeneratorCommand(index=int(generator), hue=float(hue)))


def _percent_to_unit(v) -> float:
    """0..100 percent → 0..1 unit scalar, clamped. ColorGeneratorCommand
    declares saturation/luma with ``scale=1000`` so the Send packs the
    unit value into the wire u16 (0..1000). The /100 conversion lives
    here because the operator UI is percent but the Send-field unit is
    0..1; the clamp guards against out-of-range caller input. Bucket B
    per the Phase 1 decision — DSL ``scale=`` stays clamp-free."""
    return max(0.0, min(1.0, float(v) / 100.0))


def set_color_generator_saturation(conn, generator, saturation):
    conn.send(ColorGeneratorCommand(
        index=int(generator), saturation=_percent_to_unit(saturation)))


def set_color_generator_luma(conn, generator, luma):
    conn.send(ColorGeneratorCommand(
        index=int(generator), luma=_percent_to_unit(luma)))


# -----------------------------------------------------------------------------
# Readers
# -----------------------------------------------------------------------------


def color_generator(mx, idx):
    """atemwire's ColorGenerator normalizes hue to degrees and saturation/luma
    to 0..1. PyATEMMax surfaced saturation/luma as 0..100, so the
    reader scales back to percent."""
    nodes = mx.get('color-generator') or {}
    node = nodes.get(idx)
    if node is None:
        return {'hue': 0.0, 'saturation': 0.0, 'luma': 0.0}
    return {
        'hue': safe_float(getattr(node, 'hue', 0.0), 0.0, 1),
        'saturation': percent_from_unit(getattr(node, 'saturation', 0.0), 0.0),
        'luma': percent_from_unit(getattr(node, 'luma', 0.0), 0.0),
    }
