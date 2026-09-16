# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""Macro codec — media-player ops."""

from atemwire.macrotransfer._helpers import _u16le, _u16le_bytes


# --- Decoders ---

def _d_media_player_still_index(params, mx):
    return {'mediaPlayer': str(params[0]),
            'index': str(_u16le(params, 2))}


def _d_media_player_still_only(params, mx):
    return {'mediaPlayer': str(params[0])}


# --- Encoders ---

def _e_media_player_still_index(attrs):
    return (bytes([int(attrs['mediaPlayer']), 0])
            + _u16le_bytes(int(attrs['index'])))


def _e_media_player_still_only(attrs):
    return bytes([int(attrs['mediaPlayer']), 0, 0, 0])
