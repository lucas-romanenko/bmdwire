# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""Macro codec — USK ops (key type, on-air, fill/cut, luma, mask,
fly enable, DVE position/size/border/shadow, mask edges, DVE & fly
key rate, advanced chroma key, fly keyframes).
"""

from atemwire.macrotransfer._helpers import (
    _u16le, _u16le_bytes, _resolve_source_to_symbol,
    _resolve_symbol_to_source, _bool_str, _bool_byte, _fmt_scalar,
    _pack_fixed_value, _pack_signed_fixed_value, _d_fixed_value,
    _d_signed_fixed_value, _d_me_keyer_bool, _e_me_keyer_bool,
    _d_me_keyer_u16, _e_me_keyer_u16, _d_me_keyer_marker_fp,
    _e_me_keyer_marker_fp, _FLYKEY_LOCATION_NAMES, _FLYKEY_LOCATION_TO_INT,
    _USK_TYPE_NAMES, _USK_TYPE_TO_INT, _DVE_MARKER,
)

def _d_key_type(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'type': _USK_TYPE_NAMES.get(params[2], str(params[2]))}

def _d_key_on_air(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'onAir': _bool_str(_u16le(params, 2))}

def _d_key_fill_input(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'input': _resolve_source_to_symbol(_u16le(params, 2), mx)}

def _d_key_cut_input(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'input': _resolve_source_to_symbol(_u16le(params, 2), mx)}

def _d_key_mask_enable(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'enable': _bool_str(params[2])}

def _d_key_fly_enable(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'enable': _bool_str(params[2])}

def _d_luma_key_clip(params, mx):
    # [u8 mE, u8 keyer, u16 LE marker, u32 LE value]. value is the 0..1
    # clip fraction in Q16.16 fixed point (value = fraction × 65536, i.e.
    # per-mille wire 0..1000 × 65.536). Software Control's XML stores the
    # same 0..1 fraction, so bytecode / 65536 reproduces it. (The earlier
    # 655360 divisor was 10× off: it set 100% on the ATEM for a 30% macro.)
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'clip': _fmt_scalar(_d_fixed_value(params, 65536.0))}

def _d_luma_key_gain(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'gain': _fmt_scalar(_d_fixed_value(params, 65536.0))}

def _d_luma_key_invert(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'invert': _bool_str(params[2])}

def _d_luma_key_pre_multiply(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'preMultiply': _bool_str(params[2])}

# DVE / fly key — sizes are 0..2 scalar (unsigned), positions are signed
# ±~16. The bytecode value is wire-form × 65.536, where wire-form is
# 1000ths (DVE size: 0..2000; DVE position: ±16000). XML scaler:
# bytecode / 65536. Mask edges share the same shape (signed, ±9 / ±16).

def _d_dve_xsize(params, mx):
    # XML scale 0..2; wire 0..2000; divisor 65536.
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'xSize': _fmt_scalar(_d_fixed_value(params, 65536.0))}

def _d_dve_ysize(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'ySize': _fmt_scalar(_d_fixed_value(params, 65536.0))}

def _d_dve_xpos(params, mx):
    # Signed: position can be negative. wire ±16000 → XML ±16, divisor 65536.
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'xPosition': _fmt_scalar(_d_signed_fixed_value(params, 65536.0))}

def _d_dve_ypos(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'yPosition': _fmt_scalar(_d_signed_fixed_value(params, 65536.0))}

def _d_dve_mask_enable(params, mx):
    # u8 mE, u8 keyer, u8 enable, u8 _.
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'enable': _bool_str(params[2])}

def _d_dve_mask_top(params, mx):
    # Signed mask edge. wire ±9000 → XML ±9, divisor 65536.
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'top': _fmt_scalar(_d_signed_fixed_value(params, 65536.0))}

def _d_dve_mask_bottom(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'bottom': _fmt_scalar(_d_signed_fixed_value(params, 65536.0))}

def _d_dve_mask_left(params, mx):
    # wire ±16000 → XML ±16, divisor 65536.
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'left': _fmt_scalar(_d_signed_fixed_value(params, 65536.0))}

def _d_dve_mask_right(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'keyIndex': str(params[1]),
            'right': _fmt_scalar(_d_signed_fixed_value(params, 65536.0))}

# --- USK mask edges (4 ops — KeyMaskEnable already mapped) ---


def _d_usk_mask_top(params, mx):
    return _d_me_keyer_marker_fp(params, mx, 'top')

def _e_usk_mask_top(attrs):
    return _e_me_keyer_marker_fp(attrs, 'top')

def _d_usk_mask_bottom(params, mx):
    return _d_me_keyer_marker_fp(params, mx, 'bottom')

def _e_usk_mask_bottom(attrs):
    return _e_me_keyer_marker_fp(attrs, 'bottom')

def _d_usk_mask_left(params, mx):
    return _d_me_keyer_marker_fp(params, mx, 'left')

def _e_usk_mask_left(attrs):
    return _e_me_keyer_marker_fp(attrs, 'left')

def _d_usk_mask_right(params, mx):
    return _d_me_keyer_marker_fp(params, mx, 'right')

def _e_usk_mask_right(attrs):
    return _e_me_keyer_marker_fp(attrs, 'right')

# --- DVE Fly key rate ---


def _d_dve_flykey_rate(params, mx):
    return _d_me_keyer_u16(params, mx, 'rate')

def _e_dve_flykey_rate(attrs):
    return _e_me_keyer_u16(attrs, 'rate')

# --- USK DVE border (9 ops) ---


def _d_dve_border_enable(params, mx):
    return _d_me_keyer_bool(params, mx, 'enable')

def _e_dve_border_enable(attrs):
    return _e_me_keyer_bool(attrs, 'enable')

def _d_dve_border_hue(params, mx):
    return _d_me_keyer_marker_fp(params, mx, 'hue')

def _e_dve_border_hue(attrs):
    return _e_me_keyer_marker_fp(attrs, 'hue')

def _d_dve_border_saturation(params, mx):
    return _d_me_keyer_marker_fp(params, mx, 'saturation')

def _e_dve_border_saturation(attrs):
    return _e_me_keyer_marker_fp(attrs, 'saturation')

def _d_dve_border_luminescence(params, mx):
    return _d_me_keyer_marker_fp(params, mx, 'luminescence')

def _e_dve_border_luminescence(attrs):
    return _e_me_keyer_marker_fp(attrs, 'luminescence')

def _d_dve_border_inner_width(params, mx):
    return _d_me_keyer_marker_fp(params, mx, 'innerWidth')

def _e_dve_border_inner_width(attrs):
    return _e_me_keyer_marker_fp(attrs, 'innerWidth')

def _d_dve_border_outer_width(params, mx):
    return _d_me_keyer_marker_fp(params, mx, 'outerWidth')

def _e_dve_border_outer_width(attrs):
    return _e_me_keyer_marker_fp(attrs, 'outerWidth')

def _d_dve_border_inner_softness(params, mx):
    return _d_me_keyer_u16(params, mx, 'innerSoftness')

def _e_dve_border_inner_softness(attrs):
    return _e_me_keyer_u16(attrs, 'innerSoftness')

def _d_dve_border_outer_softness(params, mx):
    return _d_me_keyer_u16(params, mx, 'outerSoftness')

def _e_dve_border_outer_softness(attrs):
    return _e_me_keyer_u16(attrs, 'outerSoftness')

def _d_dve_border_opacity(params, mx):
    return _d_me_keyer_u16(params, mx, 'opacity')

def _e_dve_border_opacity(attrs):
    return _e_me_keyer_u16(attrs, 'opacity')

# --- USK DVE shadow (3 ops) ---


def _d_dve_shadow_enable(params, mx):
    return _d_me_keyer_bool(params, mx, 'enable')

def _e_dve_shadow_enable(attrs):
    return _e_me_keyer_bool(attrs, 'enable')

def _d_dve_shadow_direction(params, mx):
    return _d_me_keyer_marker_fp(params, mx, 'direction')

def _e_dve_shadow_direction(attrs):
    return _e_me_keyer_marker_fp(attrs, 'direction')

def _d_dve_shadow_altitude(params, mx):
    return _d_me_keyer_u16(params, mx, 'altitude')

def _e_dve_shadow_altitude(attrs):
    return _e_me_keyer_u16(attrs, 'altitude')

# --- Tier 2: Advanced Chroma Key (16 ops) ---
# 13 use marker 0x000c at offset 2; 3 (cursor X/Y/size) use 0x026c.
# 2 are booleans (preview/sampling enabled) with op-specific byte 3.


def _d_ack_fp_000c(params, mx, attr_name):
    return _d_me_keyer_marker_fp(params, mx, attr_name)

def _e_ack_fp_000c(attrs, attr_name):
    return _e_me_keyer_marker_fp(attrs, attr_name, marker_u16=0x000C)

def _d_ack_fp_026c(params, mx, attr_name):
    return _d_me_keyer_marker_fp(params, mx, attr_name)

def _e_ack_fp_026c(attrs, attr_name):
    return _e_me_keyer_marker_fp(attrs, attr_name, marker_u16=0x026C)

def _d_ack_foreground_level(params, mx):
    return _d_ack_fp_000c(params, mx, 'foregroundLevel')

def _e_ack_foreground_level(attrs):
    return _e_ack_fp_000c(attrs, 'foregroundLevel')

def _d_ack_background_level(params, mx):
    return _d_ack_fp_000c(params, mx, 'backgroundLevel')

def _e_ack_background_level(attrs):
    return _e_ack_fp_000c(attrs, 'backgroundLevel')

def _d_ack_key_edge(params, mx):
    return _d_ack_fp_000c(params, mx, 'keyEdge')

def _e_ack_key_edge(attrs):
    return _e_ack_fp_000c(attrs, 'keyEdge')

def _d_ack_spill_suppress(params, mx):
    return _d_ack_fp_000c(params, mx, 'spillSuppress')

def _e_ack_spill_suppress(attrs):
    return _e_ack_fp_000c(attrs, 'spillSuppress')

def _d_ack_flare_suppress(params, mx):
    return _d_ack_fp_000c(params, mx, 'flareSuppress')

def _e_ack_flare_suppress(attrs):
    return _e_ack_fp_000c(attrs, 'flareSuppress')

def _d_ack_foreground_brightness(params, mx):
    return _d_ack_fp_000c(params, mx, 'foregroundBrightness')

def _e_ack_foreground_brightness(attrs):
    return _e_ack_fp_000c(attrs, 'foregroundBrightness')

def _d_ack_foreground_contrast(params, mx):
    return _d_ack_fp_000c(params, mx, 'foregroundContrast')

def _e_ack_foreground_contrast(attrs):
    return _e_ack_fp_000c(attrs, 'foregroundContrast')

def _d_ack_foreground_colour(params, mx):
    return _d_ack_fp_000c(params, mx, 'foregroundColour')

def _e_ack_foreground_colour(attrs):
    return _e_ack_fp_000c(attrs, 'foregroundColour')

def _d_ack_foreground_red(params, mx):
    return _d_ack_fp_000c(params, mx, 'foregroundRed')

def _e_ack_foreground_red(attrs):
    return _e_ack_fp_000c(attrs, 'foregroundRed')

def _d_ack_foreground_green(params, mx):
    return _d_ack_fp_000c(params, mx, 'foregroundGreen')

def _e_ack_foreground_green(attrs):
    return _e_ack_fp_000c(attrs, 'foregroundGreen')

def _d_ack_foreground_blue(params, mx):
    return _d_ack_fp_000c(params, mx, 'foregroundBlue')

def _e_ack_foreground_blue(attrs):
    return _e_ack_fp_000c(attrs, 'foregroundBlue')

def _d_ack_sampling_mode_enabled(params, mx):
    return _d_me_keyer_bool(params, mx, 'enabled')

def _e_ack_sampling_mode_enabled(attrs):
    return _e_me_keyer_bool(attrs, 'enabled', byte3_marker=0x02)

def _d_ack_preview_enabled(params, mx):
    return _d_me_keyer_bool(params, mx, 'enabled')

def _e_ack_preview_enabled(attrs):
    return _e_me_keyer_bool(attrs, 'enabled', byte3_marker=0x04)

def _d_ack_cursor_x(params, mx):
    return _d_ack_fp_026c(params, mx, 'xPosition')

def _e_ack_cursor_x(attrs):
    return _e_ack_fp_026c(attrs, 'xPosition')

def _d_ack_cursor_y(params, mx):
    return _d_ack_fp_026c(params, mx, 'yPosition')

def _e_ack_cursor_y(attrs):
    return _e_ack_fp_026c(attrs, 'yPosition')

def _d_ack_cursor_size(params, mx):
    return _d_ack_fp_026c(params, mx, 'size')

def _e_ack_cursor_size(attrs):
    return _e_ack_fp_026c(attrs, 'size')

# --- FlyKey keyframe ops (4 ops) ---


def _d_flykey_set_keyframe(params, mx):
    return _d_me_keyer_u16(params, mx, 'keyFrameIndex')

def _e_flykey_set_keyframe(attrs):
    return _e_me_keyer_u16(attrs, 'keyFrameIndex')

def _d_flykey_run_to_keyframe(params, mx):
    return _d_me_keyer_u16(params, mx, 'keyFrameIndex')

def _e_flykey_run_to_keyframe(attrs):
    return _e_me_keyer_u16(attrs, 'keyFrameIndex')

def _d_flykey_run_to_full(params, mx):
    return {
        'mixEffectBlockIndex': str(params[0]),
        'keyIndex': str(params[1]),
    }

def _e_flykey_run_to_full(attrs):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    keyer = int(attrs['keyIndex']) & 0xFF
    return bytes([me, keyer]) + _u16le_bytes(0x024A)

def _d_flykey_run_to_infinity(params, mx):
    loc_int = params[2]
    return {
        'mixEffectBlockIndex': str(params[0]),
        'keyIndex': str(params[1]),
        'location': _FLYKEY_LOCATION_NAMES.get(loc_int, str(loc_int)),
    }

def _e_flykey_run_to_infinity(attrs):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    keyer = int(attrs['keyIndex']) & 0xFF
    loc = attrs['location']
    loc_int = (_FLYKEY_LOCATION_TO_INT.get(loc) if isinstance(loc, str)
               else None)
    if loc_int is None:
        try:
            loc_int = int(loc)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown FlyKey location {loc!r}") from exc
    return bytes([me, keyer, loc_int & 0xFF, 0x02])

def _e_key_type(attrs):
    # byte 3 is uninitialized memory in SC's emit; fresh recordings from a production switcher
    # show 0x03 consistently. ATEM ignores it.
    type_name = attrs.get('type', 'Luma')
    type_int = (_USK_TYPE_TO_INT.get(type_name, 0)
                if isinstance(type_name, str) else int(type_name))
    return bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                  int(attrs['keyIndex']), type_int, 0x03])

def _e_key_on_air(attrs):
    on = 1 if str(attrs['onAir']).lower() in ('true', '1') else 0
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(on))

def _e_key_fill_input(attrs):
    src = _resolve_symbol_to_source(attrs['input'])
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(src))

def _e_key_cut_input(attrs):
    src = _resolve_symbol_to_source(attrs['input'])
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(src))

def _e_key_mask_enable(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _bool_byte(attrs['enable']) + b'\x00')

def _e_key_fly_enable(attrs):
    # byte 3 = 0x03 in fresh recordings from a production switcher (uninitialized memory).
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _bool_byte(attrs['enable']) + b'\x03')

# Fixed-value scalar ops (luma clip/gain). Wire is per-mille (0..1000),
# XML is the 0..1 fraction; the macro u32 = fraction × 65536 (Q16.16).
# (Was 655360 — a 10× error that made a 30% macro set 100% on the ATEM.)

_LUMA_CLIP_GAIN_DIVISOR = 65536.0

# u16 at offset 2 of the params block is uninitialized memory in
# Software Control's bytecode emit (like the FadeToBlackEnabled mEBI
# byte). Observed values: 0x0000 from older recordings (slot 0 'Deal
# Cam' fixture), 0x000c from fresh recordings from a production switcher (Lots XML). Match
# the live default so most round-trips are byte-identical; either
# value is functionally equivalent (ATEM ignores it).

_LUMA_CLIP_GAIN_MARKER = 0x000C

def _e_luma_key_clip(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(_LUMA_CLIP_GAIN_MARKER)
            + _pack_fixed_value(attrs['clip'], _LUMA_CLIP_GAIN_DIVISOR))

def _e_luma_key_gain(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(_LUMA_CLIP_GAIN_MARKER)
            + _pack_fixed_value(attrs['gain'], _LUMA_CLIP_GAIN_DIVISOR))

def _e_luma_key_invert(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _bool_byte(attrs['invert']) + b'\x00')

def _e_luma_key_pre_multiply(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _bool_byte(attrs['preMultiply']) + b'\x00')

# DVE / fly key fixed-value scalar ops. XML form maps to wire via
# divisor 65536 (size 0..2 / position ±16 / mask edges ±9 or ±16).
# Marker u16 at params offset 2 is uninitialized memory in Software
# Control's emit; 0x000c matches fresh recordings from a production switcher.

_DVE_DIVISOR = 65536.0

# _DVE_MARKER is shared with downstream_keyer — imported from _helpers.


def _e_dve_xsize(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(_DVE_MARKER)
            + _pack_fixed_value(attrs['xSize'], _DVE_DIVISOR))

def _e_dve_ysize(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(_DVE_MARKER)
            + _pack_fixed_value(attrs['ySize'], _DVE_DIVISOR))

def _e_dve_xpos(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(_DVE_MARKER)
            + _pack_signed_fixed_value(attrs['xPosition'], _DVE_DIVISOR))

def _e_dve_ypos(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(_DVE_MARKER)
            + _pack_signed_fixed_value(attrs['yPosition'], _DVE_DIVISOR))

def _e_dve_mask_enable(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _bool_byte(attrs['enable']) + b'\x00')

def _e_dve_mask_top(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(_DVE_MARKER)
            + _pack_signed_fixed_value(attrs['top'], _DVE_DIVISOR))

def _e_dve_mask_bottom(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(_DVE_MARKER)
            + _pack_signed_fixed_value(attrs['bottom'], _DVE_DIVISOR))

def _e_dve_mask_left(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(_DVE_MARKER)
            + _pack_signed_fixed_value(attrs['left'], _DVE_DIVISOR))

def _e_dve_mask_right(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)),
                   int(attrs['keyIndex'])])
            + _u16le_bytes(_DVE_MARKER)
            + _pack_signed_fixed_value(attrs['right'], _DVE_DIVISOR))
