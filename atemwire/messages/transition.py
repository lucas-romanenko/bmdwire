# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Transition messages — settings (style + next-transition selection),
per-style configuration (mix / dip / wipe / DVE / stinger), and live
state (preview, position, per-style state).

Wire packets:
    CTMx — outgoing, mix transition rate
    CTDp — outgoing, dip transition rate + source
    CTWp — outgoing, wipe transition (10 fields)
    CTSt — outgoing, stinger transition (9 fields)
    CTDv — outgoing, DVE transition (11 fields)
    TrSS — incoming, transition settings (style + next-transition mask)
    TsPr — incoming, preview-transition flag
    TrPs — incoming, T-handle position + remaining frames
    TMxP — incoming, mix transition state
    TDpP — incoming, dip transition state
    TWpP — incoming, wipe transition state
    TDvP — incoming, DVE transition state
    TStP — incoming, stinger transition state
"""

import struct

from atemwire._state import (
    _kv, display_fps, percent_from_thousandths,
    percent_to_tenths, percent_to_thousandths, safe_bool, safe_float,
    safe_int, unit_from_thousandths, unit_to_thousandths,
    value_from_tenths,
)
from atemwire.helpers import parse_rate, transition_mask_from_selection
from atemwire.messages._dsl import Recv, Send, boolean, u8, u16
from atemwire.messages._dsl import Send, boolean, i16, string, u8, u16, u32  # noqa: F401  (restored upstream commands)


# -----------------------------------------------------------------------------
# Outgoing — overall transition settings (style + next-transition mask)
# -----------------------------------------------------------------------------


class TransitionSettingsCommand(Send):
    """``CTTp`` — set the transition style for a M/E and / or the next-
    transition layer mask.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (bit 0 style, bit 1 next_transition)
    1      1    u8     M/E index
    2      1    u8     Style
    3      1    u8     Next transition (bitfield: BKGD + Key1..Key4)
    ====== ==== ====== ===========
    """
    CODE = 'CTTp'
    SIZE = 4
    MASK_AT = 0

    index           = u8(at=1)
    style           = u8(at=2, mask_bit=0)
    next_transition = u8(at=3, mask_bit=1)

    def __init__(self, index, style=None, next_transition=None):
        super().__init__(index=index, style=style, next_transition=next_transition)


# -----------------------------------------------------------------------------
# Outgoing — per-style settings
# -----------------------------------------------------------------------------


class MixSettingsCommand(Send):
    """``CTMx`` — set mix-transition duration on one M/E.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Rate (frames)
    2      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'CTMx'
    SIZE = 4

    index = u8(at=0)
    rate  = u8(at=1)

    def __init__(self, index, rate):
        super().__init__(index=index, rate=rate)


class DipSettingsCommand(Send):
    """``CTDp`` — set dip-transition rate + source.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (bit 0 rate, bit 1 source)
    1      1    u8     M/E index
    2      1    u8     Rate (frames)
    3      1    ?      padding
    4      2    u16    Source index
    6      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'CTDp'
    SIZE = 8
    MASK_AT = 0

    index  = u8 (at=1)
    rate   = u8 (at=2, mask_bit=0)
    source = u16(at=4, mask_bit=1)

    def __init__(self, index, rate=None, source=None):
        super().__init__(index=index, rate=rate, source=source)


class WipeSettingsCommand(Send):
    """``CTWp`` — set wipe-transition parameters.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Mask
    2      1    u8     M/E index
    3      1    u8     Rate (frames)
    4      1    u8     Pattern style
    5      1    ?      padding
    6      2    u16    Border width [0-10000]
    8      2    u16    Border fill source
    10     2    u16    Symmetry [0-10000]
    12     2    u16    Softness [0-10000]
    14     2    u16    Origin X [0-10000]
    16     2    u16    Origin Y [0-10000]
    18     1    bool   Reverse
    19     1    bool   Flip flop
    ====== ==== ====== ===========

    Mask bits: 0 rate, 1 pattern, 2 width, 3 source, 4 symmetry,
               5 softness, 6 x, 7 y, 8 reverse, 9 flipflop
    """
    CODE = 'CTWp'
    SIZE = 20
    MASK_AT = 0
    MASK_TYPE = u16  # bits 8 (reverse), 9 (flipflop) live in the high byte

    index     = u8     (at=2)
    rate      = u8     (at=3, mask_bit=0)
    pattern   = u8     (at=4, mask_bit=1)
    width     = u16    (at=6, mask_bit=2)
    source    = u16    (at=8, mask_bit=3)
    symmetry  = u16    (at=10, mask_bit=4)
    softness  = u16    (at=12, mask_bit=5)
    positionx = u16    (at=14, mask_bit=6)
    positiony = u16    (at=16, mask_bit=7)
    reverse   = boolean(at=18, mask_bit=8)
    flipflop  = boolean(at=19, mask_bit=9)

    def __init__(self, index, rate=None, pattern=None, width=None, source=None,
                 symmetry=None, softness=None, positionx=None, positiony=None,
                 reverse=None, flipflop=None):
        super().__init__(index=index, rate=rate, pattern=pattern, width=width,
                         source=source, symmetry=symmetry, softness=softness,
                         positionx=positionx, positiony=positiony,
                         reverse=reverse, flipflop=flipflop)


class StingerSettingsCommand(Send):
    """``CTSt`` — set stinger-transition parameters.

    Total payload 20 bytes per the PyATEMMax reference. Earlier code shipped
    with a 22-byte trailer that the ATEM silently rejected — making every
    stinger write a no-op. Fixed in atemwire.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Mask
    2      1    u8     M/E index
    3      1    u8     Stinger mediaplayer index (1..4)
    4      1    bool   Key premultiplied
    5      1    ?      padding
    6      2    u16    Key clip [0-1000]
    8      2    u16    Key gain [0-1000]
    10     1    bool   Key invert
    11     1    ?      padding
    12     2    u16    Preroll frames
    14     2    u16    Clip duration frames
    16     2    u16    Trigger point frame
    18     2    u16    Mix rate
    ====== ==== ====== ===========

    Mask bits: 0 mediaplayer, 1 key_premultiplied, 2 clip, 3 gain,
               4 invert, 5 preroll, 6 duration, 7 triggerpoint, 8 rate
    """
    CODE = 'CTSt'
    SIZE = 20
    MASK_AT = 0
    MASK_TYPE = u16  # bit 8 (rate) lives in the high byte

    index             = u8     (at=2)
    mediaplayer       = u8     (at=3, mask_bit=0)
    key_premultiplied = boolean(at=4, mask_bit=1)
    key_clip          = u16    (at=6, mask_bit=2)
    key_gain          = u16    (at=8, mask_bit=3)
    key_invert        = boolean(at=10, mask_bit=4)
    preroll           = u16    (at=12, mask_bit=5)
    duration          = u16    (at=14, mask_bit=6)
    triggerpoint      = u16    (at=16, mask_bit=7)
    rate              = u16    (at=18, mask_bit=8)

    def __init__(self, index, mediaplayer=None, key_premultiplied=None,
                 key_clip=None, key_gain=None, key_invert=None,
                 preroll=None, duration=None, triggerpoint=None, rate=None):
        super().__init__(
            index=index, mediaplayer=mediaplayer,
            key_premultiplied=key_premultiplied, key_clip=key_clip,
            key_gain=key_gain, key_invert=key_invert,
            preroll=preroll, duration=duration, triggerpoint=triggerpoint,
            rate=rate,
        )


class DveSettingsCommand(Send):
    """``CTDv`` — set DVE-transition parameters.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Mask
    2      1    u8     M/E index
    3      1    u8     Logo-wipe rate (frames)
    4      1    u8     Rate (frames)
    5      1    u8     DVE style
    6      2    u16    Fill source
    8      2    u16    Key source
    10     1    bool   Enable key
    11     1    bool   Key premultiplied
    12     2    u16    Key clip [0-1000]
    14     2    u16    Key gain [0-1000]
    16     1    bool   Key invert
    17     1    bool   Reverse
    18     1    bool   Flip flop
    19     1    ?      padding
    ====== ==== ====== ===========

    Mask bits: 0 logo_rate, 1 rate, 2 style, 3 fill_source, 4 key_source,
               5 key_enable, 6 key_premultiplied, 7 key_clip, 8 key_gain,
               9 key_invert, 10 reverse, 11 flipflop.

    WIRE-FORMAT CORRECTION (2026-08-22, verified against ASC on the 1 M/E
    Constellation HD): upstream (and this fork until today) put
    ``rate`` at byte 3 / mask bit 0 and called byte 4 padding and bit 1
    unused. Byte 3 / bit 0 is actually the DVE LOGO-WIPE rate — a real,
    ASC-invisible-by-default register that echoes into TDvP byte 1 — so
    every rate write round-tripped perfectly through our own reader while
    ASC (which drives byte 4 / bit 1, echoing TDvP byte 2) never saw it,
    and vice versa. Found live: "DVE rate dead both ways while wipe works".
    Do not "fix" this back to match upstream.
    """
    CODE = 'CTDv'
    SIZE = 20
    MASK_AT = 0
    MASK_TYPE = u16  # bits 8-11 (key_gain, key_invert, reverse, flipflop) in high byte

    index             = u8     (at=2)
    logo_rate         = u8     (at=3, mask_bit=0)
    rate              = u8     (at=4, mask_bit=1)
    style             = u8     (at=5, mask_bit=2)
    fill_source       = u16    (at=6, mask_bit=3)
    key_source        = u16    (at=8, mask_bit=4)
    key_enable        = boolean(at=10, mask_bit=5)
    key_premultiplied = boolean(at=11, mask_bit=6)
    key_clip          = u16    (at=12, mask_bit=7)
    key_gain          = u16    (at=14, mask_bit=8)
    key_invert        = boolean(at=16, mask_bit=9)
    reverse           = boolean(at=17, mask_bit=10)
    flipflop          = boolean(at=18, mask_bit=11)

    def __init__(self, index, rate=None, logo_rate=None, style=None,
                 fill_source=None, key_source=None, key_enable=None,
                 key_premultiplied=None, key_clip=None, key_gain=None,
                 key_invert=None, reverse=None, flipflop=None):
        super().__init__(
            index=index, rate=rate, logo_rate=logo_rate, style=style,
            fill_source=fill_source, key_source=key_source,
            key_enable=key_enable, key_premultiplied=key_premultiplied,
            key_clip=key_clip, key_gain=key_gain, key_invert=key_invert,
            reverse=reverse, flipflop=flipflop,
        )


# -----------------------------------------------------------------------------
# Incoming — settings / preview / position
# -----------------------------------------------------------------------------


class TransitionSettingsField(Recv):
    """``TrSS`` — current transition settings + next-transition layer mask.

    The next-transition byte is a bitfield (background + 4 USK layers);
    the field exposes those as 5 booleans + 5 "_next" booleans for the
    pending settings that will land after the active transition completes.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Active transition style
    2      1    u8     Active next-transition layers (bitfield)
    3      1    u8     Pending transition style
    4      1    u8     Pending next-transition layers (bitfield)
    5      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'TrSS'
    PRETTY = 'transition-settings'
    # Stage 4A (control-topology refactor): keyed per M/E. The wire format
    # always carried the M/E index at byte 0; without a KEY_FORMAT the
    # packet was stored bare and the last-arriving M/E overwrote the
    # others — invisible on 1-M/E units, wrong on Constellations.
    KEY_FORMAT = struct.Struct('>B')

    STYLE_MIX   = 0
    STYLE_DIP   = 1
    STYLE_WIPE  = 2
    STYLE_DVE   = 3
    STYLE_STING = 4

    def __init__(self, raw: bytes):
        self.raw = raw
        (self.index, self.style, nt,
         self.style_next, ntn) = struct.unpack('>B 2B 2B 3x', raw)

        self.next_transition_bkgd = nt & (1 << 0) != 0
        self.next_transition_key1 = nt & (1 << 1) != 0
        self.next_transition_key2 = nt & (1 << 2) != 0
        self.next_transition_key3 = nt & (1 << 3) != 0
        self.next_transition_key4 = nt & (1 << 4) != 0

        self.next_transition_bkgd_next = ntn & (1 << 0) != 0
        self.next_transition_key1_next = ntn & (1 << 1) != 0
        self.next_transition_key2_next = ntn & (1 << 2) != 0
        self.next_transition_key3_next = ntn & (1 << 3) != 0
        self.next_transition_key4_next = ntn & (1 << 4) != 0

    def __repr__(self):
        return f'<transition-settings: me={self.index} style={self.style}>'


class TransitionPreviewField(Recv):
    """``TrPr`` — PREV TRANS button state for one M/E.

    The upstream field class had ``CODE = 'TsPr'`` but the actual wire
    code emitted by the ATEM (per the dispatch table that's been working
    for years) is ``TrPr``. Upstream's name-based dispatch never noticed
    the CODE-attribute typo; our CODE-based registry does.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    bool   Enabled
    2      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'TrPr'
    PRETTY = 'transition-preview'
    KEY_FORMAT = struct.Struct('>B')

    index   = u8     (at=0)
    enabled = boolean(at=1)

    def __repr__(self):
        return f'<transition-preview: me={self.index} enabled={self.enabled}>'


class TransitionPositionField(Recv):
    """``TrPs`` — T-handle position + remaining frames.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    bool   In transition
    2      1    u8     Frames remaining
    3      1    ?      padding
    4      2    u16    Position [0-9999]
    6      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'TrPs'
    PRETTY = 'transition-position'
    KEY_FORMAT = struct.Struct('>B')

    index            = u8     (at=0)
    in_transition    = boolean(at=1)
    frames_remaining = u8     (at=2)
    position         = u16    (at=4)

    def __repr__(self):
        return (f'<transition-position: me={self.index} '
                f'frames-remaining={self.frames_remaining} '
                f'position={self.position:02f}>')


# -----------------------------------------------------------------------------
# Incoming — per-style state (mix / dip / wipe / DVE / stinger)
# -----------------------------------------------------------------------------


class TransitionMixField(Recv):
    """``TMxP`` — mix-transition state (just the rate).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Rate (frames)
    2      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'TMxP'
    PRETTY = 'transition-mix'
    KEY_FORMAT = struct.Struct('>B')

    index = u8(at=0)
    rate  = u8(at=1)

    def __repr__(self):
        return f'<transition-mix: me={self.index}, rate={self.rate}>'


class TransitionDipField(Recv):
    """``TDpP`` — dip-transition state.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Rate (frames)
    2      2    u16    Dip source index
    ====== ==== ====== ===========
    """
    CODE = 'TDpP'
    PRETTY = 'transition-dip'
    KEY_FORMAT = struct.Struct('>B')

    index  = u8 (at=0)
    rate   = u8 (at=1)
    source = u16(at=2)

    def __repr__(self):
        return (f'<transition-dip: me={self.index}, rate={self.rate} '
                f'source={self.source}>')


class TransitionWipeField(Recv):
    """``TWpP`` — wipe-transition state.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Rate (frames)
    2      1    u8     Pattern id
    3      1    ?      padding
    4      2    u16    Border width
    6      2    u16    Border fill source
    8      2    u16    Symmetry
    10     2    u16    Softness
    12     2    u16    Origin X
    14     2    u16    Origin Y
    16     1    bool   Reverse
    17     1    bool   Flip flop
    18     2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'TWpP'
    PRETTY = 'transition-wipe'
    KEY_FORMAT = struct.Struct('>B')

    index     = u8     (at=0)
    rate      = u8     (at=1)
    pattern   = u8     (at=2)
    width     = u16    (at=4)
    source    = u16    (at=6)
    symmetry  = u16    (at=8)
    softness  = u16    (at=10)
    positionx = u16    (at=12)
    positiony = u16    (at=14)
    reverse   = boolean(at=16)
    flipflop  = boolean(at=17)

    def __repr__(self):
        return (f'<transition-wipe: me={self.index}, rate={self.rate} '
                f'pattern={self.pattern}>')


class TransitionDveField(Recv):
    """``TDvP`` — DVE-transition state.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Logo-wipe rate (frames)
    2      1    u8     Rate (frames)
    3      1    u8     DVE style
    4      2    u16    Fill source
    6      2    u16    Key source
    8      1    bool   Enable key
    9      1    bool   Key premultiplied
    10     2    u16    Key clip [0-1000]
    12     2    u16    Key gain [0-1000]
    14     1    bool   Key invert
    15     1    bool   Reverse
    16     1    bool   Flip flop
    17     3    ?      padding
    ====== ==== ====== ===========

    WIRE-FORMAT CORRECTION (2026-08-22): byte 1 is the LOGO-WIPE rate,
    byte 2 (upstream calls it unknown) is THE transition rate — ASC's
    DVE rate box moves byte 2, captured live on the 1 M/E Constellation
    HD. See the matching note on DveSettingsCommand; do not revert to
    the upstream layout.
    """
    CODE = 'TDvP'
    PRETTY = 'transition-dve'
    KEY_FORMAT = struct.Struct('>B')

    index             = u8     (at=0)
    logo_rate         = u8     (at=1)
    rate              = u8     (at=2)
    style             = u8     (at=3)
    fill_source       = u16    (at=4)
    key_source        = u16    (at=6)
    key_enable        = boolean(at=8)
    key_premultiplied = boolean(at=9)
    key_clip          = u16    (at=10)
    key_gain          = u16    (at=12)
    key_invert        = boolean(at=14)
    reverse           = boolean(at=15)
    flipflop          = boolean(at=16)

    def __repr__(self):
        return (f'<transition-dve: me={self.index}, rate={self.rate} '
                f'style={self.style}>')


class TransitionStingerField(Recv):
    """``TStP`` — stinger-transition state.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Stinger mediaplayer index
    2      1    bool   Key premultiplied
    3      1    ?      padding
    4      2    u16    Key clip [0-1000]
    6      2    u16    Key gain [0-1000]
    8      1    bool   Key invert
    9      1    ?      padding
    10     2    u16    Preroll frames
    12     2    u16    Clip duration frames
    14     2    u16    Trigger point frame
    16     2    u16    Mix rate
    18     2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'TStP'
    PRETTY = 'transition-stinger'
    KEY_FORMAT = struct.Struct('>B')

    index             = u8     (at=0)
    mediaplayer       = u8     (at=1)
    key_premultiplied = boolean(at=2)
    key_clip          = u16    (at=4)
    key_gain          = u16    (at=6)
    key_invert        = boolean(at=8)
    preroll           = u16    (at=10)
    duration          = u16    (at=12)
    triggerpoint      = u16    (at=14)
    rate              = u16    (at=16)

    def __repr__(self):
        return (f'<transition-stinger: me={self.index}, '
                f'mediaplayer={self.mediaplayer}>')


# =============================================================================
# Operations + readers — basic + mix/dip + selection (sub-commit 3a)
# =============================================================================
#
# Operations and readers below correspond to the per-style sub-groupings of
# the transition feature. The 3a sub-commit covers: overall transition
# settings (style, next-transition selection RMW + absolute setter), mix
# rate, dip rate + source. Per-style configuration / state for wipe, DVE
# transition, and stinger lands in subsequent sub-commits (3b–3d).

# Helper: parse a 'seconds:frames' rate string against the ATEM's current
# display fps. Reads ``video-mode`` from mixerstate via ``display_fps``;
# both helpers live in atemwire._state.


def _resolve_rate(conn, rate_str):
    return parse_rate(rate_str, display_fps(conn.mixerstate))


# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def set_transition_style(conn, style, me=0):
    conn.send(TransitionSettingsCommand(index=me, style=int(style)))


def set_mix_rate(conn, rate_str, me=0):
    conn.send(MixSettingsCommand(index=me, rate=_resolve_rate(conn, rate_str)))


def set_dip_rate(conn, rate_str, me=0):
    conn.send(DipSettingsCommand(index=me, rate=_resolve_rate(conn, rate_str)))


def set_dip_source(conn, source, me=0):
    conn.send(DipSettingsCommand(index=me, source=int(source)))


def set_transition_rate(conn, rate_str, me=0):
    """Apply ``rate_str`` to whichever transition style is currently active.

    Compound read-modify-write: reads ``transition-settings.style`` from
    mixerstate, branches to the corresponding *SettingsCommand class. If
    the style field isn't in mixerstate yet, defaults to Mix (0)."""
    rate_frames = _resolve_rate(conn, rate_str)
    style = transition_style(conn.mixerstate, me)
    if style == 0:
        conn.send(MixSettingsCommand(index=me, rate=rate_frames))
    elif style == 1:
        conn.send(DipSettingsCommand(index=me, rate=rate_frames))
    elif style == 2:
        conn.send(WipeSettingsCommand(index=me, rate=rate_frames))
    elif style == 3:
        conn.send(DveSettingsCommand(index=me, rate=rate_frames))
    elif style == 4:
        conn.send(StingerSettingsCommand(index=me, rate=rate_frames))
    else:
        raise ValueError(f"unknown transition style {style}")


def toggle_transition_key(conn, key, me=0):
    """Toggle one of the 4 keyer bits in the next-transition selection.

    Compound RMW: reads current selection from mixerstate, flips the
    requested bit, packs, sends. ATEM rejects a zero mask (at-least-one-
    layer rule); we no-op rather than sending a rejected packet."""
    sel = transition_selection(conn.mixerstate, me)
    sel[f'key{int(key) + 1}'] = not sel.get(f'key{int(key) + 1}', False)
    mask = transition_mask_from_selection(sel)
    if mask == 0:
        return
    conn.send(TransitionSettingsCommand(index=me, next_transition=mask))


def toggle_transition_background(conn, me=0):
    """Toggle the background bit in the next-transition selection."""
    sel = transition_selection(conn.mixerstate, me)
    sel['background'] = not sel.get('background', True)
    mask = transition_mask_from_selection(sel)
    if mask == 0:
        return
    conn.send(TransitionSettingsCommand(index=me, next_transition=mask))


def set_next_transition_layers(conn, *, background=False,
                               key1=False, key2=False, key3=False, key4=False,
                               me=0):
    """Set the next-transition layer mask absolutely (no RMW).

    Use this for restore / preset / save-state apply paths where the
    target state is known up-front and you want to set BKGD + 4 keyers
    in a single packet. Sequential ``toggle_transition_*`` calls are
    NOT a substitute — each toggle reads ``mixerstate`` and packs the
    full mask, and mixerstate doesn't reflect prior toggles until the
    ATEM round-trips a ``TrSS`` update. Back-to-back toggles race.

    The ATEM rejects a zero mask (at-least-one-layer rule); we no-op
    rather than send a packet that would be dropped.
    """
    sel = {'background': bool(background),
           'key1': bool(key1), 'key2': bool(key2),
           'key3': bool(key3), 'key4': bool(key4)}
    mask = transition_mask_from_selection(sel)
    if mask == 0:
        return
    conn.send(TransitionSettingsCommand(index=me, next_transition=mask))


# -----------------------------------------------------------------------------
# Readers
# -----------------------------------------------------------------------------


def transition_style(mx, me=0):
    # transition-settings is keyed per M/E since Stage 4A (TrSS carries
    # the M/E index at byte 0; see TransitionSettingsField.KEY_FORMAT).
    return safe_int(_kv(mx, 'transition-settings', me, attr='style'), 0)


def transition_in_transition(mx, me=0):
    return safe_bool(
        _kv(mx, 'transition-position', me, attr='in_transition'), False)


def transition_position(mx, me=0):
    return safe_float(
        _kv(mx, 'transition-position', me, attr='position'), 0.0)


def transition_frames_remaining(mx, me=0):
    return safe_int(
        _kv(mx, 'transition-position', me, attr='frames_remaining'), 0)


def transition_selection(mx, me=0):
    """Bucket B: decomposes the next-transition mask field into the 5
    named booleans (background + 4 keyers) the UI consumes."""
    ts = _kv(mx, 'transition-settings', me)
    if ts is None:
        return {'background': True,
                'key1': False, 'key2': False, 'key3': False, 'key4': False}
    return {
        'background': safe_bool(getattr(ts, 'next_transition_bkgd', True), True),
        'key1': safe_bool(getattr(ts, 'next_transition_key1', False), False),
        'key2': safe_bool(getattr(ts, 'next_transition_key2', False), False),
        'key3': safe_bool(getattr(ts, 'next_transition_key3', False), False),
        'key4': safe_bool(getattr(ts, 'next_transition_key4', False), False),
    }


def mix_rate(mx, me=0):
    return safe_int(_kv(mx, 'transition-mix', me, attr='rate'), 25)


def dip_rate(mx, me=0):
    return safe_int(_kv(mx, 'transition-dip', me, attr='rate'), 25)


def dip_source(mx, me=0):
    return safe_int(_kv(mx, 'transition-dip', me, attr='source'), 0)


def active_transition_rate(mx, me=0):
    """Bucket B: dispatches to the right per-style rate reader based on
    the current ``transition_style``. All five per-style rate readers
    live in this file (mix/dip in 3a, wipe in 3b, dve in 3c, stinger
    in 3d), so direct references work."""
    style = transition_style(mx, me)
    by_style = {
        0: mix_rate,
        1: dip_rate,
        2: wipe_rate,
        3: dve_rate,
        4: stinger_rate,
    }
    reader = by_style.get(style)
    return reader(mx, me) if reader else 25


# =============================================================================
# Wipe transition (sub-commit 3b)
# =============================================================================

# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def set_wipe_rate(conn, rate_str, me=0):
    conn.send(WipeSettingsCommand(index=me, rate=_resolve_rate(conn, rate_str)))


def set_wipe_pattern(conn, pattern, me=0):
    conn.send(WipeSettingsCommand(index=me, pattern=int(pattern)))


def set_wipe_fill_source(conn, source, me=0):
    conn.send(WipeSettingsCommand(index=me, source=int(source)))


def set_wipe_flip_flop(conn, flip_flop, me=0):
    conn.send(WipeSettingsCommand(index=me, flipflop=bool(flip_flop)))


def set_wipe_reverse(conn, reverse, me=0):
    conn.send(WipeSettingsCommand(index=me, reverse=bool(reverse)))


def set_wipe_position_x(conn, position_x, me=0):
    """0..1 unit → wire 0..10000 (×10000), clamped. Bucket B because of
    the clamp on out-of-range caller values."""
    conn.send(WipeSettingsCommand(
        index=me, positionx=unit_to_thousandths(position_x)))


def set_wipe_position_y(conn, position_y, me=0):
    conn.send(WipeSettingsCommand(
        index=me, positiony=unit_to_thousandths(position_y)))


def set_wipe_softness(conn, softness, me=0):
    """0..100 percent → wire 0..10000 (×100), clamped. Bucket B."""
    conn.send(WipeSettingsCommand(
        index=me, softness=percent_to_thousandths(softness)))


def set_wipe_symmetry(conn, symmetry, me=0):
    conn.send(WipeSettingsCommand(
        index=me, symmetry=percent_to_thousandths(symmetry)))


def set_wipe_width(conn, width, me=0):
    conn.send(WipeSettingsCommand(
        index=me, width=percent_to_thousandths(width)))


# -----------------------------------------------------------------------------
# Readers
# -----------------------------------------------------------------------------


def wipe_rate(mx, me=0):
    return safe_int(_kv(mx, 'transition-wipe', me, attr='rate'), 25)


def wipe_pattern(mx, me=0):
    return safe_int(_kv(mx, 'transition-wipe', me, attr='pattern'), 0)


def wipe_fill_source(mx, me=0):
    return safe_int(_kv(mx, 'transition-wipe', me, attr='source'), 0)


def wipe_position_x(mx, me=0):
    return unit_from_thousandths(
        _kv(mx, 'transition-wipe', me, attr='positionx'), 0.5)


def wipe_position_y(mx, me=0):
    return unit_from_thousandths(
        _kv(mx, 'transition-wipe', me, attr='positiony'), 0.5)


def wipe_softness(mx, me=0):
    """Wire is u16 ×100 of percent (range 0..10000). Earlier code used
    a raw safe_float read which returned 100× the display value."""
    return percent_from_thousandths(
        _kv(mx, 'transition-wipe', me, attr='softness'), 0.0)


def wipe_symmetry(mx, me=0):
    return percent_from_thousandths(
        _kv(mx, 'transition-wipe', me, attr='symmetry'), 50.0)


def wipe_width(mx, me=0):
    return percent_from_thousandths(
        _kv(mx, 'transition-wipe', me, attr='width'), 0.0)


def wipe_reverse(mx, me=0):
    return safe_bool(_kv(mx, 'transition-wipe', me, attr='reverse'), False)


def wipe_flip_flop(mx, me=0):
    return safe_bool(_kv(mx, 'transition-wipe', me, attr='flipflop'), False)


# =============================================================================
# DVE transition (sub-commit 3c)
# =============================================================================

# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def set_dve_rate(conn, rate_str, me=0):
    conn.send(DveSettingsCommand(index=me, rate=_resolve_rate(conn, rate_str)))


def set_dve_fill_source(conn, source, me=0):
    conn.send(DveSettingsCommand(index=me, fill_source=int(source)))


def set_dve_key_source(conn, source, me=0):
    conn.send(DveSettingsCommand(index=me, key_source=int(source)))


def set_dve_enable_key(conn, enable_key, me=0):
    conn.send(DveSettingsCommand(index=me, key_enable=bool(enable_key)))


def set_dve_clip(conn, clip, me=0):
    """DVE-tx clip wire is ×10 of percent — same family as USK luma /
    stinger clip/gain. Bucket B because of the clamp 0..1000.
    Earlier code used percent_to_thousandths (×100) and had frontend
    writes landing 100× too low; verified empirically Bug D 2026-05-04."""
    conn.send(DveSettingsCommand(index=me, key_clip=percent_to_tenths(clip)))


def set_dve_gain(conn, gain, me=0):
    """See set_dve_clip above — same wire scale."""
    conn.send(DveSettingsCommand(index=me, key_gain=percent_to_tenths(gain)))


def set_dve_pre_multiplied(conn, pre_multiplied, me=0):
    conn.send(DveSettingsCommand(
        index=me, key_premultiplied=bool(pre_multiplied)))


def set_dve_invert_key(conn, invert_key, me=0):
    conn.send(DveSettingsCommand(index=me, key_invert=bool(invert_key)))


def set_dve_style(conn, style, me=0):
    conn.send(DveSettingsCommand(index=me, style=int(style)))


def set_dve_reverse(conn, reverse, me=0):
    conn.send(DveSettingsCommand(index=me, reverse=bool(reverse)))


def set_dve_flip_flop(conn, flip_flop, me=0):
    conn.send(DveSettingsCommand(index=me, flipflop=bool(flip_flop)))


# -----------------------------------------------------------------------------
# Readers
# -----------------------------------------------------------------------------


def dve_rate(mx, me=0):
    return safe_int(_kv(mx, 'transition-dve', me, attr='rate'), 25)


def dve_fill_source(mx, me=0):
    return safe_int(_kv(mx, 'transition-dve', me, attr='fill_source'), 1)


def dve_key_source(mx, me=0):
    return safe_int(_kv(mx, 'transition-dve', me, attr='key_source'), 1)


def dve_enable_key(mx, me=0):
    return safe_bool(_kv(mx, 'transition-dve', me, attr='key_enable'), False)


def dve_clip(mx, me=0):
    """Wire is ×10 of percent — same family as USK luma / stinger clip/gain.
    Earlier code used percent_from_thousandths (÷100), which had frontend
    showing 10× too low; verified empirically Bug D 2026-05-04."""
    return value_from_tenths(_kv(mx, 'transition-dve', me, attr='key_clip'), 0.0)


def dve_gain(mx, me=0):
    """See dve_clip above — same wire scale."""
    return value_from_tenths(
        _kv(mx, 'transition-dve', me, attr='key_gain'), 100.0)


def dve_pre_multiplied(mx, me=0):
    return safe_bool(
        _kv(mx, 'transition-dve', me, attr='key_premultiplied'), False)


def dve_invert_key(mx, me=0):
    return safe_bool(_kv(mx, 'transition-dve', me, attr='key_invert'), False)


def dve_style(mx, me=0):
    """Returns the raw style int OR None when the field has never been
    populated — preserves the pre-migration semantic so save/restore can
    distinguish 'style 0' from 'unset'."""
    v = _kv(mx, 'transition-dve', me, attr='style')
    return None if v is None else safe_int(v, None)


def dve_reverse(mx, me=0):
    return safe_bool(_kv(mx, 'transition-dve', me, attr='reverse'), False)


def dve_flip_flop(mx, me=0):
    return safe_bool(_kv(mx, 'transition-dve', me, attr='flipflop'), False)


# =============================================================================
# Stinger transition (sub-commit 3d)
# =============================================================================
#
# Wire units (CTSt outgoing, mirrors TStP incoming):
#   key_clip, key_gain: u16 0..1000 (frontend 0..100 percent → ×10)
#   preroll, duration, triggerpoint, rate: u16 frames (parsed from
#                                          seconds:frames against the
#                                          ATEM's display fps)

# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def set_stinger_rate(conn, rate_str, me=0):
    conn.send(StingerSettingsCommand(
        index=me, rate=_resolve_rate(conn, rate_str)))


def set_stinger_source(conn, source, me=0):
    """``source`` is a media-player SLOT NUMBER (u8, 1..4) — the same
    space the incoming TStP frame and the ``stinger_source`` reader use
    (resolved 2026-08-19: the reader used to default to 3010, MP1's
    frontend source ID, which is a different space and can't even fit the
    u8 wire field). Reader values round-trip into this op safely."""
    conn.send(StingerSettingsCommand(index=me, mediaplayer=int(source)))


def set_stinger_clip_duration(conn, clip_duration, me=0):
    conn.send(StingerSettingsCommand(
        index=me, duration=_resolve_rate(conn, clip_duration)))


def set_stinger_trigger_point(conn, trigger_point, me=0):
    conn.send(StingerSettingsCommand(
        index=me, triggerpoint=_resolve_rate(conn, trigger_point)))


def set_stinger_mix_rate(conn, rate_str, me=0):
    """Duplicate of ``set_stinger_rate`` — kept under both names for
    dispatch-table back-compat. Both wrap StingerSettingsCommand.rate."""
    conn.send(StingerSettingsCommand(
        index=me, rate=_resolve_rate(conn, rate_str)))


def set_stinger_pre_roll(conn, pre_roll, me=0):
    conn.send(StingerSettingsCommand(
        index=me, preroll=_resolve_rate(conn, pre_roll)))


def set_stinger_clip(conn, clip, me=0):
    """Frontend sends 0..100 percent; wire is 0..1000 (×10). No clamp
    (bucket A by the strict reading — the math is just scale)."""
    conn.send(StingerSettingsCommand(
        index=me, key_clip=int(round(float(clip) * 10))))


def set_stinger_gain(conn, gain, me=0):
    conn.send(StingerSettingsCommand(
        index=me, key_gain=int(round(float(gain) * 10))))


def set_stinger_pre_multiplied(conn, pre_multiplied, me=0):
    conn.send(StingerSettingsCommand(
        index=me, key_premultiplied=bool(pre_multiplied)))


def set_stinger_invert_key(conn, invert_key, me=0):
    conn.send(StingerSettingsCommand(
        index=me, key_invert=bool(invert_key)))


# -----------------------------------------------------------------------------
# Readers
# -----------------------------------------------------------------------------


def stinger_rate(mx, me=0):
    return safe_int(_kv(mx, 'transition-stinger', me, attr='rate'), 25)


def stinger_source(mx, me=0):
    """The wire field is ``mediaplayer`` — a u8 media-player SLOT NUMBER
    (1..4), same space as ``set_stinger_source``. Default 1 = MP1.
    (resolved 2026-08-19: the old default 3010 was MP1's frontend
    SOURCE ID — a different space that a u8 frame can't even carry, and it
    broke the UI's selected-option display when the entry was absent.)"""
    return safe_int(
        _kv(mx, 'transition-stinger', me, attr='mediaplayer'), 1)


def stinger_clip_duration(mx, me=0):
    return safe_int(_kv(mx, 'transition-stinger', me, attr='duration'), 150)


def stinger_trigger_point(mx, me=0):
    return safe_int(
        _kv(mx, 'transition-stinger', me, attr='triggerpoint'), 34)


def stinger_pre_roll(mx, me=0):
    return safe_int(_kv(mx, 'transition-stinger', me, attr='preroll'), 48)


def stinger_clip(mx, me=0):
    """Wire is ×10 of percent (0..1000 = 0..100%). Default 50.0."""
    return value_from_tenths(
        _kv(mx, 'transition-stinger', me, attr='key_clip'), 50.0)


def stinger_gain(mx, me=0):
    """See stinger_clip above — wire ×10, same pattern. Default 70.0."""
    return value_from_tenths(
        _kv(mx, 'transition-stinger', me, attr='key_gain'), 70.0)


def stinger_pre_multiplied(mx, me=0):
    """Defaults to True (note: differs from DVE-tx default of False —
    matches the ATEM's factory default for the stinger transition)."""
    return safe_bool(
        _kv(mx, 'transition-stinger', me, attr='key_premultiplied'), True)


def stinger_invert_key(mx, me=0):
    return safe_bool(
        _kv(mx, 'transition-stinger', me, attr='key_invert'), False)


# -----------------------------------------------------------------------------
# Restored upstream commands (0.15, 2026-09-11)
#
# These Send classes existed in upstream pyatem and were dropped from the
# fork because nothing exercised them. They are back, declared in the DSL
# with the exact byte layout of upstream's struct.pack strings and pinned
# byte-for-byte in tests/test_restored_upstream_commands.py. They have NOT
# been re-verified against a switcher in this fork; treat them as upstream
# did, and Wireshark-check before relying on a write on your model.
# -----------------------------------------------------------------------------


class TransitionPreviewCommand(Send):
    """``CTPr`` — enable or disable Transition Preview on an M/E (the Prev
    Trans button).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    bool   Preview enabled
    2      2    ?      unknown
    ====== ==== ====== ===========
    """
    CODE = 'CTPr'
    SIZE = 4

    index   = u8     (at=0)
    enabled = boolean(at=1)

    def __init__(self, index, enabled):
        super().__init__(index=index, enabled=enabled)


class TransitionPositionCommand(Send):
    """``CTPs`` — set the T-bar position of an M/E.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    ?      unknown
    2      2    u16    Position [0-10000]
    ====== ==== ====== ===========
    """
    CODE = 'CTPs'
    SIZE = 4

    index    = u8 (at=0)
    position = u16(at=2)

    def __init__(self, index, position):
        super().__init__(index=index, position=position)


def set_transition_preview(conn, enabled, me=0):
    """Enable/disable Transition Preview on M/E ``me``."""
    conn.send(TransitionPreviewCommand(index=me, enabled=bool(enabled)))


def set_transition_position(conn, position, me=0):
    """Move the T-bar of M/E ``me`` to ``position`` in 0..10000."""
    conn.send(TransitionPositionCommand(index=me, position=max(0, min(10000, int(position)))))
