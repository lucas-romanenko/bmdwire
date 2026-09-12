# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""Macro codec — Fairlight audio-mixer ops (strip fader/mix,
master fader, EQ, compressor, limiter, expander, input gain,
dynamics gain, headphones).

The strip ops share a 12-byte source/sourceId prefix (see
``_strip_prefix_decode`` / ``_strip_prefix_encode``); per-op payload
appends after that. Family markers (``0x4346`` for fader/mix/EQ
gain/compressor/input gain/dynamics gain; ``0x03E1`` for EQ band
ops + limiter; ``0x024A`` for expander) distinguish the wire layout
the ATEM expects per op family.
"""

import struct

from atemwire.macrotransfer._helpers import (
    _u16le, _u32le, _u16le_bytes, _u32le_bytes, _i32le_bytes,
    _resolve_source_to_symbol, _resolve_symbol_to_source, _fmt_scalar,
    _FAIRLIGHT_FAM_4346, _FAIRLIGHT_FAM_03E1, _FAIRLIGHT_FAM_024A,
)

# --- Fairlight audio mixer ---

# Op-code 0x4346 appears at param offset 2-3 of every observed strip op
# (FairlightAudioMixerInputSourceFaderGain, ...MixType). Its meaning is
# unknown — possibly a wire-protocol-version marker or sub-channel index.
# The decoder ignores it and the encoder emits the observed constant.

_FAIRLIGHT_STRIP_MARKER = 0x4346

# Mix-type / state values per FairlightStripPropertiesCommand.

_MIX_TYPE_NAMES = {1: 'Off', 2: 'On', 4: 'AFV'}

_MIX_TYPE_TO_INT = {v: k for k, v in _MIX_TYPE_NAMES.items()}

# Trailing u16 at offset 14 of MixType ops. Live-recorded macros
# consistently show 0x000c here (verified on a production switcher with CFSP-state
# recordings); the decoder ignores it but the encoder emits it for
# round-trip-byte-identical recovery.

_FAIRLIGHT_MIX_TYPE_TRAILING_MARKER = 0x000C

# Fader gain conversion: the bytecode stores `gain_db × 65536` as a 4-byte
# i32 LE. Software Control's XML stores `gain_int / 65536` as a float
# string with full precision (e.g. -5243 ↔ "-0.0800018"). Verified by
# recording single-CFSP-volume macros on a live ATEM and matching the
# bytecode against the saved XML.

_FAIRLIGHT_GAIN_DIVISOR = 65536.0

def _d_fairlight_strip_fader_gain(params, mx):
    # u16 LE source, u16 marker, i64 LE sourceId, i32 LE gain × 65536.
    source = _u16le(params, 0)
    source_id = struct.unpack_from('<Q', params, 4)[0]
    gain_int = struct.unpack_from('<i', params, 12)[0]
    return {
        'input': _resolve_source_to_symbol(source, mx),
        'sourceId': str(source_id),
        'gain': _fmt_scalar(gain_int / _FAIRLIGHT_GAIN_DIVISOR),
    }

def _d_fairlight_strip_mix_type(params, mx):
    # u16 LE source, u16 marker, i64 LE sourceId, u16 LE state, u16 marker.
    source = _u16le(params, 0)
    source_id = struct.unpack_from('<Q', params, 4)[0]
    state = _u16le(params, 12)
    return {
        'input': _resolve_source_to_symbol(source, mx),
        'sourceId': str(source_id),
        'mixType': _MIX_TYPE_NAMES.get(state, str(state)),
    }

def _d_fairlight_master_fader_gain(params, mx):
    # i32 LE gain × 65536.
    gain_int = struct.unpack_from('<i', params, 0)[0]
    return {'gain': _fmt_scalar(gain_int / _FAIRLIGHT_GAIN_DIVISOR)}

# Fairlight EQ band shape — wire is a bitfield matching atemwire's
# FAIRLIGHT_EQ_FILTER_SHAPES table (LowShelf=1, LowPass=2, BandPass=4,
# Notch=8, HighPass=16, HighShelf=32). Live captures confirm BandPass=4,
# Notch=8, HighShelf=32.

_FAIRLIGHT_EQ_SHAPE_NAMES = {
    1: 'LowShelf', 2: 'LowPass', 4: 'BandPass',
    8: 'Notch', 16: 'HighPass', 32: 'HighShelf',
}

_FAIRLIGHT_EQ_SHAPE_TO_INT = {v: k for k, v in _FAIRLIGHT_EQ_SHAPE_NAMES.items()}

# Fairlight EQ band range — wire is a bitfield (Low=1, MidLow=2,
# MidHigh=4, High=8). Captures confirm MidLow=2, High=8. The macro
# bytecode and the live CEBP / AEBP wire packets all use this same
# bitfield (FAIRLIGHT_EQ_FREQ_RANGES in operations.py was previously
# 0-3 ordinal — that produced an off-by-one in the AEBP decoder
# where ATEM's L/ML/MH/H displayed as ML/MH/none/none).

_FAIRLIGHT_EQ_RANGE_NAMES = {
    1: 'Low', 2: 'MidLow', 4: 'MidHigh', 8: 'High',
}

_FAIRLIGHT_EQ_RANGE_TO_INT = {v: k for k, v in _FAIRLIGHT_EQ_RANGE_NAMES.items()}

# Op-specific marker at offset 14-15 of "enable"-style strip ops:

_FAIRLIGHT_ENABLE_TRAILING_MARKER = 0x000C

def _strip_prefix_decode(params):
    """Read 12-byte strip prefix → (source, source_id, family_marker)."""
    return (_u16le(params, 0),
            _u16le(params, 2),
            struct.unpack_from('<Q', params, 4)[0])

def _strip_prefix_encode(attrs, family_marker):
    src = _resolve_symbol_to_source(attrs['input'])
    sid = _resolve_source_id(attrs.get('sourceId'))
    return (struct.pack('<H', src & 0xFFFF)
            + struct.pack('<H', family_marker)
            + struct.pack('<Q', sid))

def _d_strip_value_fp(params, mx, attr_name, divisor=65536.0, signed=True):
    """16-byte: 12-byte prefix + i32 LE × divisor."""
    source, _marker, sid = _strip_prefix_decode(params)
    val_int = (struct.unpack_from('<i', params, 12)[0] if signed
               else _u32le(params, 12))
    return {
        'input': _resolve_source_to_symbol(source, mx),
        'sourceId': str(sid),
        attr_name: _fmt_scalar(val_int / divisor),
    }

def _e_strip_value_fp(attrs, family_marker, attr_name,
                      divisor=65536.0, signed=True):
    val_int = int(round(float(attrs[attr_name]) * divisor))
    payload = (_i32le_bytes(val_int) if signed
               else _u32le_bytes(val_int & 0xFFFFFFFF))
    return _strip_prefix_encode(attrs, family_marker) + payload

def _d_strip_enable(params, mx, family_marker_unused=None):
    """16-byte enable op: prefix + [u8 enabled, u8 marker, u16 LE marker]."""
    source, _marker, sid = _strip_prefix_decode(params)
    return {
        'input': _resolve_source_to_symbol(source, mx),
        'sourceId': str(sid),
        'enabled': 'True' if params[12] else 'False',
    }

def _e_strip_enable(attrs, family_marker, byte13=0xAC,
                     trailing_marker=_FAIRLIGHT_ENABLE_TRAILING_MARKER):
    val = 1 if str(attrs['enabled']).strip().lower() in ('true', '1', 'yes') else 0
    return (_strip_prefix_encode(attrs, family_marker)
            + bytes([val, byte13])
            + _u16le_bytes(trailing_marker))

# Equaliser gain (0x014d): same shape as fader gain, family marker 0x4346.


def _d_eq_gain(params, mx):
    return _d_strip_value_fp(params, mx, 'gain')

def _e_eq_gain(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_4346, 'gain')

# EQ band ops use family marker 0x03E1.
# 16-byte band-enable: prefix + [u8 band, u8 enabled, u16 marker=0x000c].
# 16-byte band-shape/range: prefix + [u8 band, u8 marker=0xAC, u16 enum_LE].
# 20-byte band-frequency/gain/qfactor: prefix + [u8 band, u8 0xAC,
#                                        u16 marker=0x000c, u32 LE value].


def _d_eq_band_enabled(params, mx):
    source, _m, sid = _strip_prefix_decode(params)
    return {
        'input': _resolve_source_to_symbol(source, mx),
        'sourceId': str(sid),
        'band': str(params[12]),
        'enabled': 'True' if params[13] else 'False',
    }

def _e_eq_band_enabled(attrs):
    band = int(attrs['band']) & 0xFF
    val = 1 if str(attrs['enabled']).strip().lower() in ('true', '1', 'yes') else 0
    return (_strip_prefix_encode(attrs, _FAIRLIGHT_FAM_03E1)
            + bytes([band, val])
            + _u16le_bytes(0x000C))

def _d_eq_band_shape(params, mx):
    source, _m, sid = _strip_prefix_decode(params)
    shape_int = _u16le(params, 14)
    return {
        'input': _resolve_source_to_symbol(source, mx),
        'sourceId': str(sid),
        'band': str(params[12]),
        'shape': _FAIRLIGHT_EQ_SHAPE_NAMES.get(shape_int, str(shape_int)),
    }

def _e_eq_band_shape(attrs):
    band = int(attrs['band']) & 0xFF
    shape = attrs['shape']
    shape_int = (_FAIRLIGHT_EQ_SHAPE_TO_INT.get(shape)
                 if isinstance(shape, str) else None)
    if shape_int is None:
        try:
            shape_int = int(shape)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown EQ shape {shape!r}") from exc
    return (_strip_prefix_encode(attrs, _FAIRLIGHT_FAM_03E1)
            + bytes([band, 0xAC])
            + _u16le_bytes(shape_int & 0xFFFF))

def _d_eq_band_range(params, mx):
    source, _m, sid = _strip_prefix_decode(params)
    range_int = _u16le(params, 14)
    return {
        'input': _resolve_source_to_symbol(source, mx),
        'sourceId': str(sid),
        'band': str(params[12]),
        'range': _FAIRLIGHT_EQ_RANGE_NAMES.get(range_int, str(range_int)),
    }

def _e_eq_band_range(attrs):
    band = int(attrs['band']) & 0xFF
    rng = attrs['range']
    rng_int = (_FAIRLIGHT_EQ_RANGE_TO_INT.get(rng)
               if isinstance(rng, str) else None)
    if rng_int is None:
        try:
            rng_int = int(rng)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown EQ range {rng!r}") from exc
    return (_strip_prefix_encode(attrs, _FAIRLIGHT_FAM_03E1)
            + bytes([band, 0xAC])
            + _u16le_bytes(rng_int & 0xFFFF))

def _d_eq_band_frequency(params, mx):
    source, _m, sid = _strip_prefix_decode(params)
    return {
        'input': _resolve_source_to_symbol(source, mx),
        'sourceId': str(sid),
        'band': str(params[12]),
        'frequency': str(_u32le(params, 16)),
    }

def _e_eq_band_frequency(attrs):
    band = int(attrs['band']) & 0xFF
    freq = int(attrs['frequency']) & 0xFFFFFFFF
    return (_strip_prefix_encode(attrs, _FAIRLIGHT_FAM_03E1)
            + bytes([band, 0xAC])
            + _u16le_bytes(0x000C)
            + _u32le_bytes(freq))

def _d_eq_band_gain(params, mx):
    source, _m, sid = _strip_prefix_decode(params)
    val_int = struct.unpack_from('<i', params, 16)[0]
    return {
        'input': _resolve_source_to_symbol(source, mx),
        'sourceId': str(sid),
        'band': str(params[12]),
        'gain': _fmt_scalar(val_int / 65536.0),
    }

def _e_eq_band_gain(attrs):
    band = int(attrs['band']) & 0xFF
    val_int = int(round(float(attrs['gain']) * 65536))
    return (_strip_prefix_encode(attrs, _FAIRLIGHT_FAM_03E1)
            + bytes([band, 0xAC])
            + _u16le_bytes(0x000C)
            + _i32le_bytes(val_int))

def _d_eq_band_qfactor(params, mx):
    source, _m, sid = _strip_prefix_decode(params)
    val_int = struct.unpack_from('<i', params, 16)[0]
    return {
        'input': _resolve_source_to_symbol(source, mx),
        'sourceId': str(sid),
        'band': str(params[12]),
        'qFactor': _fmt_scalar(val_int / 65536.0),
    }

def _e_eq_band_qfactor(attrs):
    band = int(attrs['band']) & 0xFF
    val_int = int(round(float(attrs['qFactor']) * 65536))
    return (_strip_prefix_encode(attrs, _FAIRLIGHT_FAM_03E1)
            + bytes([band, 0xAC])
            + _u16le_bytes(0x000C)
            + _i32le_bytes(val_int))

# --- Compressor (family marker 0x4346) ---


def _d_comp_enabled(params, mx):
    return _d_strip_enable(params, mx)

def _e_comp_enabled(attrs):
    return _e_strip_enable(attrs, _FAIRLIGHT_FAM_4346)

def _d_comp_threshold(params, mx):
    return _d_strip_value_fp(params, mx, 'threshold')

def _e_comp_threshold(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_4346, 'threshold')

def _d_comp_ratio(params, mx):
    return _d_strip_value_fp(params, mx, 'ratio')

def _e_comp_ratio(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_4346, 'ratio')

def _d_comp_attack(params, mx):
    return _d_strip_value_fp(params, mx, 'attack')

def _e_comp_attack(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_4346, 'attack')

def _d_comp_hold(params, mx):
    return _d_strip_value_fp(params, mx, 'hold')

def _e_comp_hold(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_4346, 'hold')

def _d_comp_release(params, mx):
    return _d_strip_value_fp(params, mx, 'release')

def _e_comp_release(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_4346, 'release')

# --- Limiter (family marker 0x03E1) ---


def _d_limiter_enabled(params, mx):
    return _d_strip_enable(params, mx)

def _e_limiter_enabled(attrs):
    return _e_strip_enable(attrs, _FAIRLIGHT_FAM_03E1)

def _d_limiter_threshold(params, mx):
    return _d_strip_value_fp(params, mx, 'threshold')

def _e_limiter_threshold(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_03E1, 'threshold')

def _d_limiter_attack(params, mx):
    return _d_strip_value_fp(params, mx, 'attack')

def _e_limiter_attack(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_03E1, 'attack')

def _d_limiter_hold(params, mx):
    return _d_strip_value_fp(params, mx, 'hold')

def _e_limiter_hold(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_03E1, 'hold')

def _d_limiter_release(params, mx):
    return _d_strip_value_fp(params, mx, 'release')

def _e_limiter_release(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_03E1, 'release')

# --- Expander (family marker 0x024A) ---


def _d_expander_enabled(params, mx):
    return _d_strip_enable(params, mx)

def _e_expander_enabled(attrs):
    # The byte13 marker for expander enable is 0x60 (per captures), not 0xAC.
    return _e_strip_enable(attrs, _FAIRLIGHT_FAM_024A, byte13=0x60,
                           trailing_marker=0x01B4)

def _d_expander_gate_mode_enabled(params, mx):
    return _d_strip_enable(params, mx)

def _e_expander_gate_mode_enabled(attrs):
    return _e_strip_enable(attrs, _FAIRLIGHT_FAM_024A, byte13=0x60,
                           trailing_marker=0x01B4)

def _d_expander_threshold(params, mx):
    return _d_strip_value_fp(params, mx, 'threshold')

def _e_expander_threshold(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_024A, 'threshold')

def _d_expander_range(params, mx):
    return _d_strip_value_fp(params, mx, 'range')

def _e_expander_range(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_024A, 'range')

def _d_expander_ratio(params, mx):
    return _d_strip_value_fp(params, mx, 'ratio')

def _e_expander_ratio(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_024A, 'ratio')

def _d_expander_attack(params, mx):
    return _d_strip_value_fp(params, mx, 'attack')

def _e_expander_attack(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_024A, 'attack')

def _d_expander_hold(params, mx):
    return _d_strip_value_fp(params, mx, 'hold')

def _e_expander_hold(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_024A, 'hold')

def _d_expander_release(params, mx):
    return _d_strip_value_fp(params, mx, 'release')

def _e_expander_release(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_024A, 'release')

# --- Fairlight: input gain, dynamics gain (family marker 0x4346) ---


def _d_input_gain(params, mx):
    return _d_strip_value_fp(params, mx, 'gain')

def _e_input_gain(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_4346, 'gain')

def _d_dynamics_gain(params, mx):
    return _d_strip_value_fp(params, mx, 'gain')

def _e_dynamics_gain(attrs):
    return _e_strip_value_fp(attrs, _FAIRLIGHT_FAM_4346, 'gain')

# --- Fairlight headphones (4 ops, master-style 4-byte i32 LE × 65536) ---
#
# Four op-codes share this codec (HeadphoneOutGain / MasterGain /
# TalkbackGain / SidetoneGain). The XML attribute name is ``gain`` on
# all four — the op-code disambiguates which bus the gain applies to,
# not an attribute-name suffix.


def _d_hp_gain(params, mx):
    val = struct.unpack_from('<i', params, 0)[0]
    return {'gain': _fmt_scalar(val / 65536.0)}

def _e_hp_gain(attrs):
    val = int(round(float(attrs['gain']) * 65536))
    return _i32le_bytes(val)

# --- Fairlight audio mixer ---


def _resolve_source_id(value) -> int:
    """Parse the XML ``sourceId`` attribute.

    Software Control writes the unsigned 64-bit form; atemwire stores
    it as i64 LE on the wire. Accept either.
    """
    if value is None or value == '':
        # Default to the universal stereo sentinel (-65280 as i64).
        return 0xFFFFFFFFFFFF0000
    try:
        return int(value) & 0xFFFFFFFFFFFFFFFF
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unparseable sourceId {value!r}") from exc

def _e_fairlight_strip_fader_gain(attrs):
    source = _resolve_symbol_to_source(attrs['input'])
    source_id = _resolve_source_id(attrs.get('sourceId'))
    gain_int = int(round(float(attrs['gain']) * _FAIRLIGHT_GAIN_DIVISOR))
    return (struct.pack('<H', source & 0xFFFF)
            + struct.pack('<H', _FAIRLIGHT_STRIP_MARKER)
            + struct.pack('<Q', source_id)
            + struct.pack('<i', gain_int))

def _e_fairlight_strip_mix_type(attrs):
    source = _resolve_symbol_to_source(attrs['input'])
    source_id = _resolve_source_id(attrs.get('sourceId'))
    mix_type = attrs['mixType']
    state = (_MIX_TYPE_TO_INT.get(mix_type, 2)
             if isinstance(mix_type, str) else int(mix_type))
    return (struct.pack('<H', source & 0xFFFF)
            + struct.pack('<H', _FAIRLIGHT_STRIP_MARKER)
            + struct.pack('<Q', source_id)
            + struct.pack('<H', state)
            + struct.pack('<H', _FAIRLIGHT_MIX_TYPE_TRAILING_MARKER))

def _e_fairlight_master_fader_gain(attrs):
    gain_int = int(round(float(attrs['gain']) * _FAIRLIGHT_GAIN_DIVISOR))
    return struct.pack('<i', gain_int)
