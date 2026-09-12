# SPDX-License-Identifier: LGPL-3.0-only
"""Tests for the ``scale=`` field option on the DSL.

``scale=`` lives on the ``Field`` base class in atemwire.messages._dsl:

    pack:    int(round(value * self.scale))
    unpack:  wire / self.scale

These tests cover:

  1. Round-trip for each integer field type at scale=1 (identity),
     scale=10, scale=100.
  2. Boundary values: min/max for signed and unsigned at scale=1.
  3. Float inputs that aren't clean multiples of the scale — verify
     the int(round(...)) rounding rather than truncation.
  4. mask_bit + scale interaction: a field carrying both options
     packs the mask bit AND the scaled value correctly.
  5. The six existing call sites in ``messages/color_generator.py``
     (hue scale=10, saturation/luma scale=1000 on both Send and Recv)
     round-trip cleanly.

The DSL itself is unchanged by this Phase 2 work — these are
characterization tests for the already-shipped scale= feature.
"""

import struct

import pytest

from atemwire.messages._dsl import (
    Send, u8, u16, u32, i8, i16, i32
)
from atemwire.messages.color_generator import (
    ColorGeneratorCommand, ColorGeneratorField,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pack_unpack(field, buf_size, value):
    """Pack a single value through ``field``, then unpack and return."""
    buf = bytearray(buf_size)
    field.pack(buf, value)
    return field.unpack(bytes(buf))


# ---------------------------------------------------------------------------
# 1. Round-trip at common scales for each integer type
# ---------------------------------------------------------------------------

# Test matrix: (field_class, buf_size, value, scale, expected_back).
# scale=1 is the identity case — pack stores the value as-is, unpack
# returns the same int. scale=10 / 100 / 1000 cover the common factors
# used in production (transition rates ×10, wipe positions ×10000, color
# generator saturation/luma ×1000, etc.).
@pytest.mark.parametrize("field_cls,size,value,scale", [
    # scale=1 (identity) — pack stores raw, unpack returns int.
    (u8,  1, 100,        1),
    (u16, 2, 30000,      1),
    (u32, 4, 1_000_000,  1),
    (i8,  1, -50,        1),
    (i16, 2, -10000,     1),
    (i32, 4, -100_000,   1),
    # scale=10 — pack stores value*10, unpack returns value as float.
    (u8,  1, 12.5,       10),     # wire 125
    (u16, 2, 180.5,      10),     # wire 1805
    (u32, 4, 100_000.0,  10),     # wire 1_000_000
    (i8,  1, -12.5,      10),     # wire -125
    (i16, 2, -180.5,     10),     # wire -1805
    (i32, 4, -100_000.0, 10),     # wire -1_000_000
    # scale=100 — typical for percent fields packed as basis points (×100).
    (u8,  1, 2.5,        100),    # wire 250
    (u16, 2, 50.0,       100),    # wire 5000
    (i16, 2, -50.0,      100),    # wire -5000
])
def test_roundtrip(field_cls, size, value, scale):
    f = field_cls(at=0, scale=scale)
    assert _pack_unpack(f, size, value) == pytest.approx(value)


# ---------------------------------------------------------------------------
# 2. Boundary values at scale=1
# ---------------------------------------------------------------------------

# Unsigned and signed extremes round-trip cleanly at scale=1. The two
# halves (min/max) verify struct.pack accepts the boundary and unpack
# returns the same number.
@pytest.mark.parametrize("field_cls,size,value", [
    (u8,  1, 0),
    (u8,  1, 255),
    (u16, 2, 0),
    (u16, 2, 65535),
    (u32, 4, 0),
    (u32, 4, 0xFFFFFFFF),
    (i8,  1, -128),
    (i8,  1, 127),
    (i16, 2, -32768),
    (i16, 2, 32767),
    (i32, 4, -2_147_483_648),
    (i32, 4, 2_147_483_647),
])
def test_boundary_scale1(field_cls, size, value):
    f = field_cls(at=0, scale=1)
    assert _pack_unpack(f, size, value) == value


# At higher scales the representable display range shrinks. Verify the
# packed max stays in the wire type's range.
def test_u16_scale10_caps_below_wire_max():
    """u16 max wire is 65535; at scale=10, display max is 6553.5."""
    f = u16(at=0, scale=10)
    assert _pack_unpack(f, 2, 6553.5) == pytest.approx(6553.5)


def test_i16_scale10_signed_boundaries():
    """i16 at scale=10: display range is [-3276.8, 3276.7]."""
    f = i16(at=0, scale=10)
    assert _pack_unpack(f, 2, 3276.7) == pytest.approx(3276.7)
    assert _pack_unpack(f, 2, -3276.8) == pytest.approx(-3276.8)


# ---------------------------------------------------------------------------
# 3. Float inputs that aren't clean multiples of the scale
# ---------------------------------------------------------------------------

# Rounding is ``int(round(value * scale))``. Python 3's round() uses
# banker's rounding (half-to-even), so 0.5 inputs land on the even side.
# These tests pin the observed behavior — what matters is that values
# round to the nearest representable wire integer, not that the rounding
# rule is arithmetically intuitive in every edge case.

def test_clean_multiple_roundtrips_exactly():
    """180.5 at scale=10 has an exact wire representation (1805)."""
    f = u16(at=0, scale=10)
    buf = bytearray(2)
    f.pack(buf, 180.5)
    assert struct.unpack_from('>H', bytes(buf), 0)[0] == 1805
    assert f.unpack(bytes(buf)) == pytest.approx(180.5)


def test_non_clean_multiple_rounds_to_nearest():
    """180.5499 at scale=10 → 1804.999 → rounds to 1805."""
    f = u16(at=0, scale=10)
    buf = bytearray(2)
    f.pack(buf, 180.5499)
    assert struct.unpack_from('>H', bytes(buf), 0)[0] == 1805


def test_non_clean_rounds_up_above_half():
    """180.56 at scale=10 → 1805.6 → rounds to 1806."""
    f = u16(at=0, scale=10)
    buf = bytearray(2)
    f.pack(buf, 180.56)
    assert struct.unpack_from('>H', bytes(buf), 0)[0] == 1806


def test_banker_rounding_at_exact_half():
    """Python 3's round() is banker's rounding — round-half-to-even.
    180.45 at scale=10 = 1804.5; rounds to 1804 (even). 180.55 at
    scale=10 = 1805.5; rounds to 1806 (even). The DSL inherits this
    behavior since it uses round() directly."""
    f = u16(at=0, scale=10)
    buf = bytearray(2)
    f.pack(buf, 180.45)
    assert struct.unpack_from('>H', bytes(buf), 0)[0] == 1804
    f.pack(buf, 180.55)
    assert struct.unpack_from('>H', bytes(buf), 0)[0] == 1806


def test_negative_float_rounds_correctly():
    """-180.5 at scale=10 packs as -1805."""
    f = i16(at=0, scale=10)
    buf = bytearray(2)
    f.pack(buf, -180.5)
    assert struct.unpack_from('>h', bytes(buf), 0)[0] == -1805


# ---------------------------------------------------------------------------
# 4. mask_bit + scale interaction
# ---------------------------------------------------------------------------

class _MaskedScaledSend(Send):
    """Two scaled fields, each gated by its own mask bit. The mask byte
    sits at offset 0 (one-byte mask). Field A scales ×10, field B
    scales ×100, both u16."""
    CODE = 'TEST'
    SIZE = 6
    MASK_AT = 0

    field_a = u16(at=2, mask_bit=0, scale=10)
    field_b = u16(at=4, mask_bit=1, scale=100)

    def __init__(self, field_a=None, field_b=None):
        super().__init__(field_a=field_a, field_b=field_b)


def test_mask_scale_both_set():
    """Both fields supplied → mask bits 0 and 1 set; both scaled
    values land at their offsets."""
    cmd = _MaskedScaledSend(field_a=180.5, field_b=50.0)
    raw = cmd.get_command()
    # header is 8 bytes; payload follows.
    payload = raw[8:]
    assert len(payload) == 6
    assert payload[0] == 0b11           # mask: bit 0 + bit 1
    assert struct.unpack_from('>H', payload, 2)[0] == 1805    # 180.5 × 10
    assert struct.unpack_from('>H', payload, 4)[0] == 5000    # 50.0 × 100


def test_mask_scale_only_a_set():
    """Only field_a supplied → mask bit 0 set; field_b stays zero."""
    cmd = _MaskedScaledSend(field_a=12.0)
    payload = cmd.get_command()[8:]
    assert payload[0] == 0b01
    assert struct.unpack_from('>H', payload, 2)[0] == 120
    assert struct.unpack_from('>H', payload, 4)[0] == 0


def test_mask_scale_none_set():
    """Neither field supplied → mask byte stays 0; payload is all zeros."""
    cmd = _MaskedScaledSend()
    payload = cmd.get_command()[8:]
    assert payload == bytes(6)


# ---------------------------------------------------------------------------
# 5. The six existing color_generator call sites
# ---------------------------------------------------------------------------

# ColorGeneratorCommand (CClV) layout:
#   offset 0: mask (u8)
#   offset 1: index (u8)
#   offset 2: hue (u16, scale=10)
#   offset 4: saturation (u16, scale=1000)
#   offset 6: luma (u16, scale=1000)

def test_color_generator_command_hue_scale_10():
    """Send: hue=180.5 → wire 1805 at offset 2."""
    cmd = ColorGeneratorCommand(index=0, hue=180.5)
    payload = cmd.get_command()[8:]
    assert struct.unpack_from('>H', payload, 2)[0] == 1805
    assert payload[0] & 0b001  # hue mask bit (bit 0) set


def test_color_generator_command_saturation_scale_1000():
    """Send: saturation=0.5 → wire 500 at offset 4."""
    cmd = ColorGeneratorCommand(index=1, saturation=0.5)
    payload = cmd.get_command()[8:]
    assert struct.unpack_from('>H', payload, 4)[0] == 500
    assert payload[0] & 0b010  # saturation mask bit (bit 1) set


def test_color_generator_command_luma_scale_1000():
    """Send: luma=0.75 → wire 750 at offset 6."""
    cmd = ColorGeneratorCommand(index=0, luma=0.75)
    payload = cmd.get_command()[8:]
    assert struct.unpack_from('>H', payload, 6)[0] == 750
    assert payload[0] & 0b100  # luma mask bit (bit 2) set


def test_color_generator_command_all_three():
    """All three set together: mask = 0b111, all values scaled at their
    offsets."""
    cmd = ColorGeneratorCommand(index=1, hue=359.9, saturation=1.0, luma=0.5)
    payload = cmd.get_command()[8:]
    assert payload[0] == 0b111
    assert payload[1] == 1                                     # index
    assert struct.unpack_from('>H', payload, 2)[0] == 3599     # hue
    assert struct.unpack_from('>H', payload, 4)[0] == 1000     # sat
    assert struct.unpack_from('>H', payload, 6)[0] == 500      # luma


# ColorGeneratorField (ColV) — synthetic wire payload, then unpack
# and verify the scaled display values come back. Layout:
#   offset 0: index (u8)
#   offset 1: padding (1 byte)
#   offset 2: hue (u16, scale=10)
#   offset 4: saturation (u16, scale=1000)
#   offset 6: luma (u16, scale=1000)

def _build_colv_payload(index, hue_wire, sat_wire, luma_wire):
    return struct.pack('>BxHHH', index, hue_wire, sat_wire, luma_wire)


def test_color_generator_field_hue_unpack():
    """Recv: wire 1805 at offset 2 → display 180.5."""
    raw = _build_colv_payload(0, 1805, 0, 0)
    field = ColorGeneratorField(raw)
    assert field.hue == pytest.approx(180.5)


def test_color_generator_field_saturation_unpack():
    """Recv: wire 500 at offset 4 → display 0.5."""
    raw = _build_colv_payload(0, 0, 500, 0)
    field = ColorGeneratorField(raw)
    assert field.saturation == pytest.approx(0.5)


def test_color_generator_field_luma_unpack():
    """Recv: wire 750 at offset 6 → display 0.75."""
    raw = _build_colv_payload(0, 0, 0, 750)
    field = ColorGeneratorField(raw)
    assert field.luma == pytest.approx(0.75)


def test_color_generator_send_recv_consistency():
    """Send-side and Recv-side scales for hue/sat/luma match. A value
    packed by the Send unpacks identically by the Recv when the wire
    representation is preserved. This is the protection against future
    asymmetric edits to either side."""
    # Pack via Send
    cmd = ColorGeneratorCommand(index=2, hue=240.0, saturation=0.6, luma=0.4)
    send_payload = cmd.get_command()[8:]

    # Splice the wire bytes (offsets 2/4/6) into a synthetic ColV payload
    # — Send and Recv have different first-byte layouts, but the three
    # scaled u16 fields share offsets 2/4/6.
    recv_raw = (
        struct.pack('>Bx', 2)             # index + pad (ColV layout)
        + send_payload[2:8]               # hue, saturation, luma
    )
    field = ColorGeneratorField(recv_raw)
    assert field.hue == pytest.approx(240.0)
    assert field.saturation == pytest.approx(0.6)
    assert field.luma == pytest.approx(0.4)
