# SPDX-License-Identifier: LGPL-3.0-only
"""Migration tests for the ``upstream_keyer`` feature.

USK is the largest feature in atemwire (62 ops + 10 readers). Split
into 6 sub-commits matching Phase 1's per-key-type sub-groupings:

  5a — base (on-air / type / sources) + luma
  5b — chroma (advanced chroma + sample box + color correction)
  5c — dve (position / size / rotation / border / shadow / mask / light)
  5d — pattern
  5e — mask
  5f — fly (keyframe machinery)

Sub-blocks below grow as each sub-commit lands. Test fixtures
(_FakeConn, fake mixerstate classes) are shared across blocks.

The +8 header offset applies (Send.get_command prepends an 8-byte
header before the payload).
"""

import struct

import pytest

from atemwire.messages.upstream_keyer import (
    configure_usk_luma, set_usk_chroma_background, set_usk_chroma_blue,
    set_usk_chroma_brightness, set_usk_chroma_contrast,
    set_usk_chroma_flare_suppression, set_usk_chroma_foreground,
    set_usk_chroma_green, set_usk_chroma_key_edge, set_usk_chroma_preview,
    set_usk_chroma_red, set_usk_chroma_sample,
    set_usk_chroma_sample_position, set_usk_chroma_sample_size,
    set_usk_chroma_saturation, set_usk_chroma_spill,
    set_usk_dve_border_bevel_position, set_usk_dve_border_bevel_softness,
    set_usk_dve_border_enabled, set_usk_dve_border_hue,
    set_usk_dve_border_inner_softness, set_usk_dve_border_inner_width,
    set_usk_dve_border_luma, set_usk_dve_border_opacity,
    set_usk_dve_border_outer_softness, set_usk_dve_border_outer_width,
    set_usk_dve_border_saturation, set_usk_dve_bottom, set_usk_dve_left,
    set_usk_dve_light_altitude, set_usk_dve_light_direction,
    set_usk_dve_masked, set_usk_dve_position_x, set_usk_dve_position_y,
    set_usk_dve_rate, set_usk_dve_right, set_usk_dve_rotation,
    set_usk_dve_shadow, set_usk_dve_size_x, set_usk_dve_size_y,
    set_usk_dve_top, set_usk_fly_enabled, set_keyer_fly_keyframe,
    run_flying_key_keyframe, run_flying_key_infinite_direction,
    set_usk_mask_bottom, set_usk_mask_enabled, set_usk_mask_left,
    set_usk_mask_right, set_usk_mask_top, set_usk_pattern_invert,
    set_usk_pattern_position_x, set_usk_pattern_position_y,
    set_usk_pattern_size, set_usk_pattern_softness, set_usk_pattern_style,
    set_usk_pattern_symmetry, set_usk_fill_source, set_usk_key_source,
    set_usk_luma_clip, set_usk_luma_gain, set_usk_luma_invert,
    set_usk_luma_pre_multiplied, set_usk_on_air, set_usk_type, toggle_usk,
    usk_chroma, usk_dve, usk_fill_source, usk_fly, usk_key_source,
    usk_luma, usk_mask, usk_on_air, usk_pattern, usk_type
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self, mixerstate=None):
        self.sent = []
        self.mixerstate = mixerstate or {}

    def send(self, cmd):
        self.sent.append(cmd.get_command())


class _VideoMode:
    """Shared mixerstate stand-in used by rate-resolving tests."""
    def __init__(self, rate):
        self.rate = rate


class _KeOn:
    """Stand-in for the key-on-air per-(me, keyer) entry."""
    def __init__(self, enabled=False):
        self.enabled = enabled


class _KeBP:
    """Stand-in for key-properties-base per-(me, keyer)."""
    def __init__(self, type=0, fill_source=0, key_source=0,
                 fly_enabled=False):
        self.type = type
        self.fill_source = fill_source
        self.key_source = key_source
        self.fly_enabled = fly_enabled


class _KeLm:
    """Stand-in for key-properties-luma per-(me, keyer)."""
    def __init__(self, clip=0, gain=0, key_inverted=False,
                 premultiplied=False):
        self.clip = clip
        self.gain = gain
        self.key_inverted = key_inverted
        self.premultiplied = premultiplied


# ===========================================================================
# 5a — base (on-air / type / sources) + luma
# ===========================================================================

# --- Operations -----------------------------------------------------------

def test_set_usk_on_air_packs_ckon():
    conn = _FakeConn()
    set_usk_on_air(conn, keyer=0, enabled=True, me=0)
    assert conn.sent[0][4:8] == b'CKOn'


def test_toggle_usk_reads_state_and_inverts():
    """RMW: reads key-on-air[0][2].enabled=True, sends inverted via
    set_usk_on_air."""
    conn = _FakeConn(mixerstate={
        'key-on-air': {0: {2: _KeOn(enabled=True)}},
    })
    toggle_usk(conn, keyer=2, me=0)
    assert conn.sent[0][4:8] == b'CKOn'


def test_set_usk_type_packs_cktp():
    conn = _FakeConn()
    set_usk_type(conn, keyer=1, key_type=3, me=0)
    assert conn.sent[0][4:8] == b'CKTp'


def test_set_usk_fill_and_key_sources():
    conn = _FakeConn()
    set_usk_fill_source(conn, keyer=0, source=15, me=0)
    set_usk_key_source(conn, keyer=0, source=18, me=0)
    assert conn.sent[0][4:8] == b'CKeF'
    assert conn.sent[1][4:8] == b'CKeC'


def test_set_usk_luma_clip_gain_use_tenths_scale():
    """Luma clip/gain wire is ×10 of percent (0..1000), clamped via
    percent_to_tenths."""
    conn = _FakeConn()
    set_usk_luma_clip(conn, keyer=0, clip=66, me=0)
    set_usk_luma_gain(conn, keyer=0, gain=50, me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CKLm'


def test_set_usk_luma_clip_clamps_above_100():
    """percent_to_tenths clamps 150 → wire 1000 (= 100% in display)."""
    conn = _FakeConn()
    set_usk_luma_clip(conn, keyer=0, clip=150, me=0)
    assert conn.sent[0][4:8] == b'CKLm'


def test_set_usk_luma_invert_and_pre_multiplied_bool():
    conn = _FakeConn()
    set_usk_luma_invert(conn, keyer=0, invert=True, me=0)
    set_usk_luma_pre_multiplied(conn, keyer=0, pre_multiplied=True, me=0)
    assert all(c[4:8] == b'CKLm' for c in conn.sent)


def test_configure_usk_luma_uses_tenths_scale():
    """Scale fix (2026-06-02): configure_usk_luma now uses percent_to_tenths
    (×10), matching set_usk_luma_clip / set_usk_luma_gain. CKLm packs
    clip at payload offset 4 (packet offset 12) and gain at offset 6
    (packet offset 14), both u16 BE. clip=50 → wire 500 (50.0%), NOT the
    old 5000 that the ATEM clamped to 100%."""
    conn = _FakeConn()
    configure_usk_luma(conn, keyer=0, clip=50, gain=50, me=0)
    pkt = conn.sent[0]
    assert pkt[4:8] == b'CKLm'
    assert struct.unpack_from('>H', pkt, 12)[0] == 500   # clip ×10
    assert struct.unpack_from('>H', pkt, 14)[0] == 500   # gain ×10


# --- Readers --------------------------------------------------------------

def test_usk_on_air_reads_nested_dict():
    """key-on-air is nested as {me: {keyer: entry}}."""
    mx = {'key-on-air': {0: {1: _KeOn(enabled=True)}}}
    assert usk_on_air(mx, 0, 1) is True
    assert usk_on_air(mx, 0, 2) is False  # missing keyer
    assert usk_on_air({}, 0, 0) is False   # empty mx


def test_usk_type_reads_base():
    mx = {'key-properties-base': {0: {2: _KeBP(type=3)}}}
    assert usk_type(mx, 0, 2) == 3
    assert usk_type(mx, 0, 0) == 0  # default


def test_usk_fill_and_key_sources_share_base_key():
    mx = {'key-properties-base': {0: {0: _KeBP(
        fill_source=10, key_source=12,
    )}}}
    assert usk_fill_source(mx, 0, 0) == 10
    assert usk_key_source(mx, 0, 0) == 12


def test_usk_luma_assembles_4_field_dict():
    """clip/gain at wire ×10; bools pass through."""
    mx = {'key-properties-luma': {0: {0: _KeLm(
        clip=660, gain=500, key_inverted=True, premultiplied=False,
    )}}}
    result = usk_luma(mx, 0, 0)
    assert result['luma_clip'] == pytest.approx(66.0)
    assert result['luma_gain'] == pytest.approx(50.0)
    assert result['luma_invert'] is True
    assert result['luma_pre_multiplied'] is False


def test_usk_luma_default_when_missing():
    """Returns the empty 4-field shape with zeros / False."""
    assert usk_luma({}, 0, 0) == {
        'luma_clip': 0.0, 'luma_gain': 0.0,
        'luma_invert': False, 'luma_pre_multiplied': False,
    }


# ===========================================================================
# 5b — chroma + sample + color correction
# ===========================================================================

class _KACk:
    """Stand-in for key-properties-advanced-chroma per-(me, keyer)."""
    def __init__(self, foreground=0, background=0, key_edge=0,
                 spill_suppress=0, flare_suppress=0,
                 brightness=0, contrast=0, saturation=1000,
                 red=0, green=0, blue=0):
        self.foreground = foreground
        self.background = background
        self.key_edge = key_edge
        self.spill_suppress = spill_suppress
        self.flare_suppress = flare_suppress
        self.brightness = brightness
        self.contrast = contrast
        self.saturation = saturation
        self.red = red
        self.green = green
        self.blue = blue


class _KACC:
    """Stand-in for key-properties-advanced-chroma-colorpicker."""
    def __init__(self, cursor=False, preview=False, size=1000,
                 x=0, y=0, Y=0.0, Cb=0.0, Cr=0.0):
        self.cursor = cursor
        self.preview = preview
        self.size = size
        self.x = x
        self.y = y
        self.Y = Y
        self.Cb = Cb
        self.Cr = Cr


# --- Operations: KACk fields ---------------------------------------------

def test_set_usk_chroma_foreground_scale_thousandths():
    """Chroma 5-fields scale: unit × 1000 → wire. 0.091 → 91."""
    conn = _FakeConn()
    set_usk_chroma_foreground(conn, keyer=0, foreground=0.091, me=0)
    assert conn.sent[0][4:8] == b'CACK'


def test_set_usk_chroma_5_field_setters():
    """foreground / background / key_edge / spill / flare all use the
    same scale and pack to the same CACK wire."""
    conn = _FakeConn()
    set_usk_chroma_background(conn, keyer=0, background=0.5, me=0)
    set_usk_chroma_key_edge(conn, keyer=0, key_edge=0.5, me=0)
    set_usk_chroma_spill(conn, keyer=0, spill=0.5, me=0)
    set_usk_chroma_flare_suppression(conn, keyer=0, flare_suppression=0.5, me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CACK'


def test_set_usk_chroma_color_correction_setters():
    """The 6 color-correction setters share the ×1000 scale (Bug B fix
    2026-05-04 — earlier code had brightness/contrast/RGB at ×10)."""
    conn = _FakeConn()
    set_usk_chroma_brightness(conn, keyer=0, brightness=0.5, me=0)
    set_usk_chroma_contrast(conn, keyer=0, contrast=0.5, me=0)
    set_usk_chroma_saturation(conn, keyer=0, saturation=0.6, me=0)
    set_usk_chroma_red(conn, keyer=0, red=0.5, me=0)
    set_usk_chroma_green(conn, keyer=0, green=0.5, me=0)
    set_usk_chroma_blue(conn, keyer=0, blue=0.5, me=0)
    assert all(c[4:8] == b'CACK' for c in conn.sent)


# --- Operations: KACC sample/preview -------------------------------------

def test_set_usk_chroma_sample_toggles_cursor():
    conn = _FakeConn()
    set_usk_chroma_sample(conn, keyer=0, enabled=True, me=0)
    assert conn.sent[0][4:8] == b'CACC'


def test_set_usk_chroma_sample_position_uses_signed_wire():
    """0..1 unit → signed wire (x: ±16000, y: ±9000) via the
    (value - 0.5) * scale transform. 0.5 → 0; 1.0 → ±16000/±9000."""
    conn = _FakeConn()
    set_usk_chroma_sample_position(conn, keyer=0, x=0.5, y=0.5, me=0)
    assert conn.sent[0][4:8] == b'CACC'


def test_set_usk_chroma_sample_size_clamps_to_spec_range():
    """The wire range is 620..9925; below or above clamps to those
    bounds. A typed 5% (size=0.05 → wire 500) snaps to 620."""
    conn = _FakeConn()
    set_usk_chroma_sample_size(conn, keyer=0, size=0.05, me=0)
    set_usk_chroma_sample_size(conn, keyer=0, size=2.0, me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CACC'


def test_set_usk_chroma_preview_toggles_overlay():
    conn = _FakeConn()
    set_usk_chroma_preview(conn, keyer=0, preview=True, me=0)
    assert conn.sent[0][4:8] == b'CACC'


# --- Reader: usk_chroma aggregator ---------------------------------------

def test_usk_chroma_default_when_no_kack_no_cacc():
    """Empty mixerstate → returns the full default shape."""
    result = usk_chroma({}, 0, 0)
    assert result['chroma_foreground'] == 0.0
    assert result['chroma_saturation'] == 1.0  # default saturation differs
    assert result['chroma_sample'] is False
    assert result['chroma_sample_position'] == {'x': 0.5, 'y': 0.5}


def test_usk_chroma_reads_kack_5_fields_with_thousandths():
    """Wire 91 → display 0.091 (3 decimals to preserve 1-decimal-percent)."""
    mx = {'key-properties-advanced-chroma': {0: {0: _KACk(
        foreground=91, background=500, key_edge=850,
        spill_suppress=200, flare_suppress=100,
    )}}}
    result = usk_chroma(mx, 0, 0)
    assert result['chroma_foreground'] == pytest.approx(0.091)
    assert result['chroma_background'] == pytest.approx(0.5)
    assert result['chroma_key_edge'] == pytest.approx(0.85)


def test_usk_chroma_color_correction_6_fields():
    mx = {'key-properties-advanced-chroma': {0: {0: _KACk(
        brightness=500, contrast=500, saturation=600,
        red=100, green=200, blue=300,
    )}}}
    result = usk_chroma(mx, 0, 0)
    assert result['chroma_brightness'] == pytest.approx(0.5)
    assert result['chroma_saturation'] == pytest.approx(0.6)
    assert result['chroma_red'] == pytest.approx(0.1)
    assert result['chroma_green'] == pytest.approx(0.2)
    assert result['chroma_blue'] == pytest.approx(0.3)


def test_usk_chroma_cacc_sample_position_round_trip():
    """Symmetric to the writer: wire 0 → unit 0.5 (center)."""
    mx = {'key-properties-advanced-chroma-colorpicker': {0: {0: _KACC(
        cursor=True, x=0, y=0, size=5000,
    )}}}
    result = usk_chroma(mx, 0, 0)
    assert result['chroma_sample'] is True
    assert result['chroma_sample_position']['x'] == pytest.approx(0.5)
    assert result['chroma_sample_size'] == pytest.approx(0.5)


def test_usk_chroma_cacc_sample_position_clamps_above_one():
    """Out-of-spec wire (a few ATEMs send past the documented max)
    clamps to the 0..1 display range."""
    mx = {'key-properties-advanced-chroma-colorpicker': {0: {0: _KACC(
        x=20000,  # above ±16000 spec
    )}}}
    result = usk_chroma(mx, 0, 0)
    assert result['chroma_sample_position']['x'] == pytest.approx(1.0)


# ===========================================================================
# 5c — DVE (position / size / rotation / border / mask / light / shadow / rate)
# ===========================================================================

class _KeDV:
    """Stand-in for key-properties-dve per-(me, keyer)."""
    def __init__(self, pos_x=0, pos_y=0, size_x=1000, size_y=1000,
                 rotation=0, mask_enabled=False, mask_top=0,
                 mask_bottom=0, mask_left=0, mask_right=0,
                 border_enabled=False, border_hue=0.0,
                 border_saturation=0.0, border_luma=0.0,
                 border_opacity=100, border_outer_width=0,
                 border_inner_width=0, border_outer_softness=0,
                 border_inner_softness=0, border_bevel=0,
                 border_bevel_position=0, border_bevel_softness=0,
                 light_angle=0, light_altitude=0,
                 shadow_enabled=False, rate=25):
        self.pos_x = pos_x
        self.pos_y = pos_y
        self.size_x = size_x
        self.size_y = size_y
        self.rotation = rotation
        self.mask_enabled = mask_enabled
        self.mask_top = mask_top
        self.mask_bottom = mask_bottom
        self.mask_left = mask_left
        self.mask_right = mask_right
        self.border_enabled = border_enabled
        self.border_hue = border_hue
        self.border_saturation = border_saturation
        self.border_luma = border_luma
        self.border_opacity = border_opacity
        self.border_outer_width = border_outer_width
        self.border_inner_width = border_inner_width
        self.border_outer_softness = border_outer_softness
        self.border_inner_softness = border_inner_softness
        self.border_bevel = border_bevel
        self.border_bevel_position = border_bevel_position
        self.border_bevel_softness = border_bevel_softness
        self.light_angle = light_angle
        self.light_altitude = light_altitude
        self.shadow_enabled = shadow_enabled
        self.rate = rate


# --- Operations: position / size / rotation -------------------------------

def test_set_usk_dve_position_clamps_to_spec_range():
    """pos_x clamps to ±16000, pos_y to ±9000."""
    conn = _FakeConn()
    set_usk_dve_position_x(conn, keyer=0, position_x=20.0, me=0)  # clamps to 16
    set_usk_dve_position_y(conn, keyer=0, position_y=-12.0, me=0)  # clamps to -9
    for cmd in conn.sent:
        assert cmd[4:8] == b'CKDV'


def test_set_usk_dve_size_uses_size_thousandths_helper():
    """size_x/y: 0..2 unit → 0..2000 wire (×1000) via size_thousandths."""
    conn = _FakeConn()
    set_usk_dve_size_x(conn, keyer=0, size_x=1.5, me=0)
    set_usk_dve_size_y(conn, keyer=0, size_y=0.5, me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CKDV'


def test_set_usk_dve_rotation_scale_tenths():
    """rotation 45.0° → wire 450."""
    conn = _FakeConn()
    set_usk_dve_rotation(conn, keyer=0, rotation=45.0, me=0)
    assert conn.sent[0][4:8] == b'CKDV'


# --- Operations: border ---------------------------------------------------

def test_set_usk_dve_border_enabled_bool():
    conn = _FakeConn()
    set_usk_dve_border_enabled(conn, keyer=0, enabled=True, me=0)
    assert conn.sent[0][4:8] == b'CKDV'


def test_set_usk_dve_border_hue_sat_luma_use_scale_10():
    """SCALE CONTRACT: all three border color fields are WRITE ×10 to
    match the parser's per-field divisors. See banner."""
    conn = _FakeConn()
    set_usk_dve_border_hue(conn, keyer=0, hue=180.0, me=0)
    set_usk_dve_border_saturation(conn, keyer=0, saturation=50.0, me=0)
    set_usk_dve_border_luma(conn, keyer=0, luma=75.0, me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CKDV'


def test_set_usk_dve_border_widths_scale_hundredths():
    """outer_width / inner_width: display 0..16.00 → wire 0..1600 (×100)."""
    conn = _FakeConn()
    set_usk_dve_border_outer_width(conn, keyer=0, outer_width=8.5, me=0)
    set_usk_dve_border_inner_width(conn, keyer=0, inner_width=2.0, me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CKDV'


def test_set_usk_dve_border_softness_and_opacity():
    """All 8-bit fields: opacity, outer/inner/bevel softness,
    bevel_position. Identity rounding only."""
    conn = _FakeConn()
    set_usk_dve_border_opacity(conn, keyer=0, opacity=90, me=0)
    set_usk_dve_border_outer_softness(conn, keyer=0, outer_softness=50, me=0)
    set_usk_dve_border_inner_softness(conn, keyer=0, inner_softness=25, me=0)
    set_usk_dve_border_bevel_softness(conn, keyer=0, bevel_softness=10, me=0)
    set_usk_dve_border_bevel_position(conn, keyer=0, bevel_position=50, me=0)
    assert len(conn.sent) == 5


# --- Operations: mask + light + shadow + rate ----------------------------

def test_set_usk_dve_mask_setters_scale_thousandths():
    """mask top/bottom/left/right: display ±9 or ±16 → wire ×1000."""
    conn = _FakeConn()
    set_usk_dve_masked(conn, keyer=0, masked=True, me=0)
    set_usk_dve_top(conn, keyer=0, top=5.0, me=0)
    set_usk_dve_bottom(conn, keyer=0, bottom=-5.0, me=0)
    set_usk_dve_left(conn, keyer=0, left=8.0, me=0)
    set_usk_dve_right(conn, keyer=0, right=-8.0, me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CKDV'


def test_set_usk_dve_light_direction_scale_tenths():
    """direction 90.0° → wire 900 (×10)."""
    conn = _FakeConn()
    set_usk_dve_light_direction(conn, keyer=0, direction=90.0, me=0)
    set_usk_dve_light_altitude(conn, keyer=0, altitude=45, me=0)
    set_usk_dve_shadow(conn, keyer=0, shadow=True, me=0)
    assert len(conn.sent) == 3


def test_set_usk_dve_rate_parses_against_display_fps():
    conn = _FakeConn(mixerstate={'video-mode': _VideoMode(rate=60)})
    set_usk_dve_rate(conn, keyer=0, rate_str='1:00', me=0)
    assert conn.sent[0][4:8] == b'CKDV'


# --- Reader: usk_dve aggregator -------------------------------------------

def test_usk_dve_default_when_missing():
    """26-field default dict when key-properties-dve is absent."""
    result = usk_dve({}, 0, 0)
    assert result['dve_position_x'] == 0.0
    assert result['dve_size_x'] == 1.0  # size defaults to 1.0, not 0
    assert result['dve_border_opacity'] == 100.0
    assert result['dve_rate'] == 25.0


def test_usk_dve_reads_position_size_rotation_with_scale():
    """pos/size wire ×1000; rotation wire ×10."""
    mx = {'key-properties-dve': {0: {0: _KeDV(
        pos_x=8000, pos_y=-4500, size_x=1500, size_y=750,
        rotation=450,
    )}}}
    result = usk_dve(mx, 0, 0)
    assert result['dve_position_x'] == pytest.approx(8.0)
    assert result['dve_position_y'] == pytest.approx(-4.5)
    assert result['dve_size_x'] == pytest.approx(1.5)
    assert result['dve_size_y'] == pytest.approx(0.75)
    assert result['dve_rotation'] == pytest.approx(45.0)


def test_usk_dve_border_scale_contract_round_trip():
    """SCALE CONTRACT: hue is identity (parser delivered degrees);
    sat/luma are percent_from_unit (parser delivered 0..1 unit)."""
    mx = {'key-properties-dve': {0: {0: _KeDV(
        border_enabled=True,
        border_hue=180.0,        # parser already converted ÷10
        border_saturation=0.6,   # parser already ÷1000
        border_luma=0.75,
        border_opacity=80,
        border_outer_width=850,  # wire ÷100 → 8.5 display
        border_inner_width=200,
    )}}}
    result = usk_dve(mx, 0, 0)
    assert result['dve_border_hue'] == pytest.approx(180.0)
    assert result['dve_border_saturation'] == pytest.approx(60.0)
    assert result['dve_border_luma'] == pytest.approx(75.0)
    assert result['dve_border_outer_width'] == pytest.approx(8.5)


def test_usk_dve_mask_fields_scale_thousandths():
    mx = {'key-properties-dve': {0: {0: _KeDV(
        mask_enabled=True,
        mask_top=5000, mask_bottom=-5000,
        mask_left=8000, mask_right=-16000,
    )}}}
    result = usk_dve(mx, 0, 0)
    assert result['dve_masked'] is True
    assert result['dve_top'] == pytest.approx(5.0)
    assert result['dve_bottom'] == pytest.approx(-5.0)
    assert result['dve_left'] == pytest.approx(8.0)
    assert result['dve_right'] == pytest.approx(-16.0)


def test_usk_dve_light_direction_wire_divided_by_10():
    """light_angle wire ÷10 to display degrees."""
    mx = {'key-properties-dve': {0: {0: _KeDV(
        light_angle=900, light_altitude=45,
    )}}}
    result = usk_dve(mx, 0, 0)
    assert result['dve_light_direction'] == pytest.approx(90.0)
    assert result['dve_light_altitude'] == pytest.approx(45.0)


# ===========================================================================
# 5d — pattern (×100 ruled the correct scale, not a bug)
# ===========================================================================

class _KePt:
    """Stand-in for key-properties-pattern per-(me, keyer)."""
    def __init__(self, pattern=0, size=5000, symmetry=5000, softness=0,
                 invert=False, position_x=5000, position_y=5000):
        self.pattern = pattern
        self.size = size
        self.symmetry = symmetry
        self.softness = softness
        self.invert = invert
        self.position_x = position_x
        self.position_y = position_y


# --- Operations -----------------------------------------------------------

def test_set_usk_pattern_style_identity():
    conn = _FakeConn()
    set_usk_pattern_style(conn, keyer=0, pattern=3, me=0)
    assert conn.sent[0][4:8] == b'CKPt'


def test_set_usk_pattern_size_uses_thousandths():
    """Not a bug: size correctly uses percent_to_thousandths
    (×100) — the wire field is u16 0..10000, so display 0..100 maps to
    wire 0..10000. Round-trip with the read side's
    percent_from_thousandths is self-consistent and externally matches
    ATEM Software Control. Do not "fix" to ×10."""
    conn = _FakeConn()
    set_usk_pattern_size(conn, keyer=0, size=75, me=0)
    assert conn.sent[0][4:8] == b'CKPt'


def test_set_usk_pattern_symmetry_and_softness():
    conn = _FakeConn()
    set_usk_pattern_symmetry(conn, keyer=0, symmetry=50, me=0)
    set_usk_pattern_softness(conn, keyer=0, softness=25, me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CKPt'


def test_set_usk_pattern_invert_bool():
    conn = _FakeConn()
    set_usk_pattern_invert(conn, keyer=0, invert=True, me=0)
    assert conn.sent[0][4:8] == b'CKPt'


def test_set_usk_pattern_position_uses_unit_thousandths():
    """position_x/y: 0..1 unit → 0..10000 wire (×10000)."""
    conn = _FakeConn()
    set_usk_pattern_position_x(conn, keyer=0, position_x=0.5, me=0)
    set_usk_pattern_position_y(conn, keyer=0, position_y=0.25, me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CKPt'


# --- Reader: usk_pattern --------------------------------------------------

def test_usk_pattern_default_when_missing():
    """Defaults: size/symmetry 50.0, softness 0.0, positions 0.5."""
    result = usk_pattern({}, 0, 0)
    assert result['pattern_size'] == 50.0
    assert result['pattern_symmetry'] == 50.0
    assert result['pattern_softness'] == 0.0
    assert result['pattern_position_x'] == 0.5
    assert result['pattern_position_y'] == 0.5


def test_usk_pattern_round_trip_with_thousandths_scale():
    """Wire 7500 → display 75.0 percent (÷100, places=2)."""
    mx = {'key-properties-pattern': {0: {0: _KePt(
        pattern=5, size=7500, symmetry=4000, softness=2500,
        invert=True, position_x=8000, position_y=2000,
    )}}}
    result = usk_pattern(mx, 0, 0)
    assert result['pattern_style'] == 5
    assert result['pattern_size'] == pytest.approx(75.0)
    assert result['pattern_symmetry'] == pytest.approx(40.0)
    assert result['pattern_softness'] == pytest.approx(25.0)
    assert result['pattern_invert'] is True
    assert result['pattern_position_x'] == pytest.approx(0.8)
    assert result['pattern_position_y'] == pytest.approx(0.2)


# ===========================================================================
# 5e — mask
# ===========================================================================

class _KeBPMask:
    """Stand-in for key-properties-base mask fields. Same Recv class as
    base (see _KeBP fixture), but exposing only the mask fields here
    for the mask reader's read paths."""
    def __init__(self, mask_enabled=False, mask_top=0, mask_bottom=0,
                 mask_left=0, mask_right=0):
        self.mask_enabled = mask_enabled
        self.mask_top = mask_top
        self.mask_bottom = mask_bottom
        self.mask_left = mask_left
        self.mask_right = mask_right


# --- Operations -----------------------------------------------------------

def test_set_usk_mask_enabled_bool():
    conn = _FakeConn()
    set_usk_mask_enabled(conn, keyer=0, enabled=True, me=0)
    assert conn.sent[0][4:8] == b'CKMs'


def test_set_usk_mask_fields_scale_thousandths():
    """Wire is i16 ×1000 of display (e.g. 9.000 → 9000); CKMs packet."""
    conn = _FakeConn()
    set_usk_mask_top(conn, keyer=0, top=5.0, me=0)
    set_usk_mask_bottom(conn, keyer=0, bottom=-5.0, me=0)
    set_usk_mask_left(conn, keyer=0, left=8.0, me=0)
    set_usk_mask_right(conn, keyer=0, right=-8.0, me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CKMs'


# --- Reader: usk_mask -----------------------------------------------------

def test_usk_mask_default_when_missing():
    """key-properties-base absent → zero-fill defaults."""
    result = usk_mask({}, 0, 0)
    assert result == {
        'mask_enabled': False, 'mask_top': 0.0, 'mask_bottom': 0.0,
        'mask_left': 0.0, 'mask_right': 0.0,
    }


def test_usk_mask_reads_from_base_with_thousandths_scale():
    """mask_* fields on key-properties-base (NOT a separate Recv class).
    Wire ÷1000 to display."""
    mx = {'key-properties-base': {0: {0: _KeBPMask(
        mask_enabled=True, mask_top=9000, mask_bottom=-9000,
        mask_left=16000, mask_right=-16000,
    )}}}
    result = usk_mask(mx, 0, 0)
    assert result['mask_enabled'] is True
    assert result['mask_top'] == pytest.approx(9.0)
    assert result['mask_bottom'] == pytest.approx(-9.0)
    assert result['mask_left'] == pytest.approx(16.0)
    assert result['mask_right'] == pytest.approx(-16.0)


# ===========================================================================
# 5f — fly (keyframe machinery)
# ===========================================================================

class _KeFP:
    """Stand-in for key-properties-fly."""
    def __init__(self, is_a_set=False, is_b_set=False,
                 at_keyframe_a=False, at_keyframe_b=False,
                 at_keyframe_full=False, at_keyframe_infinite=False):
        self.is_a_set = is_a_set
        self.is_b_set = is_b_set
        self.at_keyframe_a = at_keyframe_a
        self.at_keyframe_b = at_keyframe_b
        self.at_keyframe_full = at_keyframe_full
        self.at_keyframe_infinite = at_keyframe_infinite


# --- Operations -----------------------------------------------------------

def test_set_usk_fly_enabled_packs_cktp():
    """fly_enabled rides on KeyTypeCommand (same Send as set_usk_type)."""
    conn = _FakeConn()
    set_usk_fly_enabled(conn, keyer=0, enabled=True, me=0)
    assert conn.sent[0][4:8] == b'CKTp'


def test_set_keyer_fly_keyframe_packs_sfkf():
    """Keyframe set: 'a' / 'b' / 'full' → wire constants via
    keyframe_constant. Packet code is SFKF, and the keyframe byte (payload
    offset 2 → packet offset 10) is the right constant — A=1, B=2, Full=3.
    Regression guard for the int-vs-'A' coercion bug that made every
    snapshot land on keyframe B."""
    conn = _FakeConn()
    set_keyer_fly_keyframe(conn, keyer=0, keyframe='a', me=0)
    set_keyer_fly_keyframe(conn, keyer=0, keyframe='b', me=0)
    set_keyer_fly_keyframe(conn, keyer=0, keyframe='full', me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'SFKF'
    assert conn.sent[0][10] == 1   # 'a' -> A
    assert conn.sent[1][10] == 2   # 'b' -> B
    assert conn.sent[2][10] == 3   # 'full'


def test_set_keyer_fly_keyframe_unknown_raises():
    conn = _FakeConn()
    with pytest.raises(ValueError, match='unknown keyframe'):
        set_keyer_fly_keyframe(conn, keyer=0, keyframe='unknown', me=0)


def test_run_flying_key_keyframe_packs_rflk():
    """Run-to-keyframe: same 'a' / 'b' / 'full' string lookup; packet
    code RFlK."""
    conn = _FakeConn()
    run_flying_key_keyframe(conn, keyer=0, keyframe='a', me=0)
    run_flying_key_keyframe(conn, keyer=0, keyframe='full', me=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'RFlK'


def test_run_flying_key_keyframe_unknown_raises():
    conn = _FakeConn()
    with pytest.raises(ValueError, match='unknown keyframe'):
        run_flying_key_keyframe(conn, keyer=0, keyframe='not_real', me=0)


def test_run_flying_key_infinite_direction_includes_direction():
    """Infinite variant always uses keyframe_constant('runToInfinite')
    and packs the numeric direction."""
    conn = _FakeConn()
    run_flying_key_infinite_direction(conn, keyer=0, direction=5, me=0)
    assert conn.sent[0][4:8] == b'RFlK'


# --- Reader: usk_fly ------------------------------------------------------

def test_usk_fly_default_when_missing():
    """Both key-properties-fly and key-properties-base absent → all-False."""
    result = usk_fly({}, 0, 0)
    assert result['fly_enabled'] is False
    assert result['fly_keyframe_a_stored'] is False
    assert result['fly_is_at_keyframe_a'] is False


def test_usk_fly_reads_enable_from_base():
    """fly_enabled lives on key-properties-base (alongside type / fill /
    key sources), separate from the keyframe-storage state on
    key-properties-fly. Reader combines both."""
    mx = {
        'key-properties-base': {0: {0: _KeBP(fly_enabled=True)}},
        'key-properties-fly': {0: {0: _KeFP(
            is_a_set=True, at_keyframe_b=True,
        )}},
    }
    result = usk_fly(mx, 0, 0)
    assert result['fly_enabled'] is True
    assert result['fly_keyframe_a_stored'] is True
    assert result['fly_is_at_keyframe_b'] is True
    assert result['fly_is_at_keyframe_a'] is False


def test_usk_fly_enabled_without_fly_state():
    """fly_enabled True but no key-properties-fly entry → enabled is
    propagated from base; the rest of the dict defaults to False."""
    mx = {'key-properties-base': {0: {0: _KeBP(fly_enabled=True)}}}
    result = usk_fly(mx, 0, 0)
    assert result['fly_enabled'] is True
    assert result['fly_keyframe_a_stored'] is False
