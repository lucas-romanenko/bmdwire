# SPDX-License-Identifier: LGPL-3.0-only
"""
Enum tables and format constants for the ATEM Software Control profile XML.

Pure data + one symbol-resolver helper. No dependencies on any other
profile package modules — everything else imports from here.
"""


# =============================================================================
# Format constants
# =============================================================================

PROFILE_MAJOR_VERSION = 2
PROFILE_MINOR_VERSION = 1


# =============================================================================
# Enum tables — XML name ↔ wire integer value
# =============================================================================

# Wipe/pattern style names. Indices match the wire enum used by
# WipeSettingsCommand.pattern and KeyPropertiesPatternCommand.pattern.
# Indices 0-3 were CORRECTED 2026-08-31 against a live ATEM
# Software Control "Save Switcher State" capture — one XML per wire pattern
# 0/1/2/3, whose `<WipeParameters pattern="...">` strings are the ground truth
# these must match. The old 0-3 here were wrong (LeftToRightBar/TopToBottomBar
# were mis-placed at 2/3, and HorizontalBars/VerticalBars don't exist in ASC's
# enum at all); wire 2 = HorizontalBarnDoor also matches the earlier live pin.
# Indices 4-17 were already correct. See atemwire/tests/test_wipe_pattern_names.py.
WIPE_PATTERN_NAMES = [
    'LeftToRightBar', 'TopToBottomBar',
    'HorizontalBarnDoor', 'VerticalBarnDoor',
    'CornersInFourBox',                              # index 4 (ASC-confirmed)
    'RectangleIris', 'DiamondIris', 'CircleIris',
    'TopLeftBox', 'TopRightBox', 'BottomRightBox', 'BottomLeftBox',
    'TopCentreBox', 'RightCentreBox', 'BottomCentreBox', 'LeftCentreBox',
    'TopLeftDiagonal', 'TopRightDiagonal',
]
WIPE_PATTERN_TO_INT = {n: i for i, n in enumerate(WIPE_PATTERN_NAMES)}

# DVE transition effect names → wire int (transition-dve.style).
# Complete 16-entry set for ATEM 1 M/E Constellation HD: 8 Squeeze
# (16..23) + 8 Push (24..31). Byte 16 = SqueezeTopLeft is
# hardware-confirmed by a live probe against a switcher (slot 10: the
# stored macro byte and the live transition-dve.style both read 16, which
# the switcher labels SqueezeTopLeft); the rest follow the contiguous
# Squeeze-then-Push ordering. The macro codec's
# atemwire.macrotransfer._helpers._DVE_PATTERN_NAMES was reconciled to this
# exact layout (2026-06-03), so the two tables now agree — they share the
# same wire enum.
DVE_EFFECT_NAMES = {
    16: 'SqueezeTopLeft', 17: 'SqueezeTop', 18: 'SqueezeTopRight',
    19: 'SqueezeLeft', 20: 'SqueezeRight', 21: 'SqueezeBottomLeft',
    22: 'SqueezeBottom', 23: 'SqueezeBottomRight',
    24: 'PushTopLeft', 25: 'PushTop', 26: 'PushTopRight',
    27: 'PushLeft', 28: 'PushRight', 29: 'PushBottomLeft',
    30: 'PushBottom', 31: 'PushBottomRight',
}
DVE_EFFECT_TO_INT = {v: k for k, v in DVE_EFFECT_NAMES.items()}

# USK type — int → XML name.
USK_TYPE_NAMES = {0: 'Luma', 1: 'Chroma', 2: 'Pattern', 3: 'DVE'}
USK_TYPE_TO_INT = {v: k for k, v in USK_TYPE_NAMES.items()}

# Transition style.
TRANS_STYLE_NAMES = {0: 'Mix', 1: 'Dip', 2: 'Wipe', 3: 'DVE', 4: 'Stinger'}
TRANS_STYLE_TO_INT = {v: k for k, v in TRANS_STYLE_NAMES.items()}

# Mix-option (per-input audio mix mode). Bitfield from FairlightStripPropertiesField.state.
MIX_OPTION_NAMES = {0x01: 'Off', 0x02: 'On', 0x04: 'AudioFollowVideo'}
MIX_OPTION_TO_INT = {v: k for k, v in MIX_OPTION_NAMES.items()}

# EQ band shape. Bitfield from AtemEqBandPropertiesField.band_filter.
EQ_SHAPE_NAMES = {
    0x01: 'LowShelf', 0x02: 'LowPass', 0x04: 'BandPass',
    0x08: 'Notch', 0x10: 'HighPass', 0x20: 'HighShelf',
}
EQ_SHAPE_TO_INT = {v: k for k, v in EQ_SHAPE_NAMES.items()}

# EQ band frequency range. Bitfield from band_freq_range (mirrors
# fairlight.FAIRLIGHT_EQ_FREQ_RANGES and state._EQ_FREQ_RANGE_NAMES).
EQ_FREQ_RANGE_NAMES = {1: 'Low', 2: 'MidLow', 4: 'MidHigh', 8: 'High'}
EQ_FREQ_RANGE_TO_INT = {v: k for k, v in EQ_FREQ_RANGE_NAMES.items()}

# External port type bitfield.
EXT_PORT_TYPE_NAMES = {1: 'SDI', 2: 'HDMI', 4: 'Component', 8: 'Composite', 16: 'SVideo'}

# Source ID → symbolic name lookup for macro Op elements. ATEM's name
# convention as observed in Software Control's macro export.
def _source_symbol(source_id: int) -> str:
    """Map a numeric source ID to the symbolic name used in macro Ops.
    Falls back to a numeric string if no symbolic form is known."""
    sid = int(source_id)
    if sid == 0:
        return 'Black'
    if 1 <= sid <= 20:
        return f'Camera{sid}'
    if sid == 1000:
        return 'Bars'
    if sid == 2001:
        return 'Color1'
    if sid == 2002:
        return 'Color2'
    if sid == 3010:
        return 'MediaPlayer1'
    if sid == 3011:
        return 'MediaPlayer1Key'
    if sid == 3020:
        return 'MediaPlayer2'
    if sid == 3021:
        return 'MediaPlayer2Key'
    if sid == 1301:
        return 'XLRMic'
    if sid == 1401:
        return 'ExternalTRS'
    if sid == 4000:
        return 'ProgramOut'
    if sid == 4010:
        return 'PreviewOut'
    if sid == 4030:
        return 'CleanFeed1'
    if 8001 <= sid <= 8016:
        return f'Auxiliary{sid - 8000}'
    if 9000 <= sid <= 9100:
        return f'MultiViewer{sid - 9000 + 1}'
    if sid == 10010:
        return 'ProgramOut'
    if sid == 10011:
        return 'PreviewOut'
    return str(sid)
