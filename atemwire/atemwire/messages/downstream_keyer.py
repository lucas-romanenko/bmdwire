# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Downstream keyer (DSK) messages — on-air toggle, auto transition, rate,
fill / key sources, gain (clip / gain / invert / pre-multiply), mask
(top / bottom / left / right enable + values).

Wire packets:
    CDsL — outgoing, on-air toggle
    DDsA — outgoing, auto transition
    CDsR — outgoing, transition rate (frames)
    CDsF — outgoing, fill source
    CDsC — outgoing, key (cut) source
    CDsG — outgoing, gain block (premultiplied / clip / gain / invert)
    CDsM — outgoing, mask block (enabled / top / bottom / left / right)
    DskB — incoming, base info (fill + key sources)
    DskP — incoming, full DSK config (rate, gain, mask)
    DskS — incoming, runtime state (on air, transitioning)
"""

import struct

from atemwire._state import (
    _kv, display_fps, percent_to_tenths,
    safe_bool, safe_float, safe_int,
)
from atemwire.helpers import parse_rate
from atemwire.messages._dsl import Recv, Send, boolean, i16, u8, u16


# -----------------------------------------------------------------------------
# Outgoing — on-air / auto / rate
# -----------------------------------------------------------------------------


class DkeyOnairCommand(Send):
    """``CDsL`` — set DSK on-air state.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     DSK index
    1      1    bool   On-air
    2      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'CDsL'
    SIZE = 4

    index  = u8     (at=0)
    on_air = boolean(at=1)

    def __init__(self, index, on_air):
        super().__init__(index=index, on_air=on_air)


class DkeyAutoCommand(Send):
    """``DDsA`` — trigger DSK auto transition.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     DSK index
    1      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'DDsA'
    SIZE = 4

    index = u8(at=0)

    def __init__(self, index):
        super().__init__(index=index)


class DkeyRateCommand(Send):
    """``CDsR`` — set DSK transition duration in frames.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     DSK index
    1      1    u8     Rate (frames)
    2      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'CDsR'
    SIZE = 4

    index = u8(at=0)
    rate  = u8(at=1)

    def __init__(self, index, rate):
        super().__init__(index=index, rate=rate)


class DkeyTieCommand(Send):
    """``CDsT`` — set DSK tie state.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     DSK index
    1      1    bool   Tie
    2      2    ?      padding
    ====== ==== ====== ===========

    Ported from upstream (``DkeyTieCommand``); the upstream
    ``struct.pack('>B?xx', index, tie)`` is the same 4-byte layout this
    DSL declaration emits.
    """
    CODE = 'CDsT'
    SIZE = 4

    index = u8     (at=0)
    tie   = boolean(at=1)

    def __init__(self, index, tie):
        super().__init__(index=index, tie=tie)


# -----------------------------------------------------------------------------
# Outgoing — fill / key sources
# -----------------------------------------------------------------------------


class DkeySetFillCommand(Send):
    """``CDsF`` — set DSK fill source.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     DSK index
    1      1    ?      padding
    2      2    u16    Fill source index
    ====== ==== ====== ===========
    """
    CODE = 'CDsF'
    SIZE = 4

    index  = u8 (at=0)
    source = u16(at=2)

    def __init__(self, index, source):
        super().__init__(index=index, source=source)


class DkeySetKeyCommand(Send):
    """``CDsC`` — set DSK key (cut) source.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     DSK index
    1      1    ?      padding
    2      2    u16    Key source index
    ====== ==== ====== ===========
    """
    CODE = 'CDsC'
    SIZE = 4

    index  = u8 (at=0)
    source = u16(at=2)

    def __init__(self, index, source):
        super().__init__(index=index, source=source)


# -----------------------------------------------------------------------------
# Outgoing — gain block (mask-gated optional fields)
# -----------------------------------------------------------------------------


class DkeyGainCommand(Send):
    """``CDsG`` — set DSK gain-block parameters (premultiplied / clip / gain
    / invert). Each parameter is independently gated by a mask bit; only
    fields explicitly passed are applied to the keyer.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask
    1      1    u8     DSK index
    2      1    bool   Premultiplied
    3      1    ?      padding
    4      2    u16    Clip
    6      2    u16    Gain
    8      1    bool   Invert
    9      3    ?      padding
    ====== ==== ====== ===========

    === ==========
    Bit Mask value
    === ==========
    0   Pre-multiplied
    1   Clip
    2   Gain
    3   Invert
    === ==========
    """
    CODE = 'CDsG'
    SIZE = 12
    MASK_AT = 0

    index         = u8     (at=1)
    premultiplied = boolean(at=2, mask_bit=0)
    clip          = u16    (at=4, mask_bit=1)
    gain          = u16    (at=6, mask_bit=2)
    invert        = boolean(at=8, mask_bit=3)

    def __init__(self, index, premultiplied=None, clip=None, gain=None, invert=None):
        super().__init__(index=index, premultiplied=premultiplied,
                         clip=clip, gain=gain, invert=invert)


# -----------------------------------------------------------------------------
# Outgoing — mask block (mask-gated optional fields)
# -----------------------------------------------------------------------------


class DkeyMaskCommand(Send):
    """``CDsM`` — set DSK mask parameters (enabled + top / bottom / left /
    right edges). Each parameter is independently gated by a mask bit.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask
    1      1    u8     DSK index
    2      1    bool   Mask enabled
    3      1    ?      padding
    4      2    i16    Top
    6      2    i16    Bottom
    8      2    i16    Left
    10     2    i16    Right
    ====== ==== ====== ===========

    === ==========
    Bit Mask value
    === ==========
    0   Enabled
    1   Top
    2   Bottom
    3   Left
    4   Right
    === ==========
    """
    CODE = 'CDsM'
    SIZE = 12
    MASK_AT = 0

    index   = u8     (at=1)
    enabled = boolean(at=2, mask_bit=0)
    top     = i16    (at=4, mask_bit=1)
    bottom  = i16    (at=6, mask_bit=2)
    left    = i16    (at=8, mask_bit=3)
    right   = i16    (at=10, mask_bit=4)

    def __init__(self, index, enabled=None, top=None, bottom=None,
                 left=None, right=None):
        super().__init__(index=index, enabled=enabled, top=top,
                         bottom=bottom, left=left, right=right)


# -----------------------------------------------------------------------------
# Incoming — base info / properties / state
# -----------------------------------------------------------------------------


class DkeyPropertiesBaseField(Recv):
    """``DskB`` — fill + key source assignments for one DSK.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     DSK index
    1      1    ?      padding
    2      2    u16    Fill source index
    4      2    u16    Key source index
    6      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'DskB'
    PRETTY = 'dkey-properties-base'
    KEY_FORMAT = struct.Struct('>B')

    index       = u8 (at=0)
    fill_source = u16(at=2)
    key_source  = u16(at=4)

    def __repr__(self):
        return (f'<downstream-keyer-base: dsk={self.index}, '
                f'fill={self.fill_source}, key={self.key_source}>')


class DkeyPropertiesField(Recv):
    """``DskP`` — full DSK config (rate, gain block, mask block).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     DSK index
    1      1    bool   Tie enabled
    2      1    u8     Transition rate (frames)
    3      1    bool   Mask is pre-multiplied alpha
    4      2    u16    Clip [0-1000]
    6      2    u16    Gain [0-1000]
    8      1    bool   Invert key
    9      1    bool   Enable mask
    10     2    i16    Top
    12     2    i16    Bottom
    14     2    i16    Left
    16     2    i16    Right
    18     2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'DskP'
    PRETTY = 'dkey-properties'
    KEY_FORMAT = struct.Struct('>B')

    index         = u8     (at=0)
    tie           = boolean(at=1)
    rate          = u8     (at=2)
    premultiplied = boolean(at=3)
    clip          = u16    (at=4)
    gain          = u16    (at=6)
    invert_key    = boolean(at=8)
    masked        = boolean(at=9)
    top           = i16    (at=10)
    bottom        = i16    (at=12)
    left          = i16    (at=14)
    right         = i16    (at=16)

    def __repr__(self):
        return (f'<downstream-keyer-mask: dsk={self.index}, tie={self.tie}, '
                f'rate={self.rate}, masked={self.masked}>')



class DkeyStateField(Recv):
    """``DskS`` — runtime state (on air, transitioning).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     DSK index
    1      1    bool   On air
    2      1    bool   Is transitioning
    3      1    bool   Is auto-transitioning
    4      1    u8     Frames remaining
    5      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'DskS'
    PRETTY = 'dkey-state'
    KEY_FORMAT = struct.Struct('>B')

    index                = u8     (at=0)
    on_air               = boolean(at=1)
    is_transitioning     = boolean(at=2)
    is_autotransitioning = boolean(at=3)
    frames_remaining     = u8     (at=4)

    def __repr__(self):
        return (f'<downstream-keyer-state: dsk={self.index}, '
                f'onair={self.on_air}, transitioning={self.is_transitioning} '
                f'autotrans={self.is_autotransitioning} '
                f'frames={self.frames_remaining}>')


# =============================================================================
# Operations
# =============================================================================


def _resolve_rate(conn, rate_str):
    return parse_rate(rate_str, display_fps(conn.mixerstate))


def set_dsk_on_air(conn, enabled, dsk_idx=0):
    """Absolute DSK on-air set. Use ``toggle_dsk`` for a flip."""
    conn.send(DkeyOnairCommand(index=int(dsk_idx), on_air=bool(enabled)))


def set_dsk_tie(conn, tie, dsk_idx=0):
    """Absolute DSK tie set (CDsT)."""
    conn.send(DkeyTieCommand(index=int(dsk_idx), tie=bool(tie)))


def toggle_dsk(conn, dsk_idx=0):
    """Flip DSK on-air state. Compound RMW: reads current on-air via
    ``dsk_state``, delegates to ``set_dsk_on_air`` with the inverted
    value."""
    current = dsk_state(conn.mixerstate, int(dsk_idx))['on_air']
    set_dsk_on_air(conn, enabled=not current, dsk_idx=dsk_idx)


def dsk_auto(conn, dsk_idx=0):
    conn.send(DkeyAutoCommand(index=int(dsk_idx)))


def set_dsk_rate(conn, rate_str, dsk_idx=0):
    conn.send(DkeyRateCommand(
        index=int(dsk_idx), rate=_resolve_rate(conn, rate_str)))


def set_dsk_fill_source(conn, source, dsk_idx=0):
    conn.send(DkeySetFillCommand(index=int(dsk_idx), source=int(source)))


def set_dsk_key_source(conn, source, dsk_idx=0):
    conn.send(DkeySetKeyCommand(index=int(dsk_idx), source=int(source)))


def set_dsk_mask_enabled(conn, enabled, dsk_idx=0):
    conn.send(DkeyMaskCommand(index=int(dsk_idx), enabled=bool(enabled)))


def set_dsk_mask_top(conn, top, dsk_idx=0):
    conn.send(DkeyMaskCommand(
        index=int(dsk_idx), top=int(round(float(top) * 1000))))


def set_dsk_mask_bottom(conn, bottom, dsk_idx=0):
    conn.send(DkeyMaskCommand(
        index=int(dsk_idx), bottom=int(round(float(bottom) * 1000))))


def set_dsk_mask_left(conn, left, dsk_idx=0):
    conn.send(DkeyMaskCommand(
        index=int(dsk_idx), left=int(round(float(left) * 1000))))


def set_dsk_mask_right(conn, right, dsk_idx=0):
    conn.send(DkeyMaskCommand(
        index=int(dsk_idx), right=int(round(float(right) * 1000))))


def set_dsk_pre_multiplied(conn, pre_multiplied, dsk_idx=0):
    conn.send(DkeyGainCommand(
        index=int(dsk_idx), premultiplied=bool(pre_multiplied)))


def set_dsk_clip(conn, clip, dsk_idx=0):
    """DSK clip/gain wire is u16 0..1000 (×10 of percent), NOT the ×100
    scale percent_to_thousandths produces. Earlier use of that helper
    sent 10× over and ATEM clamped at max (100%)."""
    conn.send(DkeyGainCommand(
        index=int(dsk_idx), clip=percent_to_tenths(clip)))


def set_dsk_gain(conn, gain, dsk_idx=0):
    conn.send(DkeyGainCommand(
        index=int(dsk_idx), gain=percent_to_tenths(gain)))


def set_dsk_invert_key(conn, invert_key, dsk_idx=0):
    conn.send(DkeyGainCommand(
        index=int(dsk_idx), invert=bool(invert_key)))


def configure_dsk_gain(conn, *, clip=None, gain=None, pre_multiplied=None,
                      invert=None, dsk_idx=0):
    """Set multiple DSK gain fields atomically in one CDsG packet. Each
    kwarg is optional; only non-None kwargs land in the field mask.

    Scale fix (2026-06-02): clip/gain now use ``percent_to_tenths``
    (×10, clamp 0..1000) — consistent with the single setters
    ``set_dsk_clip`` / ``set_dsk_gain``. Previously this used
    ``percent_to_thousandths`` (×100), which the ATEM clamped to 100%
    for any value ≳10% (e.g. ``configure_dsk_gain(clip=22, gain=30)``)."""
    conn.send(DkeyGainCommand(
        index=int(dsk_idx),
        premultiplied=None if pre_multiplied is None else bool(pre_multiplied),
        clip=None if clip is None else percent_to_tenths(clip),
        gain=None if gain is None else percent_to_tenths(gain),
        invert=None if invert is None else bool(invert),
    ))


# =============================================================================
# Readers
# =============================================================================


def dsk_state(mx, dsk_idx=0):
    """Assemble the full DSK state into the dict shape build_full_state
    emits. Reads spread across dkey-state, dkey-properties, and
    dkey-properties-base. Bucket B (multi-key aggregator)."""
    state = _kv(mx, 'dkey-state', dsk_idx)
    props = _kv(mx, 'dkey-properties', dsk_idx)
    base = _kv(mx, 'dkey-properties-base', dsk_idx)

    on_air = safe_bool(
        getattr(state, 'on_air', False), False) if state else False
    in_transition = safe_bool(
        getattr(state, 'is_transitioning', False), False) if state else False
    is_auto = safe_bool(
        getattr(state, 'is_autotransitioning', False), False) if state else False
    frames = safe_int(getattr(state, 'frames_remaining', 0), 0) if state else 0

    fill = safe_int(getattr(base, 'fill_source', 0), 0) if base else 0
    key = safe_int(getattr(base, 'key_source', 0), 0) if base else 0

    rate = safe_int(getattr(props, 'rate', 25), 25) if props else 25
    tie = safe_bool(getattr(props, 'tie', False), False) if props else False
    pre_mult = safe_bool(
        getattr(props, 'premultiplied', False), False) if props else False
    # DSK clip/gain wire is u16 0..1000 (×10 of percent) per DskP field
    # decoder. NOT the ×100 scale percent_from_thousandths assumes —
    # earlier use of that helper read 10× too low (wire 660 → 6.6
    # instead of 66.0). Write side above uses percent_to_tenths to match.
    clip = safe_float(
        (getattr(props, 'clip', 0) if props else 0) / 10.0, 0.0, 1)
    gain = safe_float(
        (getattr(props, 'gain', 0) if props else 0) / 10.0, 0.0, 1)
    invert = safe_bool(
        getattr(props, 'invert_key', False), False) if props else False
    mask_enabled = safe_bool(
        getattr(props, 'masked', False), False) if props else False
    # DSK mask wire is fixed-point ×1000 (e.g. 9000 = 9.000), same scale
    # as USK mask + DVE mask. Write above uses int(round(value * 1000)).
    mask_top = safe_float(
        (getattr(props, 'top', 0) if props else 0) / 1000.0, 0.0, 3)
    mask_bottom = safe_float(
        (getattr(props, 'bottom', 0) if props else 0) / 1000.0, 0.0, 3)
    mask_left = safe_float(
        (getattr(props, 'left', 0) if props else 0) / 1000.0, 0.0, 3)
    mask_right = safe_float(
        (getattr(props, 'right', 0) if props else 0) / 1000.0, 0.0, 3)

    return {
        'on_air': on_air,
        'tie': tie,
        'rate': rate,
        'in_transition': in_transition or is_auto,
        'is_auto_transitioning': is_auto,
        'frames_remaining': frames,
        'fill_source': fill,
        'key_source': key,
        'mask_enabled': mask_enabled,
        'mask_top': mask_top,
        'mask_bottom': mask_bottom,
        'mask_left': mask_left,
        'mask_right': mask_right,
        'pre_multiplied': pre_mult,
        'clip': clip,
        'gain': gain,
        'invert_key': invert,
    }
