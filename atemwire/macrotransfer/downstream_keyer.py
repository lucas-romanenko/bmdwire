# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""Macro codec — DSK ops (fill/cut input, rate, auto, on-air,
clip, gain, mask enable, pre-multiply).
"""

from atemwire.macrotransfer._helpers import (
    _u16le, _u16le_bytes, _resolve_source_to_symbol,
    _resolve_symbol_to_source, _bool_str, _bool_byte, _fmt_scalar,
    _pack_fixed_value, _d_fixed_value, _DVE_MARKER,
)

# --- Downstream keyer ---


def _d_dsk_fill_input(params, mx):
    return {'keyIndex': str(params[0]),
            'input': _resolve_source_to_symbol(_u16le(params, 2), mx)}

def _d_dsk_cut_input(params, mx):
    return {'keyIndex': str(params[0]),
            'input': _resolve_source_to_symbol(_u16le(params, 2), mx)}

def _d_dsk_rate(params, mx):
    return {'keyIndex': str(params[0]),
            'rate': str(_u16le(params, 2))}

def _d_dsk_auto(params, mx):
    return {'keyIndex': str(params[0])}

def _d_dsk_on_air(params, mx):
    return {'keyIndex': str(params[0]),
            'onAir': _bool_str(params[1])}

def _d_dsk_tie(params, mx):
    # u8 dsk, u8 tie, u16 _. Same shape as on_air; op 0x9B sits between
    # on_air (0x9A) and clip (0x9C). Confirmed live 2026-06-09: tie=True
    # records params 00 01 00 00.
    return {'keyIndex': str(params[0]),
            'tie': _bool_str(params[1])}

def _d_dsk_clip(params, mx):
    return {'keyIndex': str(params[0]),
            'clip': _fmt_scalar(_d_fixed_value(params, 65536.0))}

def _d_dsk_gain(params, mx):
    # Same fractional shape as clip. Wire per-mille (0..1000) → XML 0..1
    # fraction; macro u32 = fraction × 65536 (Q16.16). (Was 655360, a 10×
    # error that set 100% on the ATEM for a 30% macro.)
    return {'keyIndex': str(params[0]),
            'gain': _fmt_scalar(_d_fixed_value(params, 65536.0))}

def _d_dsk_mask_enable(params, mx):
    # u8 dsk, u8 enable, u16 _.
    return {'keyIndex': str(params[0]),
            'enable': _bool_str(params[1])}

def _d_dsk_pre_multiply(params, mx):
    # u8 dsk, u8 preMultiply, u16 _.
    return {'keyIndex': str(params[0]),
            'preMultiply': _bool_str(params[1])}

# --- Downstream keyer ---


def _e_dsk_fill_input(attrs):
    return (bytes([int(attrs['keyIndex']), 0])
            + _u16le_bytes(_resolve_symbol_to_source(attrs['input'])))

def _e_dsk_cut_input(attrs):
    return (bytes([int(attrs['keyIndex']), 0])
            + _u16le_bytes(_resolve_symbol_to_source(attrs['input'])))

def _e_dsk_rate(attrs):
    return bytes([int(attrs['keyIndex']), 0]) + _u16le_bytes(int(attrs['rate']))

def _e_dsk_auto(attrs):
    return bytes([int(attrs['keyIndex']), 0, 0, 0])

def _e_dsk_on_air(attrs):
    on = 1 if str(attrs['onAir']).lower() in ('true', '1') else 0
    return bytes([int(attrs['keyIndex']), on, 0, 0])

def _e_dsk_tie(attrs):
    tie = 1 if str(attrs['tie']).lower() in ('true', '1') else 0
    return bytes([int(attrs['keyIndex']), tie, 0, 0])

_DSK_CLIP_GAIN_DIVISOR = 65536.0

# u16 at params offset 2 is "unknown" to the decoder. Real Software
# Control recordings put 0 there, regardless of op flavour (luma vs
# DSK clip/gain).

_DSK_MARKER = 0x0000

def _e_dsk_clip(attrs):
    return (bytes([int(attrs['keyIndex']), 0])
            + _u16le_bytes(_DSK_MARKER)
            + _pack_fixed_value(attrs['clip'], _DSK_CLIP_GAIN_DIVISOR))

def _e_dsk_gain(attrs):
    return (bytes([int(attrs['keyIndex']), 0])
            + _u16le_bytes(_DSK_MARKER)
            + _pack_fixed_value(attrs['gain'], _DSK_CLIP_GAIN_DIVISOR))

def _e_dsk_mask_enable(attrs):
    # u8 dsk, u8 enable, u16 marker (0).
    return (bytes([int(attrs['keyIndex'])])
            + _bool_byte(attrs['enable'])
            + _u16le_bytes(_DVE_MARKER))

def _e_dsk_pre_multiply(attrs):
    # u8 dsk, u8 preMultiply, u16 marker (0).
    return (bytes([int(attrs['keyIndex'])])
            + _bool_byte(attrs['preMultiply'])
            + _u16le_bytes(_DSK_MARKER))
