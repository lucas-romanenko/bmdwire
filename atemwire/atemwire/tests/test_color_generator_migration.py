# SPDX-License-Identifier: LGPL-3.0-only
"""Migration tests for the ``color_generator`` feature.

Phase 3 commit 2: color generator setters + reader moved from
atemwire.operations / atemwire.state into atemwire.messages.color_generator.
Bucket A (1 op): set_color_generator_hue. Bucket B (2 ops):
set_color_generator_saturation, set_color_generator_luma — promoted
to B because each clamps the input to [0, 1] in addition to the
percent→unit conversion (DSL ``scale=`` is clamp-free per Phase 1
decision 4). Bucket B (1 reader): color_generator returns a 3-field
dict so it stays a function rather than collapsing into a Recv field.

The +8 header offset (Send.get_command prepends 8 bytes) applies
to every wire-byte assertion below.

ColorGeneratorCommand wire layout (per messages/color_generator.py):
    offset 0: mask byte
    offset 1: index (u8)
    offset 2: hue (u16, scale=10) — wire 0..3599, display 0.0..359.9
    offset 4: saturation (u16, scale=1000) — wire 0..1000, unit 0..1
    offset 6: luma (u16, scale=1000) — wire 0..1000, unit 0..1
"""

import struct

import pytest

from atemwire.messages.color_generator import (
    color_generator, set_color_generator_hue, set_color_generator_luma,
    set_color_generator_saturation
)


class _FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd.get_command())


# ---------------------------------------------------------------------------
# Wrappers — Send → wire
# ---------------------------------------------------------------------------

def test_set_color_generator_hue_packs_with_scale_10():
    """Hue=180.5 degrees → wire 1805 via DSL scale=10 on the Send field."""
    conn = _FakeConn()
    set_color_generator_hue(conn, generator=0, hue=180.5)
    payload = conn.sent[0][8:]
    assert payload[1] == 0                                  # generator index
    assert struct.unpack_from('>H', payload, 2)[0] == 1805  # hue wire
    assert payload[0] & 0b001                               # hue mask bit


def test_set_color_generator_saturation_percent_to_wire():
    """Saturation=50 percent → /100 → 0.5 unit → scale=1000 → wire 500."""
    conn = _FakeConn()
    set_color_generator_saturation(conn, generator=1, saturation=50)
    payload = conn.sent[0][8:]
    assert payload[1] == 1
    assert struct.unpack_from('>H', payload, 4)[0] == 500
    assert payload[0] & 0b010                               # saturation mask bit


def test_set_color_generator_luma_clamps_high():
    """Luma=150 percent (out of range) → clamped to 1.0 unit → wire 1000."""
    conn = _FakeConn()
    set_color_generator_luma(conn, generator=0, luma=150)
    payload = conn.sent[0][8:]
    assert struct.unpack_from('>H', payload, 6)[0] == 1000


def test_set_color_generator_luma_clamps_low():
    """Luma=-5 percent → clamped to 0.0 → wire 0."""
    conn = _FakeConn()
    set_color_generator_luma(conn, generator=0, luma=-5)
    payload = conn.sent[0][8:]
    assert struct.unpack_from('>H', payload, 6)[0] == 0


# ---------------------------------------------------------------------------
# Reader — mixerstate → display
# ---------------------------------------------------------------------------

class _ColV:
    """Stand-in for a ColorGeneratorField entry. The Recv class declares
    scale= on every field, so the unpacked attributes are already in
    display units (hue degrees, sat/luma 0..1)."""
    def __init__(self, hue, saturation, luma):
        self.hue = hue
        self.saturation = saturation
        self.luma = luma


def test_color_generator_reader_scales_back_to_percent():
    """saturation/luma stored as unit (0..1) in the Recv → reader scales
    to percent 0..100 for the frontend."""
    mx = {'color-generator': {0: _ColV(hue=240.0, saturation=0.6, luma=0.4)}}
    result = color_generator(mx, 0)
    assert result['hue'] == pytest.approx(240.0)
    assert result['saturation'] == pytest.approx(60.0)
    assert result['luma'] == pytest.approx(40.0)


def test_color_generator_reader_default_on_missing():
    """No mixerstate entry → zero defaults across all three fields."""
    assert color_generator({}, 0) == {'hue': 0.0, 'saturation': 0.0, 'luma': 0.0}
