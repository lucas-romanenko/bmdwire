# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Fairlight audio messages — master / strip / dynamics / EQ band / levels-arm.

Fairlight is the modern audio mixer used by Constellation-class ATEMs.
Per-strip dynamics chain is post-EQ pre-fader: Expander/Gate (CIXP/AIXP),
Compressor (CICP/AICP), Limiter (CILP/AILP). Make-up gain lives in
``FairlightStripPropertiesCommand`` / ``FairlightMasterPropertiesCommand``.

Wire packets (outgoing):
    CFMP — master properties (volume / EQ / make-up / AFV)
    CFSP — per-strip properties (gain / EQ / make-up / pan / volume / state)
    CICP — per-strip compressor parameters
    CILP — per-strip limiter parameters
    CIXP — per-strip expander/gate parameters
    CEBP — per-strip EQ band parameters
    SFLN — opt-in for level meters (FMLv / FDLv broadcasts)

Wire packets (incoming):
    FAMP — master properties
    FASP — per-strip properties
    AICP — compressor state echo
    AILP — limiter state echo
    AIXP — expander/gate state echo
    FASD — strip-deleted notification
    FAIP — audio input metadata
    FMTl — Fairlight tally state
    FMHP — headphones volume / mute
    FAMS — solo state
"""

import struct
from typing import Optional

from atemwire.state import (
    _dynamics_block_dict,
    _eq_band_dict,
    _fairlight_strip_dict,
    _strip_id_for,
    _wire_db_to_float,
    decode_name,
    safe_bool,
    safe_int,
)
from atemwire.messages._dsl import Recv, Send, boolean


# Padding marker that appears in many Fairlight commands — six 0xff bytes
# at offset 8-13. Likely a session token; ATEM accepts zeros too but
# Software Control sends 0xff.
_FAIRLIGHT_PAD = b'\xff\xff\xff\xff\xff\xff'


# Mix-option name → wire bitfield (matches the XML enum).
FAIRLIGHT_MIX_OPTIONS = {'Off': 0x01, 'On': 0x02, 'AudioFollowVideo': 0x04}

# EQ band shape name → wire bitfield.
FAIRLIGHT_EQ_SHAPES = {
    'LowShelf': 0x01, 'LowPass': 0x02, 'BandPass': 0x04,
    'Notch': 0x08, 'HighPass': 0x10, 'HighShelf': 0x20,
}

# EQ band frequency range name → wire u8.
FAIRLIGHT_EQ_FREQ_RANGES = {'Low': 1, 'MidLow': 2, 'MidHigh': 4, 'High': 8}


def _split_channel(channel):
    """Encode the (split flag, channel-byte) pair from a logical channel.
    -1 → stereo (split 0x01, ch 0); else split 0xff with the channel int."""
    if channel == -1:
        return 0x01, 0x00
    return 0xff, channel


def _db_to_wire(value, *, scale: int = 100, lo: int = -10000, hi: int = 1000) -> int:
    """Convert a dB value (or the special ``-inf``) to the wire's hundredths
    representation, clamped into the field's range."""
    f = float(value)
    if f == float('-inf'):
        return lo
    if f != f:  # NaN
        return lo
    raw = int(round(f * scale))
    return max(lo, min(hi, raw))


# =============================================================================
# Outgoing
# =============================================================================


class FairlightMasterPropertiesCommand(Send):
    """``CFMP`` — Fairlight master bus parameters.

    Mask bits: 0 EQ enable, 1 EQ gain, 2 dynamics gain, 3 master volume,
               4 AFV. Layout mirrors the ``FAMP`` echo (byte 0 is the mask
               instead of padding).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Field mask
    1      1    bool   EQ enable
    2      2    ?      padding
    4      4    i32    EQ gain (×100 dB) — value in low 2 bytes (@6-7),
                       high 2 bytes (@4-5) are sign-extension
    8      2    ?      padding
    10     2    u16    Dynamics make-up gain (×100 dB)
    12     4    i32    Master volume (×100 dB)
    16     1    bool   Audio follow video
    17     3    ?      padding
    ====== ==== ====== ===========

    EQ-enable fix (2026-06-04, RE'd from a Wireshark capture of ATEM
    Software Control writing the master EQ): EQ enable was packed at
    offset 17; the ATEM reads it at offset 1 (same as the FAMP echo), so
    master EQ enable never restored. Fixed to offset 1.

    EQ-gain i32 fix (2026-06-04): EQ gain is a SIGNED i32 at offset 4, NOT an
    s16 at offset 6 — the capture shows ASC sends FF FF in bytes 4-5 to
    sign-extend negative gains (all 205 gain packets satisfy i32@4 == s16@6).
    Packing only s16@6 left bytes 4-5 zero, so a negative gain like -11.45
    (0xFB87) read as i32 0x0000FB87 = 64391 → clamped to max +20.00 dB.
    Positive gains were unaffected (zero high bytes either way), which is why
    only a negative restore exposed it. The FAMP echo still reads gain as
    i16@6 (the low 2 bytes), so save is unaffected.
    """
    CODE = 'CFMP'

    def __init__(self, eq_gain=None, dynamics_gain=None, volume=None,
                 afv=None, eq_enable=None):
        self.eq_gain = eq_gain
        self.dynamics_gain = dynamics_gain
        self.volume = volume
        self.afv = afv
        self.eq_enable = eq_enable

    def get_command(self) -> bytes:
        mask = 0
        if self.eq_enable is not None:     mask |= 1 << 0
        if self.eq_gain is not None:       mask |= 1 << 1
        if self.dynamics_gain is not None: mask |= 1 << 2
        if self.volume is not None:        mask |= 1 << 3
        if self.afv is not None:           mask |= 1 << 4

        eq_enable     = False if self.eq_enable is None else self.eq_enable
        eq_gain       = 0 if self.eq_gain is None else self.eq_gain
        dynamics_gain = 0 if self.dynamics_gain is None else self.dynamics_gain
        volume        = 0 if self.volume is None else self.volume
        afv           = False if self.afv is None else self.afv

        data = struct.pack(
            '>B ? 2x i 2x H i ? 3x',
            mask, eq_enable, eq_gain, dynamics_gain, volume, afv,
        )
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


class FairlightStripPropertiesCommand(Send):
    """``CFSP`` — Fairlight per-strip parameters.

    Mask bits: 0 delay, 1 gain, 3 EQ enable, 4 EQ gain, 5 dynamics gain,
               6 balance, 7 volume, 8 state.

    EQ gain is i32 at offset 28-31 (NOT i16 at 30 as upstream
    documented). Dynamics make-up gain is i32 at offset 32-35 (NOT u16
    at 34). Packing either as the narrower type leaves the high bytes
    zero, so a negative value reads back as a large positive on the ATEM.
    """
    CODE = 'CFSP'

    def __init__(self, source, channel, delay=None, gain=None, eq_gain=None,
                 eq_enable=None, dynamics_gain=None, balance=None,
                 volume=None, state=None):
        self.source = source
        self.channel = channel
        self.delay = delay
        self.gain = gain
        self.eq_gain = eq_gain
        self.eq_enable = eq_enable
        self.dynamics_gain = dynamics_gain
        self.balance = balance
        self.volume = volume
        self.state = state

    def get_command(self) -> bytes:
        mask = 0
        if self.delay is not None:         mask |= 1 << 0
        if self.gain is not None:          mask |= 1 << 1
        if self.eq_enable is not None:     mask |= 1 << 3
        if self.eq_gain is not None:       mask |= 1 << 4
        if self.dynamics_gain is not None: mask |= 1 << 5
        if self.balance is not None:       mask |= 1 << 6
        if self.volume is not None:        mask |= 1 << 7
        if self.state is not None:         mask |= 1 << 8

        delay         = 0 if self.delay is None else self.delay
        gain          = 0 if self.gain is None else self.gain
        eq_enable     = False if self.eq_enable is None else self.eq_enable
        eq_gain       = 0 if self.eq_gain is None else self.eq_gain
        dynamics_gain = 0 if self.dynamics_gain is None else self.dynamics_gain
        balance       = 0 if self.balance is None else self.balance
        volume        = 0 if self.volume is None else self.volume
        state         = 0 if self.state is None else self.state

        split, ch = _split_channel(self.channel)
        data = struct.pack(
            '>H H4x6sBb B 3x i xx? 1x i i h 2x iB 3x',
            mask, self.source, _FAIRLIGHT_PAD, split, ch, delay,
            gain, eq_enable, eq_gain,
            dynamics_gain, balance, volume, state,
        )
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


class FairlightCompressorPropertiesCommand(Send):
    """``CICP`` — per-strip compressor parameters.

    Mask bits: 8 enabled, 9 threshold, 10 ratio, 11 attack, 12 hold, 13 release.

    All param values are × 100 on the wire (e.g. threshold -20.50 dB
    → -2050).
    """
    CODE = 'CICP'

    def __init__(self, source, channel=-1, enabled=None, threshold=None,
                 ratio=None, attack=None, hold=None, release=None):
        self.source = source
        self.channel = channel
        self.enabled = enabled
        self.threshold = threshold
        self.ratio = ratio
        self.attack = attack
        self.hold = hold
        self.release = release

    def get_command(self) -> bytes:
        mask = 0
        if self.enabled is not None:   mask |= 1 << 8
        if self.threshold is not None: mask |= 1 << 9
        if self.ratio is not None:     mask |= 1 << 10
        if self.attack is not None:    mask |= 1 << 11
        if self.hold is not None:      mask |= 1 << 12
        if self.release is not None:   mask |= 1 << 13

        enabled = 1 if self.enabled else 0
        threshold = 0 if self.threshold is None else int(round(float(self.threshold) * 100))
        ratio     = 0 if self.ratio is None else int(round(float(self.ratio) * 100))
        attack    = 0 if self.attack is None else int(round(float(self.attack) * 100))
        hold      = 0 if self.hold is None else int(round(float(self.hold) * 100))
        release   = 0 if self.release is None else int(round(float(self.release) * 100))

        split, ch = _split_channel(self.channel)
        data = struct.pack(
            '>H H 4x 6s B b B 3x i H 2x i i i',
            mask, self.source, _FAIRLIGHT_PAD, split, ch,
            enabled, threshold, ratio, attack, hold, release,
        )
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


class FairlightLimiterPropertiesCommand(Send):
    """``CILP`` — per-strip limiter parameters.

    No ratio (limiter is fixed-ratio infinity). Mask bits: 8 enabled,
    9 threshold, 10 attack, 11 hold, 12 release.
    """
    CODE = 'CILP'

    def __init__(self, source, channel=-1, enabled=None, threshold=None,
                 attack=None, hold=None, release=None):
        self.source = source
        self.channel = channel
        self.enabled = enabled
        self.threshold = threshold
        self.attack = attack
        self.hold = hold
        self.release = release

    def get_command(self) -> bytes:
        mask = 0
        if self.enabled is not None:   mask |= 1 << 8
        if self.threshold is not None: mask |= 1 << 9
        if self.attack is not None:    mask |= 1 << 10
        if self.hold is not None:      mask |= 1 << 11
        if self.release is not None:   mask |= 1 << 12

        enabled = 1 if self.enabled else 0
        threshold = 0 if self.threshold is None else int(round(float(self.threshold) * 100))
        attack    = 0 if self.attack is None else int(round(float(self.attack) * 100))
        hold      = 0 if self.hold is None else int(round(float(self.hold) * 100))
        release   = 0 if self.release is None else int(round(float(self.release) * 100))

        split, ch = _split_channel(self.channel)
        data = struct.pack(
            '>H H 4x 6s B b B 3x i i i i',
            mask, self.source, _FAIRLIGHT_PAD, split, ch,
            enabled, threshold, attack, hold, release,
        )
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


class FairlightExpanderPropertiesCommand(Send):
    """``CIXP`` — per-strip expander/gate parameters.

    Mode byte at offset 17: 0 = Expander, 1 = Gate. Same parameter set
    for both modes; only the byte differs.

    Mask bits: 8 enabled, 9 mode, 10 threshold, 11 range, 12 ratio,
               13 attack, 14 hold, 15 release.
    """
    CODE = 'CIXP'

    def __init__(self, source, channel=-1, enabled=None, mode=None,
                 threshold=None, range=None, ratio=None, attack=None,
                 hold=None, release=None):
        self.source = source
        self.channel = channel
        self.enabled = enabled
        self.mode = mode
        self.threshold = threshold
        self.range = range
        self.ratio = ratio
        self.attack = attack
        self.hold = hold
        self.release = release

    def get_command(self) -> bytes:
        mask = 0
        if self.enabled is not None:   mask |= 1 << 8
        if self.mode is not None:      mask |= 1 << 9
        if self.threshold is not None: mask |= 1 << 10
        if self.range is not None:     mask |= 1 << 11
        if self.ratio is not None:     mask |= 1 << 12
        if self.attack is not None:    mask |= 1 << 13
        if self.hold is not None:      mask |= 1 << 14
        if self.release is not None:   mask |= 1 << 15

        enabled = 1 if self.enabled else 0
        if self.mode is None:
            mode_int = 0
        elif isinstance(self.mode, str):
            mode_int = 1 if self.mode.lower() == 'gate' else 0
        else:
            mode_int = 1 if int(self.mode) else 0
        threshold = 0 if self.threshold is None else int(round(float(self.threshold) * 100))
        range_v   = 0 if self.range is None else int(round(float(self.range) * 100))
        ratio     = 0 if self.ratio is None else int(round(float(self.ratio) * 100))
        attack    = 0 if self.attack is None else int(round(float(self.attack) * 100))
        hold      = 0 if self.hold is None else int(round(float(self.hold) * 100))
        release   = 0 if self.release is None else int(round(float(self.release) * 100))

        split, ch = _split_channel(self.channel)
        data = struct.pack(
            '>H H 4x 6s B b B B 2x i H H i i i',
            mask, self.source, _FAIRLIGHT_PAD, split, ch,
            enabled, mode_int, threshold, range_v, ratio,
            attack, hold, release,
        )
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


class FairlightMasterCompressorPropertiesCommand(Send):
    """``CMCP`` — master-out compressor parameters (SET).

    The master-bus counterpart to the per-strip ``CICP``, reverse-
    engineered from a Wireshark capture of ATEM Software Control writing
    the master compressor (2026-06-04).
    "Master" is implicit in the 4cc — there is NO source / channel
    addressing. The layout is the compact 24-byte form that the ``MOCP``
    state echo uses, with a 1-byte field mask at offset 0 (vs the
    per-strip u16 mask) and the enabled bool shifted to offset 1. All
    param values are × 100 on the wire (threshold -23.50 dB → -2350).

    Mask bits: 0x01 enabled, 0x02 threshold, 0x04 ratio, 0x08 attack,
               0x10 hold, 0x20 release.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Field mask
    1      1    bool   Enabled
    2      2    ?      padding
    4      4    i32    Threshold (×100 dB; sign-extended i32, see note)
    8      2    u16    Ratio (×100)
    10     2    ?      padding
    12     4    i32    Attack (×100 ms)
    16     4    i32    Hold (×100 ms)
    20     4    i32    Release (×100 ms)
    ====== ==== ====== ===========

    Threshold is a sign-extended i32 at offset 4, NOT the s16 at offset 6
    the field width suggests — the same trap as the master EQ gain (CFMP).
    The value sits in the low 2 bytes (6-7) with the sign extension in 4-5;
    packing it as s16@6 leaves 4-5 zero so the ATEM reads a large positive
    and clamps a negative threshold to 0 (threshold range is -60..0, so it
    ALWAYS hit the bug). Confirmed + fix validated live 2026-06-09 by a
    round-trip smoke test against hardware (set -22.5 → read -22.5 with
    i32@4; → read 0 with the old s16@6).
    """
    CODE = 'CMCP'

    def __init__(self, enabled=None, threshold=None, ratio=None,
                 attack=None, hold=None, release=None):
        self.enabled = enabled
        self.threshold = threshold
        self.ratio = ratio
        self.attack = attack
        self.hold = hold
        self.release = release

    def get_command(self) -> bytes:
        mask = 0
        if self.enabled is not None:   mask |= 0x01
        if self.threshold is not None: mask |= 0x02
        if self.ratio is not None:     mask |= 0x04
        if self.attack is not None:    mask |= 0x08
        if self.hold is not None:      mask |= 0x10
        if self.release is not None:   mask |= 0x20

        enabled = 1 if self.enabled else 0
        threshold = 0 if self.threshold is None else int(round(float(self.threshold) * 100))
        ratio     = 0 if self.ratio is None else int(round(float(self.ratio) * 100))
        attack    = 0 if self.attack is None else int(round(float(self.attack) * 100))
        hold      = 0 if self.hold is None else int(round(float(self.hold) * 100))
        release   = 0 if self.release is None else int(round(float(self.release) * 100))

        # Threshold packed as i32@4 (sign-extended), NOT s16@6 — see class
        # docstring. A negative threshold otherwise clamps to 0 on the ATEM.
        data = struct.pack(
            '>B B 2x i H 2x i i i',
            mask, enabled, threshold, ratio, attack, hold, release,
        )
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


class FairlightMasterLimiterPropertiesCommand(Send):
    """``CMLP`` — master-out limiter parameters (SET).

    Master-bus counterpart to the per-strip ``CILP``, same RE source as
    ``CMCP``. No ratio (limiter is fixed-ratio infinity), so attack / hold
    / release shift up by 2 bytes relative to the compressor — the compact
    20-byte form matching the ``AMLP`` state echo. 1-byte mask at offset 0,
    enabled at offset 1, all params × 100.

    Mask bits: 0x01 enabled, 0x02 threshold, 0x04 attack, 0x08 hold,
               0x10 release.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Field mask
    1      1    bool   Enabled
    2      2    ?      padding
    4      4    i32    Threshold (×100 dB; sign-extended i32, see note)
    8      4    i32    Attack (×100 ms)
    12     4    i32    Hold (×100 ms)
    16     4    i32    Release (×100 ms)
    ====== ==== ====== ===========

    Threshold is the same sign-extended i32@4 as the master compressor
    (CMCP) above — NOT s16@6. A negative threshold otherwise clamps to 0.
    Confirmed + fix validated live 2026-06-09 by a round-trip smoke test
    against hardware.
    """
    CODE = 'CMLP'

    def __init__(self, enabled=None, threshold=None, attack=None,
                 hold=None, release=None):
        self.enabled = enabled
        self.threshold = threshold
        self.attack = attack
        self.hold = hold
        self.release = release

    def get_command(self) -> bytes:
        mask = 0
        if self.enabled is not None:   mask |= 0x01
        if self.threshold is not None: mask |= 0x02
        if self.attack is not None:    mask |= 0x04
        if self.hold is not None:      mask |= 0x08
        if self.release is not None:   mask |= 0x10

        enabled = 1 if self.enabled else 0
        threshold = 0 if self.threshold is None else int(round(float(self.threshold) * 100))
        attack    = 0 if self.attack is None else int(round(float(self.attack) * 100))
        hold      = 0 if self.hold is None else int(round(float(self.hold) * 100))
        release   = 0 if self.release is None else int(round(float(self.release) * 100))

        # Threshold packed as i32@4 (sign-extended), NOT s16@6 — see docstring.
        data = struct.pack(
            '>B B 2x i i i i',
            mask, enabled, threshold, attack, hold, release,
        )
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


class FairlightEqBandPropertiesCommand(Send):
    """``CEBP`` — single EQ band on a Fairlight strip.

    Mask bits: 0 enabled, 1 filter, 2 range, 3 frequency, 4 gain, 5 Q.
    """
    CODE = 'CEBP'

    def __init__(self, source, channel, band, enabled=None, band_filter=None,
                 band_range=None, frequency=None, gain=None, q=None):
        self.source = source
        self.channel = channel
        self.band = band
        self.enabled = enabled
        self.filter = band_filter
        self.range = band_range
        self.frequency = frequency
        self.gain = gain
        self.q = q

    def get_command(self) -> bytes:
        mask = 0
        if self.enabled is not None:    mask |= 1 << 0
        if self.filter is not None:     mask |= 1 << 1
        if self.range is not None:      mask |= 1 << 2
        if self.frequency is not None:  mask |= 1 << 3
        if self.gain is not None:       mask |= 1 << 4
        if self.q is not None:          mask |= 1 << 5

        enabled   = 0 if self.enabled is None else self.enabled
        filt      = 0 if self.filter is None else self.filter
        rng       = 0 if self.range is None else self.range
        frequency = 0 if self.frequency is None else self.frequency
        gain      = 0 if self.gain is None else self.gain
        q         = 0 if self.q is None else self.q

        split, ch = _split_channel(self.channel)
        data = struct.pack(
            '>Bx H4x6sBb B?BB I i h2x',
            mask, self.source, _FAIRLIGHT_PAD, split, ch, self.band,
            enabled, filt, rng, frequency, gain, q,
        )
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


class FairlightMasterEqBandPropertiesCommand(Send):
    """``CMBP`` — single EQ band on the Fairlight MASTER bus (SET).

    The master-bus counterpart to the per-strip ``CEBP``, RE'd from a
    Wireshark capture of ATEM Software Control (2026-06-04). "Master" is
    implicit in the 4cc — there is NO source/channel; a 1-byte
    ``band_index`` at offset 1 selects the
    band. This is the SET counterpart to the ``AMBP`` echo, exactly as
    CMCP/CMLP are to MOCP/AMLP. ``CEBP source=0`` (the prior wiring) wrote
    Black's EQ, never the master.

    Mask bits: 0x01 enabled, 0x02 filter, 0x04 freq_range, 0x08 frequency,
               0x10 gain, 0x20 Q.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Field mask
    1      1    u8     Band index (0-5)
    2      1    bool   Enabled
    3      1    u8     Filter / shape (EQ_SHAPE bitfield)
    4      1    u8     Frequency range (EQ_FREQ_RANGE bitfield)
    5      5    ?      padding
    10     2    u16    Frequency (Hz)
    12     4    i32    Gain (×100 dB)
    16     2    u16    Q factor (×100)
    18     2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'CMBP'

    def __init__(self, band, enabled=None, band_filter=None,
                 band_range=None, frequency=None, gain=None, q=None):
        self.band = band
        self.enabled = enabled
        self.filter = band_filter
        self.range = band_range
        self.frequency = frequency
        self.gain = gain
        self.q = q

    def get_command(self) -> bytes:
        mask = 0
        if self.enabled is not None:   mask |= 0x01
        if self.filter is not None:    mask |= 0x02
        if self.range is not None:     mask |= 0x04
        if self.frequency is not None: mask |= 0x08
        if self.gain is not None:      mask |= 0x10
        if self.q is not None:         mask |= 0x20

        enabled   = 0 if self.enabled is None else self.enabled
        filt      = 0 if self.filter is None else self.filter
        rng       = 0 if self.range is None else self.range
        frequency = 0 if self.frequency is None else self.frequency
        gain      = 0 if self.gain is None else self.gain
        q         = 0 if self.q is None else self.q

        data = struct.pack(
            '>B B ?BB 5x H i H 2x',
            mask, int(self.band), enabled, filt, rng, frequency, gain, q,
        )
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


class SendFairlightLevelsCommand(Send):
    """``SFLN`` — opt-in to receive Fairlight level updates.

    ATEMs don't broadcast FMLv / FDLv by default — the caller has to
    enable this once per session and re-arm after a reconnect.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    bool   Enable
    1      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'SFLN'
    SIZE = 4

    enable = boolean(at=0)

    def __init__(self, enable):
        super().__init__(enable=bool(enable))


# =============================================================================
# Incoming
# =============================================================================


class FairlightMasterPropertiesField(Recv):
    """``FAMP`` — Fairlight master bus state.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    ?      padding
    1      1    bool   EQ enable
    2      4    ?      padding
    6      2    i16    EQ gain (× 0.01 dB)
    8      2    ?      padding
    10     2    u16    Dynamics make-up gain (× 0.01 dB)
    12     4    i32    Master volume (× 0.01 dB)
    16     1    bool   Audio follow video
    17     3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'FAMP'
    PRETTY = 'fairlight-master-properties'

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack('>x ? 4x h 2x H i ? 3x', raw)
        self.eq_enable = field[0]
        self.eq_gain = field[1]
        self.dynamics_gain = field[2]
        self.volume = field[3]
        self.afv = field[4]

    def __repr__(self):
        return (f'<fairlight-master-properties: volume={self.volume} '
                f'make-up={self.dynamics_gain} eq={self.eq_gain}>')


class FairlightStripPropertiesField(Recv):
    """``FASP`` — per-strip state echo.

    Note: byte offsets in the original docstring are off-by-2 for some
    fields (e.g. EQ gain at 34 vs the actual i16-at-32-or-34). The
    parsing string preserves the empirically-correct layout.
    """
    CODE = 'FASP'
    PRETTY = 'fairlight-strip-properties'
    KEY_FORMAT = struct.Struct('>H')

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack(
            '>H 12xBBxxB 3x h 5x ? 4x h 2x Hh 4x h x B 2x', raw)
        self.index = field[0]
        self.is_split = field[1]
        self.subchannel = field[2]
        self.delay = field[3]
        self.gain = field[4]
        self.eq_enable = field[5]
        self.eq_gain = field[6]
        self.dynamics_gain = field[7]
        self.pan = field[8]
        self.volume = field[9]
        self.state = field[10]

        if self.is_split == 0xff:
            self.strip_id = f'{self.index}.{self.subchannel}'
        else:
            self.strip_id = f'{self.index}.0'

    def __repr__(self):
        extra = f' EQ {self.eq_gain}' if self.eq_enable else ''
        return (f'<fairlight-strip-properties: index={self.strip_id} '
                f'gain={self.gain} volume={self.volume} pan={self.pan} '
                f'dgn={self.dynamics_gain}{extra}>')


class FairlightCompressorPropertiesField(Recv):
    """``AICP`` — per-strip compressor state echo. All values × 100."""
    CODE = 'AICP'
    PRETTY = 'fairlight-compressor-properties'
    KEY_FORMAT = struct.Struct('>H')

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack('>H 12xBB ?3x i H 2x i i i', raw)
        self.index = field[0]
        self.is_split = field[1]
        self.subchannel = field[2]
        self.enabled = field[3]
        self.threshold = field[4]
        self.ratio = field[5]
        self.attack = field[6]
        self.hold = field[7]
        self.release = field[8]

        if self.is_split == 0xff:
            self.strip_id = f'{self.index}.{self.subchannel}'
        else:
            self.strip_id = f'{self.index}.0'

    def __repr__(self):
        return (f'<fairlight-compressor-properties: strip={self.strip_id} '
                f'enabled={self.enabled} thr={self.threshold} '
                f'ratio={self.ratio} att={self.attack} hold={self.hold} '
                f'rel={self.release}>')


class FairlightLimiterPropertiesField(Recv):
    """``AILP`` — per-strip limiter state echo. All values × 100."""
    CODE = 'AILP'
    PRETTY = 'fairlight-limiter-properties'
    KEY_FORMAT = struct.Struct('>H')

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack('>H 12xBB ?3x i i i i', raw)
        self.index = field[0]
        self.is_split = field[1]
        self.subchannel = field[2]
        self.enabled = field[3]
        self.threshold = field[4]
        self.attack = field[5]
        self.hold = field[6]
        self.release = field[7]

        if self.is_split == 0xff:
            self.strip_id = f'{self.index}.{self.subchannel}'
        else:
            self.strip_id = f'{self.index}.0'

    def __repr__(self):
        return (f'<fairlight-limiter-properties: strip={self.strip_id} '
                f'enabled={self.enabled} thr={self.threshold} '
                f'att={self.attack} hold={self.hold} rel={self.release}>')


class FairlightExpanderPropertiesField(Recv):
    """``AIXP`` — per-strip expander/gate state echo. ``mode`` 0 = Expander,
    1 = Gate. All values × 100."""
    CODE = 'AIXP'
    PRETTY = 'fairlight-expander-properties'
    KEY_FORMAT = struct.Struct('>H')

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack('>H 12xBB ?B2x i H H i i i', raw)
        self.index = field[0]
        self.is_split = field[1]
        self.subchannel = field[2]
        self.enabled = field[3]
        self.mode = field[4]
        self.threshold = field[5]
        self.range = field[6]
        self.ratio = field[7]
        self.attack = field[8]
        self.hold = field[9]
        self.release = field[10]

        if self.is_split == 0xff:
            self.strip_id = f'{self.index}.{self.subchannel}'
        else:
            self.strip_id = f'{self.index}.0'

    def __repr__(self):
        return (f'<fairlight-expander-properties: strip={self.strip_id} '
                f'enabled={self.enabled} thr={self.threshold} '
                f'range={self.range} ratio={self.ratio} '
                f'att={self.attack} hold={self.hold} rel={self.release}>')


class FairlightMasterCompressorPropertiesField(Recv):
    """``MOCP`` — master-out compressor state echo (24 bytes). All values
    × 100.

    The master bus uses a more compact layout than the per-strip ``AICP``
    (no 16-byte strip prefix; threshold is s16 at offset 6, not i32 at 20).
    Confirmed live (Constellation HD): byte0 = enable, [6:8] threshold s16
    (fa24 = -1500 = -15.00 dB), [8:10] ratio u16 (018d = 397 = 3.97). The
    attack/hold/release i32 fields are aligned by analogy to AICP and the
    trailing ``24 54`` = 9300 = 93.00 ms release (a real field, not a
    footer). Make-up gain lives on FAMP (master properties), not here.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    bool   Enabled
    1      5    ?      markers / padding
    6      2    s16    Threshold (×100 dB)
    8      2    u16    Ratio (×100)
    10     2    ?      padding
    12     4    i32    Attack (×100 ms)
    16     4    i32    Hold (×100 ms)
    20     4    i32    Release (×100 ms)
    ====== ==== ====== ===========
    """
    CODE = 'MOCP'
    PRETTY = 'fairlight-master-compressor-properties'

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack('>B 5x h H 2x i i i', raw)
        self.enabled = bool(field[0])
        self.threshold = field[1]
        self.ratio = field[2]
        self.attack = field[3]
        self.hold = field[4]
        self.release = field[5]

    def __repr__(self):
        return (f'<fairlight-master-compressor-properties enabled={self.enabled}'
                f' thr={self.threshold} ratio={self.ratio} att={self.attack} '
                f'hold={self.hold} rel={self.release}>')


class FairlightMasterLimiterPropertiesField(Recv):
    """``AMLP`` — master-out limiter state echo (20 bytes). All values × 100.

    Same compact master layout as ``MOCP`` minus the ratio field (a limiter
    has no ratio), so attack/hold/release shift up by 4 bytes. Confirmed
    live: byte0 = enable, [6:8] threshold s16 (fe0b = -501 = -5.01 dB),
    trailing ``24 54`` = 9300 = 93.00 ms release.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    bool   Enabled
    1      5    ?      markers / padding
    6      2    s16    Threshold (×100 dB)
    8      4    i32    Attack (×100 ms)
    12     4    i32    Hold (×100 ms)
    16     4    i32    Release (×100 ms)
    ====== ==== ====== ===========
    """
    CODE = 'AMLP'
    PRETTY = 'fairlight-master-limiter-properties'

    def __init__(self, raw: bytes):
        self.raw = raw
        field = struct.unpack('>B 5x h i i i', raw)
        self.enabled = bool(field[0])
        self.threshold = field[1]
        self.attack = field[2]
        self.hold = field[3]
        self.release = field[4]

    def __repr__(self):
        return (f'<fairlight-master-limiter-properties enabled={self.enabled} '
                f'thr={self.threshold} att={self.attack} hold={self.hold} '
                f'rel={self.release}>')


class FairlightStripDeleteField(Recv):
    """``FASD`` — strip-deleted notification. Sent when source routing
    changes drop a previously-active strip."""
    CODE = 'FASD'
    PRETTY = 'fairlight-strip-delete'

    def __init__(self, raw: bytes):
        self.raw = raw

    def __repr__(self):
        return f'<fairlight-strip-delete {self.raw}>'


class FairlightAudioInputField(Recv):
    """``FAIP`` — Fairlight input metadata.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Audio source index
    2      1    u8     Input type (0 video / 1 media / 2 external)
    3      2    ?      padding
    5      1    u8     Index in group
    6      4    ?      padding
    10     1    u8     Split flag
    11     1    ?      padding
    12     1    u8     Analog input level (1=mic, 2=line)
    13     3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'FAIP'
    PRETTY = 'fairlight-audio-input'
    KEY_FORMAT = struct.Struct('>H')

    def __init__(self, raw: bytes):
        self.raw = raw
        (self.index, self.type, self.number, self.split,
         self.level) = struct.unpack('>HB 2x B xxxx B x B 3x', raw)

    def __repr__(self):
        return f'<fairlight-input index={self.index} type={self.type}>'


class FairlightTallyField(Recv):
    """``FMTl`` — Fairlight tally state. Variable-length payload (count
    + N entries, each entry 11 bytes)."""
    CODE = 'FMTl'
    PRETTY = 'fairlight-tally'

    def __init__(self, raw: bytes):
        self.raw = raw
        offset = 0
        self.num, = struct.unpack_from('>H', raw, offset)
        self.tally = {}
        offset += 15
        for _ in range(self.num):
            subchan, source, tally = struct.unpack_from('>BH?', raw, offset)
            strip_id = f'{source}.{subchan}'
            self.tally[strip_id] = tally
            offset += 11

    def __repr__(self):
        return f'<fairlight-tally {self.tally}>'


class FairlightHeadphonesField(Recv):
    """``FMHP`` — phones output volume + mute state.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      4    i32    Volume (× 0.01 dB)
    4      4    ?      padding
    8      1    bool   Unmuted (0 = muted, 1 = unmuted)
    9      23   ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'FMHP'
    PRETTY = 'fairlight-headphones'

    def __init__(self, raw: bytes):
        self.raw = raw
        self.volume, self.unmuted = struct.unpack('> i 4x ? 23x', raw)

    def __repr__(self):
        return (f'<fairlight-headphones volume={self.volume} '
                f'unmuted={self.unmuted}>')


class FairlightSoloField(Recv):
    """``FAMS`` — Fairlight solo state."""
    CODE = 'FAMS'
    PRETTY = 'fairlight-solo'

    def __init__(self, raw: bytes):
        self.raw = raw
        (self.solo, self.channel, self.is_split_lr,
         self.subchannel) = struct.unpack('> ? 8x B 12x BB', raw)

    def __repr__(self):
        ch = (self.channel if self.is_split_lr == 0x01
              else f'{self.channel}.{self.subchannel}')
        return f'<fairlight-solo active={self.solo} source={ch}>'


# =============================================================================
# Operations
# -----------------------------------------------------------------------------
# Free functions taking ``(conn, ...)``. Each emits one Send class above,
# converting operator-facing units (dB float, ms float, named enums) to the
# wire's hundredths-of-a-dB / bitfield encoding. ``_db_to_wire`` clamps into
# each field's range so out-of-range operator input doesn't get reflected
# verbatim onto the wire.
# =============================================================================


def set_fairlight_master(
    conn,
    *,
    eq_enable: Optional[bool] = None,
    eq_gain_db: Optional[float] = None,
    dynamics_makeup_db: Optional[float] = None,
    volume_db: Optional[float] = None,
    afv: Optional[bool] = None,
) -> None:
    """Set master-out Fairlight properties.

    All kwargs optional; only those provided end up in the wire mask, so
    the switcher leaves the rest unchanged. Volume accepts ``float('-inf')``
    for fully muted.

    Ranges (matching ``FairlightMasterPropertiesField``):
      - ``eq_gain_db``        : ±20.00
      - ``dynamics_makeup_db``: 0.00 .. 20.00
      - ``volume_db``         : -100.00 .. +10.00 (-inf clamped to -100)
    """
    conn.send(FairlightMasterPropertiesCommand(
        eq_enable=None if eq_enable is None else bool(eq_enable),
        eq_gain=None if eq_gain_db is None else _db_to_wire(
            eq_gain_db, scale=100, lo=-2000, hi=2000),
        dynamics_gain=None if dynamics_makeup_db is None else _db_to_wire(
            dynamics_makeup_db, scale=100, lo=0, hi=2000),
        volume=None if volume_db is None else _db_to_wire(
            volume_db, scale=100, lo=-10000, hi=1000),
        afv=None if afv is None else bool(afv),
    ))


def set_fairlight_strip(
    conn,
    source: int,
    channel: int,
    *,
    input_gain_db: Optional[float] = None,
    eq_enable: Optional[bool] = None,
    eq_gain_db: Optional[float] = None,
    dynamics_makeup_db: Optional[float] = None,
    pan: Optional[float] = None,
    fader_gain_db: Optional[float] = None,
    mix_option=None,
    delay_frames: Optional[int] = None,
) -> None:
    """Set per-strip Fairlight properties.

    ``source`` is the input source index (matches the InPr index — 1..10
    for cameras on Constellation HD, 1301 for the XLR mic, 1401 for the
    TRS jack, etc.). ``channel`` is -1 for the normal stereo channel and
    0 / 1 for split-mono channels.

    ``mix_option`` accepts the XML enum name (``'Off'``, ``'On'``,
    ``'AudioFollowVideo'``) or the integer bitfield (1, 2, 4).

    All other kwargs optional; only those provided end up in the wire mask.
    """
    state = None
    if mix_option is not None:
        if isinstance(mix_option, str):
            if mix_option not in FAIRLIGHT_MIX_OPTIONS:
                raise ValueError(f"unknown mix_option {mix_option!r}")
            state = FAIRLIGHT_MIX_OPTIONS[mix_option]
        else:
            state = int(mix_option)

    conn.send(FairlightStripPropertiesCommand(
        source=int(source),
        channel=int(channel),
        delay=None if delay_frames is None else int(delay_frames),
        gain=None if input_gain_db is None else _db_to_wire(
            input_gain_db, scale=100, lo=-10000, hi=600),
        eq_enable=None if eq_enable is None else bool(eq_enable),
        eq_gain=None if eq_gain_db is None else _db_to_wire(
            eq_gain_db, scale=100, lo=-2000, hi=2000),
        dynamics_gain=None if dynamics_makeup_db is None else _db_to_wire(
            dynamics_makeup_db, scale=100, lo=0, hi=2000),
        balance=None if pan is None else max(
            -10000, min(10000, int(round(float(pan) * 100)))),
        volume=None if fader_gain_db is None else _db_to_wire(
            fader_gain_db, scale=100, lo=-10000, hi=1000),
        state=state,
    ))


def set_fairlight_eq_band(
    conn,
    source: int,
    channel: int,
    band: int,
    *,
    enabled: Optional[bool] = None,
    shape=None,
    frequency_range=None,
    frequency_hz: Optional[int] = None,
    gain_db: Optional[float] = None,
    q: Optional[float] = None,
) -> None:
    """Set one EQ band on one Fairlight strip.

    ``source`` and ``channel`` follow the same convention as
    ``set_fairlight_strip``. ``band`` is 0..5.

    ``shape`` accepts XML enum names (``'HighPass'``, ``'LowShelf'``,
    ``'BandPass'``, ``'HighShelf'``, ``'LowPass'``, ``'Notch'``) or the
    integer bitfield. ``frequency_range`` accepts ``'Low'`` / ``'MidLow'``
    / ``'MidHigh'`` / ``'High'`` or 0..3.
    """
    band_filter = None
    if shape is not None:
        if isinstance(shape, str):
            if shape not in FAIRLIGHT_EQ_SHAPES:
                raise ValueError(f"unknown EQ shape {shape!r}")
            band_filter = FAIRLIGHT_EQ_SHAPES[shape]
        else:
            band_filter = int(shape)

    band_range = None
    if frequency_range is not None:
        if isinstance(frequency_range, str):
            if frequency_range not in FAIRLIGHT_EQ_FREQ_RANGES:
                raise ValueError(
                    f"unknown EQ frequency_range {frequency_range!r}")
            band_range = FAIRLIGHT_EQ_FREQ_RANGES[frequency_range]
        else:
            band_range = int(frequency_range)

    conn.send(FairlightEqBandPropertiesCommand(
        source=int(source),
        channel=int(channel),
        band=int(band),
        enabled=None if enabled is None else bool(enabled),
        band_filter=band_filter,
        band_range=band_range,
        frequency=None if frequency_hz is None else max(30, min(21700, int(frequency_hz))),
        gain=None if gain_db is None else _db_to_wire(
            gain_db, scale=100, lo=-2000, hi=2000),
        q=None if q is None else max(30, min(1030, int(round(float(q) * 100)))),
    ))


def set_fairlight_master_eq_band(
    conn,
    band: int,
    *,
    enabled: Optional[bool] = None,
    shape=None,
    frequency_range=None,
    frequency_hz: Optional[int] = None,
    gain_db: Optional[float] = None,
    q: Optional[float] = None,
) -> None:
    """Set one EQ band on the Fairlight MASTER bus (CMBP).

    Master-bus counterpart to ``set_fairlight_eq_band`` — no source/channel,
    just ``band`` (0..5). ``shape`` / ``frequency_range`` accept the XML enum
    names (or the integer bitfield), same partial-mask contract; values × 100
    on the wire."""
    band_filter = None
    if shape is not None:
        if isinstance(shape, str):
            if shape not in FAIRLIGHT_EQ_SHAPES:
                raise ValueError(f"unknown EQ shape {shape!r}")
            band_filter = FAIRLIGHT_EQ_SHAPES[shape]
        else:
            band_filter = int(shape)

    band_range = None
    if frequency_range is not None:
        if isinstance(frequency_range, str):
            if frequency_range not in FAIRLIGHT_EQ_FREQ_RANGES:
                raise ValueError(
                    f"unknown EQ frequency_range {frequency_range!r}")
            band_range = FAIRLIGHT_EQ_FREQ_RANGES[frequency_range]
        else:
            band_range = int(frequency_range)

    conn.send(FairlightMasterEqBandPropertiesCommand(
        band=int(band),
        enabled=None if enabled is None else bool(enabled),
        band_filter=band_filter,
        band_range=band_range,
        frequency=None if frequency_hz is None else max(30, min(21700, int(frequency_hz))),
        gain=None if gain_db is None else _db_to_wire(
            gain_db, scale=100, lo=-2000, hi=2000),
        q=None if q is None else max(30, min(1030, int(round(float(q) * 100)))),
    ))


def set_fairlight_compressor(
    conn,
    source: int,
    channel: int = -1,
    *,
    enabled: Optional[bool] = None,
    threshold_db: Optional[float] = None,
    ratio: Optional[float] = None,
    attack_ms: Optional[float] = None,
    hold_ms: Optional[float] = None,
    release_ms: Optional[float] = None,
) -> None:
    """Set per-strip compressor parameters.

    ``source`` is the input source index (matches the InPr index).
    ``channel`` is -1 for stereo strips, 0 / 1 for split mono.

    Wire range conventions (verified live, 2026-05-08):
        - threshold_db: -60.0 .. 0.0
        - ratio:        1.2 .. 20.0
        - attack_ms:    0.7 .. 100
        - hold_ms:      0 .. 4000
        - release_ms:   50 .. 4000

    All kwargs optional; only those provided end up in the wire mask.
    """
    conn.send(FairlightCompressorPropertiesCommand(
        source=int(source),
        channel=int(channel),
        enabled=None if enabled is None else bool(enabled),
        threshold=None if threshold_db is None else float(threshold_db),
        ratio=None if ratio is None else float(ratio),
        attack=None if attack_ms is None else float(attack_ms),
        hold=None if hold_ms is None else float(hold_ms),
        release=None if release_ms is None else float(release_ms),
    ))


def set_fairlight_limiter(
    conn,
    source: int,
    channel: int = -1,
    *,
    enabled: Optional[bool] = None,
    threshold_db: Optional[float] = None,
    attack_ms: Optional[float] = None,
    hold_ms: Optional[float] = None,
    release_ms: Optional[float] = None,
) -> None:
    """Set per-strip limiter parameters.

    Same signature shape as ``set_fairlight_compressor`` but no ratio (limiter
    ratio is fixed at infinity by definition).

    Wire ranges (verified live, 2026-05-08):
        - threshold_db: -60.0 .. 0.0
        - attack_ms:    0.7 .. 30
        - hold_ms:      0 .. 4000
        - release_ms:   50 .. 4000
    """
    conn.send(FairlightLimiterPropertiesCommand(
        source=int(source),
        channel=int(channel),
        enabled=None if enabled is None else bool(enabled),
        threshold=None if threshold_db is None else float(threshold_db),
        attack=None if attack_ms is None else float(attack_ms),
        hold=None if hold_ms is None else float(hold_ms),
        release=None if release_ms is None else float(release_ms),
    ))


def set_fairlight_expander(
    conn,
    source: int,
    channel: int = -1,
    *,
    enabled: Optional[bool] = None,
    mode=None,
    threshold_db: Optional[float] = None,
    range_db: Optional[float] = None,
    ratio: Optional[float] = None,
    attack_ms: Optional[float] = None,
    hold_ms: Optional[float] = None,
    release_ms: Optional[float] = None,
) -> None:
    """Set per-strip expander/gate parameters.

    ``mode`` accepts ``'gate'`` / ``'expander'`` (or 0 / 1) to switch
    the block between Gate and Expander modes; both share the same
    parameter set on the wire (range/ratio/attack/hold/release).

    Wire ranges (verified live, 2026-05-08):
        - threshold_db: -50.0 .. 0.0
        - range_db:     0 .. 60
        - ratio:        1.0 .. 3.0  (1.0 in gate mode)
        - attack_ms:    0.5 .. 100
        - hold_ms:      0 .. 4000
        - release_ms:   50 .. 4000
    """
    conn.send(FairlightExpanderPropertiesCommand(
        source=int(source),
        channel=int(channel),
        enabled=None if enabled is None else bool(enabled),
        mode=mode,
        threshold=None if threshold_db is None else float(threshold_db),
        range=None if range_db is None else float(range_db),
        ratio=None if ratio is None else float(ratio),
        attack=None if attack_ms is None else float(attack_ms),
        hold=None if hold_ms is None else float(hold_ms),
        release=None if release_ms is None else float(release_ms),
    ))


def set_fairlight_master_compressor(
    conn,
    *,
    enabled: Optional[bool] = None,
    threshold_db: Optional[float] = None,
    ratio: Optional[float] = None,
    attack_ms: Optional[float] = None,
    hold_ms: Optional[float] = None,
    release_ms: Optional[float] = None,
) -> None:
    """Set master-out compressor parameters (CMCP).

    The master-bus counterpart to ``set_fairlight_compressor`` — no
    ``source`` / ``channel`` because "master" is implicit in the 4cc.
    All kwargs optional; only those provided end up in the wire mask, the
    same partial-write contract as the per-strip setter. Values are × 100
    on the wire (handled by the command class)."""
    conn.send(FairlightMasterCompressorPropertiesCommand(
        enabled=None if enabled is None else bool(enabled),
        threshold=None if threshold_db is None else float(threshold_db),
        ratio=None if ratio is None else float(ratio),
        attack=None if attack_ms is None else float(attack_ms),
        hold=None if hold_ms is None else float(hold_ms),
        release=None if release_ms is None else float(release_ms),
    ))


def set_fairlight_master_limiter(
    conn,
    *,
    enabled: Optional[bool] = None,
    threshold_db: Optional[float] = None,
    attack_ms: Optional[float] = None,
    hold_ms: Optional[float] = None,
    release_ms: Optional[float] = None,
) -> None:
    """Set master-out limiter parameters (CMLP).

    Master-bus counterpart to ``set_fairlight_limiter`` (no ratio). Same
    partial-mask contract; values × 100 on the wire."""
    conn.send(FairlightMasterLimiterPropertiesCommand(
        enabled=None if enabled is None else bool(enabled),
        threshold=None if threshold_db is None else float(threshold_db),
        attack=None if attack_ms is None else float(attack_ms),
        hold=None if hold_ms is None else float(hold_ms),
        release=None if release_ms is None else float(release_ms),
    ))


def enable_fairlight_levels(conn, enable: bool = True) -> None:
    """Tell the ATEM to start (or stop) streaming Fairlight meter
    levels (FMLv per strip, FDLv master) at ~10 Hz per channel.

    Levels are NOT sent by default. Call this once after the handshake
    (and re-call after any reconnect) to feed the audio meters in the
    UI. Calling with ``enable=False`` stops the stream — useful when
    the audio panel closes if you want to save bandwidth, though the
    stream is small enough to leave on.
    """
    conn.send(SendFairlightLevelsCommand(enable=bool(enable)))


# =============================================================================
# Readers
# -----------------------------------------------------------------------------
# Each reader takes a atemwire mixerstate dict and returns a UI-friendly dict.
# Shape is preserved verbatim from the pre-migration ``atemwire.state`` versions
# — control.html / atem_control.js key off these field names.
#
# Wire units convention:
#   - Volume / fader gain: i32 in 0.01 dB. Range -10000..1000 maps to
#     -inf .. +10 dB. The decoder gives us the int; we expose dB as float.
#   - Master EQ gain: i16 in 0.01 dB, ±20.00 dB.
#   - Pan: i16 in 0.01 units, -10000..+10000 maps to ±100.
#   - Dynamics make-up gain: u16 in 0.01 dB, 0..2000 maps to 0..+20 dB.
#   - mix_state (FASP "state" field): bitfield 1=Off, 2=On, 4=AFV.
#   - EQ band filter: bitfield 1=LowShelf, 2=LowPass, 4=BandPass,
#     8=Notch, 16=HighPass, 32=HighShelf.
#   - EQ band freq range: bitfield 1=Low, 2=MidLow, 4=MidHigh,
#     8=High (see _EQ_FREQ_RANGE_NAMES — not an ordinal 0..3).
#
# Strip addressing in mixerstate: keys are "<source>.<subchannel>" strings.
# stereo combined is e.g. "1.0", split-mono right is "1.1". The setters
# above expect (source: int, channel: -1|0|1).
# =============================================================================


def fairlight_master(mx) -> dict:
    """Read FAMP into a dict shaped for the audio UI."""
    node = mx.get('fairlight-master-properties')
    if node is None:
        return {
            'present': False,
            'volume_db': 0.0,
            'eq_enable': False,
            'eq_gain_db': 0.0,
            'dynamics_makeup_db': 0.0,
            'afv': False,
        }
    return {
        'present': True,
        'volume_db': _wire_db_to_float(getattr(node, 'volume', -10000)),
        'eq_enable': safe_bool(getattr(node, 'eq_enable', False), False),
        'eq_gain_db': _wire_db_to_float(getattr(node, 'eq_gain', 0),
                                         lo_silenced=None),
        'dynamics_makeup_db': _wire_db_to_float(
            getattr(node, 'dynamics_gain', 0), lo_silenced=None),
        'afv': safe_bool(getattr(node, 'afv', False), False),
    }


def fairlight_strips(mx) -> list:
    """Return per-strip dicts ordered to match ATEM Software Control's
    audio-tab layout: external video inputs (cameras 1-N) first, then
    media-player audio (MP1, MP2, ...), then external audio inputs
    (Mic, TRS).

    The order is driven by FAIP (Fairlight Audio Input metadata):

      type=0 → external video input (camera) → use input-properties
               short_name; sort by source ID
      type=1 → media-player audio. ``number`` is the 0-indexed media
               player slot — display as ``MP{number+1}``. Note: on the
               1 M/E Constellation HD the source IDs for MP audio are
               2001/2002 (which overlap with the *video-side* Color1/
               Color2 source IDs — Fairlight uses its own ID space
               distinguished by FAIP type).
      type=2 → external audio input (mic / line). Hardware-specific:
               level=1 indicates a mic-level input (XLR mic),
               level=4 a line-level input (1/4" TRS jack). These
               aren't in input-properties — labels are hardcoded.

    Within each category we sort by FAIP ``number`` (the slot index
    inside the category).
    """
    strip_props = mx.get('fairlight-strip-properties', {}) or {}
    audio_inputs = mx.get('fairlight-audio-input', {}) or {}
    inputs = mx.get('input-properties', {}) or {}

    strips = []
    for strip_id, sp in strip_props.items():
        if not isinstance(strip_id, str) or '.' not in strip_id:
            continue
        strip = _fairlight_strip_dict(strip_id, sp)
        ai = audio_inputs.get(strip['source'])
        ai_type = safe_int(getattr(ai, 'type', 0), 0) if ai else 0
        ai_number = safe_int(getattr(ai, 'number', 0), 0) if ai else 0
        ai_level = safe_int(getattr(ai, 'level', 0), 0) if ai else 0
        strip['_ai_type'] = ai_type
        strip['_ai_number'] = ai_number
        strip['_ai_level'] = ai_level

        # Display name selection.
        ip = inputs.get(strip['source'])
        ip_short = decode_name(getattr(ip, 'short_name', b''), '') if ip else ''
        ip_long = decode_name(getattr(ip, 'name', b''), '') if ip else ''

        if ai_type == 0:
            # Camera-style input — use the operator-facing rename.
            strip['display_name'] = ip_short or ip_long or f'Src{strip["source"]}'
            strip['display_name_long'] = ip_long or strip['display_name']
        elif ai_type == 1:
            # Media-player audio. number is 0-indexed → display 1-indexed.
            strip['display_name'] = f'MP{ai_number + 1}'
            strip['display_name_long'] = f'Media Player {ai_number + 1}'
        elif ai_type == 2:
            # External audio input. Hardware: level=1=Mic, others=TRS/line.
            if ai_level == 1:
                strip['display_name'] = 'Mic'
                strip['display_name_long'] = 'XLR Mic'
            else:
                strip['display_name'] = 'TRS'
                strip['display_name_long'] = 'TRS Line In'
        else:
            strip['display_name'] = (ip_short or ip_long
                                      or f'Src{strip["source"]}')
            strip['display_name_long'] = (ip_long or strip['display_name'])

        strips.append(strip)

    # Stable ordering: type asc, then FAIP number asc, then subchannel.
    # type=0 (cameras) → 0; type=1 (MP) → 1; type=2 (Mic/TRS) → 2.
    strips.sort(key=lambda s: (s['_ai_type'], s['_ai_number'],
                                s['subchannel']))
    return strips


def fairlight_audio_inputs(mx) -> dict:
    """Return per-source FAIP info (input type + analog level)."""
    nodes = mx.get('fairlight-audio-input', {}) or {}
    out = {}
    for src, ai in nodes.items():
        out[int(src)] = {
            'type': safe_int(getattr(ai, 'type', 0), 0),
            'split': safe_int(getattr(ai, 'split', 0x01), 0x01),
            'level': safe_int(getattr(ai, 'level', 0), 0),
        }
    return out


def fairlight_eq_bands(mx, strip_id: Optional[str]) -> list:
    """Return the 6 EQ bands for the given strip_id, or for the master
    if strip_id is None. Bands sorted by band_index 0..5."""
    bands_by_strip = mx.get('atem-eq-band-properties', {}) or {}
    if strip_id is None:
        # Master EQ uses AMBP — separate dict in mixerstate.
        master_bands = mx.get('atem-master-eq-band-properties', {}) or {}
        bands = list(master_bands.values())
    else:
        bands = []
        # AEBP is keyed by (strip_id, band_index) nested.
        node = bands_by_strip.get(strip_id, {})
        if isinstance(node, dict):
            bands = list(node.values())
        else:
            bands = [node]
    out = [_eq_band_dict(b) for b in bands]
    out.sort(key=lambda b: b['index'])
    return out


def fairlight_compressor(mx) -> dict:
    """Return per-strip compressor state, keyed by strip_id."""
    nodes = mx.get('fairlight-compressor-properties', {}) or {}
    out = {}
    for _, node in nodes.items():
        out[_strip_id_for(node)] = _dynamics_block_dict(
            node, with_ratio=True)
    return out


def fairlight_limiter(mx) -> dict:
    """Return per-strip limiter state, keyed by strip_id."""
    nodes = mx.get('fairlight-limiter-properties', {}) or {}
    out = {}
    for _, node in nodes.items():
        out[_strip_id_for(node)] = _dynamics_block_dict(node)
    return out


def fairlight_expander(mx) -> dict:
    """Return per-strip expander/gate state, keyed by strip_id.

    Adds ``mode`` ('gate' or 'expander') alongside the standard
    threshold/attack/etc. fields. Mode is the wire byte 17 —
    0 = expander, 1 = gate (verified live against an ATEM 1 M/E
    Constellation HD via the testgateexpander capture).
    """
    nodes = mx.get('fairlight-expander-properties', {}) or {}
    out = {}
    for _, node in nodes.items():
        block = _dynamics_block_dict(node, with_ratio=True, with_range=True)
        mode_int = safe_int(getattr(node, 'mode', 0), 0)
        block['mode'] = 'gate' if mode_int else 'expander'
        out[_strip_id_for(node)] = block
    return out


def fairlight_master_compressor(mx):
    """Master-out compressor block (``MOCP``) in the same dict shape as the
    per-strip ``fairlight_compressor`` values, or ``None`` when absent."""
    node = mx.get('fairlight-master-compressor-properties')
    return _dynamics_block_dict(node, with_ratio=True) if node is not None else None


def fairlight_master_limiter(mx):
    """Master-out limiter block (``AMLP``) in the same dict shape as the
    per-strip ``fairlight_limiter`` values, or ``None`` when absent."""
    node = mx.get('fairlight-master-limiter-properties')
    return _dynamics_block_dict(node) if node is not None else None


def fairlight_headphones(mx) -> dict:
    node = mx.get('fairlight-headphones')
    if node is None:
        return {'present': False, 'volume_db': 0.0, 'unmuted': True}
    return {
        'present': True,
        'volume_db': _wire_db_to_float(
            getattr(node, 'volume', 0), lo_silenced=None),
        'unmuted': safe_bool(getattr(node, 'unmuted', True), True),
    }


def fairlight_solo(mx) -> dict:
    """Read FAMS — anything-soloed flag + the soloed strip id, if any.

    Reads the ``FairlightSoloField`` shape: ``solo`` (bool) / ``channel``
    (source id) / ``is_split_lr`` (split marker) / ``subchannel``. A split
    stereo source reports a ``{channel}.{subchannel}`` strip id; otherwise
    it's ``{channel}.0``.

    Reader fix (2026-06-10): previously looked up ``any_soloed`` / ``source``
    / ``is_split`` — names the Recv doesn't expose — so every
    getattr-with-default fired and the reader returned the empty form for
    any real input (the solo indicator was permanently "nothing soloed").
    Now reads the real attribute names. NOTE: the ``is_split_lr == 0xff``
    split test is carried over from the original logic and is NOT yet
    verified against a live FAMS capture — ``FairlightSoloField.__repr__``
    treats ``0x01`` as the non-split marker, so the exact split-suffix
    encoding still wants a hardware check; the any-soloed flag + channel id
    are correct regardless."""
    node = mx.get('fairlight-solo')
    if node is None:
        return {'any_soloed': False, 'strip_id': None}
    any_soloed = safe_bool(getattr(node, 'solo', False), False)
    if not any_soloed:
        return {'any_soloed': False, 'strip_id': None}
    src = safe_int(getattr(node, 'channel', 0), 0)
    sub = safe_int(getattr(node, 'subchannel', 0), 0)
    is_split = safe_int(getattr(node, 'is_split_lr', 0x01), 0x01)
    if is_split == 0xff:
        return {'any_soloed': True, 'strip_id': f'{src}.{sub}'}
    return {'any_soloed': True, 'strip_id': f'{src}.0'}
