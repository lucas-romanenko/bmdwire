# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""Macro codec — switching ops (bus, transition, ftb, aux, color
generator, video mode).

Decoders consume the bytecode parameter slice and return XML
attribute dicts; encoders consume the XML attribute dict and return
the raw parameter bytes. The shape mirrors what Software Control
emits in its XML ``<Op>`` elements.
"""

from atemwire.macrotransfer._helpers import (
    _u16le, _u16le_bytes, _resolve_source_to_symbol,
    _resolve_symbol_to_source, _d_me_marker_u16, _e_me_marker_u16,
    _d_me_marker_source, _e_me_marker_source, _d_me_value_marker_bool,
    _e_me_value_marker_bool, _d_me_double_marker_fp, _e_me_double_marker_fp,
    _d_gen_marker_fp, _e_gen_marker_fp, _WIPE_PATTERN_NAMES,
    _WIPE_PATTERN_TO_INT, _DVE_PATTERN_NAMES, _DVE_PATTERN_TO_INT, _FAM_MIX,
    _FAM_DIP, _FAM_WIPE, _FAM_DVE, _FAM_STINGER, _FAM_FTB_RATE,
    _OP_MARKER_026C,
)

# Per-op decoders. Each takes (params: bytes, mx: dict) and returns
# attrs as a dict (excluding the 'id' key). The order of dict insertion
# matches Software Control's observed XML attribute ordering.

# --- Bus / transition ---


def _d_program_input(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'input': _resolve_source_to_symbol(_u16le(params, 2), mx)}

def _d_preview_input(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'input': _resolve_source_to_symbol(_u16le(params, 2), mx)}

def _d_auxiliary_input(params, mx):
    """``AuxiliaryInput`` — set an aux output's source.

    Bytecode: ``u8 auxiliaryIndex, u8 _, u16 LE source``. The XML
    attribute name is ``auxiliaryIndex`` (0-indexed; UI Aux 1 = 0)
    and the input is a symbolic source name. Confirmed against a
    Software Control export that contained two AuxiliaryInput ops.
    """
    return {'auxiliaryIndex': str(params[0]),
            'input': _resolve_source_to_symbol(_u16le(params, 2), mx)}

def _d_cut(params, mx):
    return {'mixEffectBlockIndex': str(params[0])}

def _d_auto(params, mx):
    return {'mixEffectBlockIndex': str(params[0])}

def _d_fade_to_black(params, mx):
    return {'mixEffectBlockIndex': str(params[0])}

_TRANSITION_STYLES = {0: 'Mix', 1: 'Dip', 2: 'Wipe', 3: 'DVE', 4: 'Stinger'}

def _d_transition_style(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'style': _TRANSITION_STYLES.get(params[1], str(params[1]))}

def _resolve_layer_mask(mask):
    parts = []
    if mask & 0x01:
        parts.append('Background')
    for k in range(1, 5):
        if mask & (1 << k):
            parts.append(f'Key{k}')
    return ','.join(parts) if parts else 'Background'

def _d_transition_source(params, mx):
    return {'mixEffectBlockIndex': str(params[0]),
            'source': _resolve_layer_mask(_u16le(params, 2))}

# --- Upstream keyer types (moved to _helpers.py — used across switching
# and upstream_keyer files via _USK_TYPE_NAMES / _USK_TYPE_TO_INT) ---

# --- Fade-to-black enable (FEna recording capture) ---

# Wire layout: u8 mixEffectBlockIndex, u8 enabled, u16 padding.
# Verified live on a production switcher by recording FEna(enable=True/False) into a
# free macro slot and inspecting the captured bytecode. Each FEna
# emit produces multiple op-0x0202 entries; the decoder treats each
# as one <Op> and round-trips byte-for-byte.
#
# Note on `mixEffectBlockIndex`: Software Control's XML emits values
# that look like uninitialized memory (observed 224, 14, 67, 139, 177,
# 170, 175, 129 across recordings of the same operator action). The
# ATEM ignores this byte at apply time. The decoder reads it
# unchanged; the encoder writes it back unchanged. No validation, no
# normalization — preserves byte-identical round-trip across save →
# restore cycles even when SC's emitted byte is garbage.


def _d_ftb_enabled(params, mx):
    return {
        'mixEffectBlockIndex': str(params[0]),
        'enabled': 'True' if params[1] else 'False',
    }

# --- Video mode ---

# Wire layout: u8 mode index (matches VideoModeField), 3 bytes pad.
# Verified live on a production switcher (recorded CVdM at current mode → bytecode op
# 0x000c with params `<u8 mode><3 pad>`).
#
# Note: op-code 0x000c also surfaces as an auto-prologue at the head
# of macros recorded by Software Control — it captures the video mode
# that was active when recording began. The decoder treats both cases
# identically. On round-trip via Profile.from_atem, that prologue
# surfaces as an extra `<Op id="VideoMode"/>` at the head of any macro
# that didn't have one in the source XML; harmless because the value
# is the current mode (replaying the macro re-asserts the same mode,
# a functional no-op).

# Canonical int → XML name. Software Control prefers the numerical
# form (`525i5994` over `NTSC`); pick those for the reverse map even
# though `set_video_mode` accepts both.

_VIDEO_MODE_INT_TO_NAME = {
    0: '525i5994', 1: '625i50',
    2: 'NTSC_widescreen', 3: 'PAL_widescreen',
    4: '720p50', 5: '720p5994', 28: '720p60',
    6: '1080i50', 7: '1080i5994', 29: '1080i60',
    8: '1080p2398', 9: '1080p24', 10: '1080p25',
    11: '1080p2997', 26: '1080p30',
    12: '1080p50', 13: '1080p5994', 27: '1080p60',
    14: '2160p2398', 15: '2160p24', 16: '2160p25', 17: '2160p2997',
    18: '2160p50', 19: '2160p5994',
    20: '4320p2398', 21: '4320p24', 22: '4320p25', 23: '4320p2997',
    24: '4320p50', 25: '4320p5994',
}

def _d_video_mode(params, mx):
    mode_int = params[0]
    return {'videoMode': _VIDEO_MODE_INT_TO_NAME.get(mode_int, str(mode_int))}

def _d_transition_mix_rate(params, mx):
    return _d_me_marker_u16(params, mx, 'rate')

def _e_transition_mix_rate(attrs):
    return _e_me_marker_u16(attrs, 'rate', _FAM_MIX)

def _d_transition_dip_rate(params, mx):
    return _d_me_marker_u16(params, mx, 'rate')

def _e_transition_dip_rate(attrs):
    return _e_me_marker_u16(attrs, 'rate', _FAM_DIP)

def _d_transition_dip_input(params, mx):
    return _d_me_marker_source(params, mx, 'input')

def _e_transition_dip_input(attrs):
    return _e_me_marker_source(attrs, _FAM_DIP, 'input')

def _d_transition_wipe_rate(params, mx):
    return _d_me_marker_u16(params, mx, 'rate')

def _e_transition_wipe_rate(attrs):
    return _e_me_marker_u16(attrs, 'rate', _FAM_WIPE)

def _d_transition_wipe_pattern(params, mx):
    pattern_int = _u16le(params, 2)
    return {
        'mixEffectBlockIndex': str(params[0]),
        'pattern': _WIPE_PATTERN_NAMES.get(pattern_int, str(pattern_int)),
    }

def _e_transition_wipe_pattern(attrs):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    pat = attrs['pattern']
    pat_int = (_WIPE_PATTERN_TO_INT.get(pat) if isinstance(pat, str)
               else None)
    if pat_int is None:
        try:
            pat_int = int(pat)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown wipe pattern {pat!r}") from exc
    return bytes([me, _FAM_WIPE]) + _u16le_bytes(pat_int)

def _d_transition_wipe_border_softness(params, mx):
    return _d_me_double_marker_fp(params, mx, 'softness')

def _e_transition_wipe_border_softness(attrs):
    return _e_me_double_marker_fp(attrs, 'softness', _FAM_WIPE, _OP_MARKER_026C)

def _d_transition_wipe_border_width(params, mx):
    return _d_me_double_marker_fp(params, mx, 'width')

def _e_transition_wipe_border_width(attrs):
    return _e_me_double_marker_fp(attrs, 'width', _FAM_WIPE, _OP_MARKER_026C)

def _d_transition_wipe_border_fill_input(params, mx):
    return _d_me_marker_source(params, mx, 'input')

def _e_transition_wipe_border_fill_input(attrs):
    return _e_me_marker_source(attrs, _FAM_WIPE, 'input')

def _d_transition_wipe_x_position(params, mx):
    return _d_me_double_marker_fp(params, mx, 'xPosition')

def _e_transition_wipe_x_position(attrs):
    return _e_me_double_marker_fp(attrs, 'xPosition', _FAM_WIPE, _OP_MARKER_026C)

def _d_transition_wipe_y_position(params, mx):
    return _d_me_double_marker_fp(params, mx, 'yPosition')

def _e_transition_wipe_y_position(attrs):
    return _e_me_double_marker_fp(attrs, 'yPosition', _FAM_WIPE, _OP_MARKER_026C)

def _d_transition_wipe_dve_flipflop(params, mx):
    return _d_me_value_marker_bool(params, mx, 'flipFlop')

def _e_transition_wipe_dve_flipflop(attrs):
    return _e_me_value_marker_bool(attrs, 'flipFlop', _OP_MARKER_026C)

def _d_transition_wipe_dve_reverse(params, mx):
    return _d_me_value_marker_bool(params, mx, 'reverse')

def _e_transition_wipe_dve_reverse(attrs):
    return _e_me_value_marker_bool(attrs, 'reverse', _OP_MARKER_026C)

def _d_transition_dve_rate(params, mx):
    return _d_me_marker_u16(params, mx, 'rate')

def _e_transition_dve_rate(attrs):
    return _e_me_marker_u16(attrs, 'rate', _FAM_DVE)

def _d_transition_dve_pattern(params, mx):
    pat_int = params[1]
    return {
        'mixEffectBlockIndex': str(params[0]),
        'pattern': _DVE_PATTERN_NAMES.get(pat_int, str(pat_int)),
    }

def _e_transition_dve_pattern(attrs):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    pat = attrs['pattern']
    pat_int = (_DVE_PATTERN_TO_INT.get(pat) if isinstance(pat, str)
               else None)
    if pat_int is None:
        try:
            pat_int = int(pat)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown DVE pattern {pat!r}") from exc
    return bytes([me, pat_int & 0xFF]) + _u16le_bytes(_OP_MARKER_026C)

def _d_transition_dve_fill_input(params, mx):
    return _d_me_marker_source(params, mx, 'input')

def _e_transition_dve_fill_input(attrs):
    return _e_me_marker_source(attrs, _FAM_DVE, 'input')

def _d_transition_dve_cut_input(params, mx):
    return _d_me_marker_source(params, mx, 'input')

def _e_transition_dve_cut_input(attrs):
    return _e_me_marker_source(attrs, _FAM_DVE, 'input')

def _d_transition_dve_cut_input_enable(params, mx):
    return _d_me_value_marker_bool(params, mx, 'enable')

def _e_transition_dve_cut_input_enable(attrs):
    return _e_me_value_marker_bool(attrs, 'enable', _OP_MARKER_026C)

def _d_transition_stinger_source_mediaplayer(params, mx):
    return {
        'mixEffectBlockIndex': str(params[0]),
        'mediaPlayer': str(_u16le(params, 2)),
    }

def _e_transition_stinger_source_mediaplayer(attrs):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    return bytes([me, _FAM_STINGER]) + _u16le_bytes(int(attrs['mediaPlayer']))

def _d_transition_stinger_pre_roll(params, mx):
    return _d_me_marker_u16(params, mx, 'preRoll')

def _e_transition_stinger_pre_roll(attrs):
    return _e_me_marker_u16(attrs, 'preRoll', _FAM_STINGER)

def _d_transition_stinger_clip_duration(params, mx):
    return _d_me_marker_u16(params, mx, 'clipDuration')

def _e_transition_stinger_clip_duration(attrs):
    return _e_me_marker_u16(attrs, 'clipDuration', _FAM_STINGER)

def _d_transition_stinger_trigger_point(params, mx):
    return _d_me_marker_u16(params, mx, 'triggerPoint')

def _e_transition_stinger_trigger_point(attrs):
    return _e_me_marker_u16(attrs, 'triggerPoint', _FAM_STINGER)

def _d_transition_stinger_mix_rate(params, mx):
    return _d_me_marker_u16(params, mx, 'mixRate')

def _e_transition_stinger_mix_rate(attrs):
    return _e_me_marker_u16(attrs, 'mixRate', _FAM_STINGER)

def _d_transition_stinger_dve_clip(params, mx):
    return _d_me_double_marker_fp(params, mx, 'clip')

def _e_transition_stinger_dve_clip(attrs):
    return _e_me_double_marker_fp(attrs, 'clip', _FAM_STINGER, _OP_MARKER_026C)

def _d_transition_stinger_dve_gain(params, mx):
    return _d_me_double_marker_fp(params, mx, 'gain')

def _e_transition_stinger_dve_gain(attrs):
    return _e_me_double_marker_fp(attrs, 'gain', _FAM_STINGER, _OP_MARKER_026C)

def _d_transition_stinger_dve_invert(params, mx):
    return _d_me_value_marker_bool(params, mx, 'invert')

def _e_transition_stinger_dve_invert(attrs):
    return _e_me_value_marker_bool(attrs, 'invert', _OP_MARKER_026C)

def _d_transition_stinger_dve_premultiply(params, mx):
    return _d_me_value_marker_bool(params, mx, 'preMultiply')

def _e_transition_stinger_dve_premultiply(attrs):
    return _e_me_value_marker_bool(attrs, 'preMultiply', _OP_MARKER_026C)

def _d_color_gen_hue(params, mx):
    return _d_gen_marker_fp(params, mx, 'hue')

def _e_color_gen_hue(attrs):
    return _e_gen_marker_fp(attrs, 'hue')

def _d_color_gen_saturation(params, mx):
    return _d_gen_marker_fp(params, mx, 'saturation')

def _e_color_gen_saturation(attrs):
    return _e_gen_marker_fp(attrs, 'saturation')

def _d_color_gen_luminescence(params, mx):
    return _d_gen_marker_fp(params, mx, 'luminescence')

def _e_color_gen_luminescence(attrs):
    return _e_gen_marker_fp(attrs, 'luminescence')

def _d_ftb_rate(params, mx):
    return _d_me_marker_u16(params, mx, 'rate')

def _e_ftb_rate(attrs):
    return _e_me_marker_u16(attrs, 'rate', _FAM_FTB_RATE)

# ============================================================================
# Per-op encoders. Each takes ``attrs`` (the XML <Op> attribute dict, which
# the decoder produces) and returns the parameter bytes (without the 4-byte
# length+op_type header — encode_macro_bytecode prepends that).
#
# Pairs 1:1 with the ``_d_*`` decoder above each entry. The output is what
# the live ATEM stores; round-tripping a known-good bytecode through
# decode → encode → upload → download should give bytes that match the
# original (modulo source-symbol canonicalisation when multiple IDs map
# to the same name).
# ============================================================================

# --- Bus / transition ---


def _e_program_input(attrs):
    src = _resolve_symbol_to_source(attrs['input'])
    return bytes([int(attrs.get('mixEffectBlockIndex', 0)), 0]) + _u16le_bytes(src)

def _e_preview_input(attrs):
    src = _resolve_symbol_to_source(attrs['input'])
    return bytes([int(attrs.get('mixEffectBlockIndex', 0)), 0]) + _u16le_bytes(src)

def _e_auxiliary_input(attrs):
    src = _resolve_symbol_to_source(attrs['input'])
    return bytes([int(attrs['auxiliaryIndex']), 0]) + _u16le_bytes(src)

def _e_cut(attrs):
    return bytes([int(attrs.get('mixEffectBlockIndex', 0)), 0, 0, 0])

def _e_auto(attrs):
    return bytes([int(attrs.get('mixEffectBlockIndex', 0)), 0, 0, 0])

def _e_fade_to_black(attrs):
    return bytes([int(attrs.get('mixEffectBlockIndex', 0)), 0, 0, 0])

_TRANSITION_STYLE_TO_INT = {v: k for k, v in _TRANSITION_STYLES.items()}

def _e_transition_style(attrs):
    style = attrs.get('style', 'Mix')
    style_int = _TRANSITION_STYLE_TO_INT.get(style, 0) if isinstance(style, str) else int(style)
    return bytes([int(attrs.get('mixEffectBlockIndex', 0)), style_int, 0, 0])

def _pack_layer_mask(s) -> int:
    """Inverse of ``_resolve_layer_mask``."""
    parts = {p.strip() for p in (s or '').split(',') if p.strip()}
    mask = 0
    if 'Background' in parts:
        mask |= 0x01
    for k in range(1, 5):
        if f'Key{k}' in parts:
            mask |= (1 << k)
    return mask

def _e_transition_source(attrs):
    return (bytes([int(attrs.get('mixEffectBlockIndex', 0)), 0])
            + _u16le_bytes(_pack_layer_mask(attrs.get('source', ''))))

# --- Fade-to-black enable ---


def _e_ftb_enabled(attrs):
    # mixEffectBlockIndex is an uninitialized-memory byte the ATEM
    # ignores; round-trip whatever Software Control wrote (mod 256).
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    enabled = str(attrs['enabled']).strip().lower() in ('true', '1', 'yes')
    return bytes([me, 1 if enabled else 0, 0, 0])

# --- Video mode ---


def _e_video_mode(attrs):
    from atemwire.messages.input_video import VIDEO_MODE_NAMES
    mode = attrs['videoMode']
    if isinstance(mode, str):
        if mode in VIDEO_MODE_NAMES:
            mode_int = VIDEO_MODE_NAMES[mode]
        else:
            try:
                mode_int = int(mode)
            except ValueError as exc:
                raise ValueError(f"unknown videoMode {mode!r}") from exc
    else:
        mode_int = int(mode)
    if not (0 <= mode_int <= 0xFF):
        raise ValueError(f"videoMode out of range: {mode_int}")
    return bytes([mode_int, 0, 0, 0])
