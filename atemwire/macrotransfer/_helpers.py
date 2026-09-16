# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""Shared decode/encode primitives, source-id resolvers, and enum
tables for the macrotransfer codec.

All per-feature codec files (switching.py, upstream_keyer.py,
downstream_keyer.py, media.py, fairlight.py, flow.py) import from
this module — it holds the cross-cutting layout helpers (``_d_me_*``,
``_e_me_*``, etc.) and the universally-shared symbol tables.
"""

import struct
from typing import Optional

# ===========================================================================
# Bytecode decoder primitives
# ===========================================================================
#
# Bytecode layout (LE — see module docstring):
#
#   For each op:
#     u16 LE: total op length (includes the 4-byte header)
#     u16 LE: op type code
#     bytes: parameters, length-4 bytes
#
# Most ops have an 8-byte length: 4-byte header + 4-byte parameter block.
# The 4-byte parameter block is typically two u16 LE values, or one
# u8 (mE) + one u8 (keyer) + one u16 LE (value).
#
# Verified op codes (decoded against the "Deal Cam" reference macro
# whose XML form is known from macros_only_2025-05-12_11-24-47.xml):
# ===========================================================================



def _u16le(buf, offset=0):
    return struct.unpack_from('<H', buf, offset)[0]

def _u32le(buf, offset=0):
    return struct.unpack_from('<I', buf, offset)[0]

# Internal source IDs that aren't in the live <Inputs> table but appear
# symbolically in macro XML. Used to translate numeric source IDs from
# the bytecode back to Software Control's display names.

_INTERNAL_SOURCE_NAMES = {
    0: 'Black',
    1000: 'Bars',
    2001: 'Color1',
    2002: 'Color2',
    3010: 'MediaPlayer1',
    3011: 'MediaPlayer1Key',
    3020: 'MediaPlayer2',
    3021: 'MediaPlayer2Key',
    1301: 'XLRMic',
    1401: 'ExternalTRS',
    4000: 'ProgramOut',
    4010: 'PreviewOut',
    4030: 'CleanFeed1',
    7001: 'CleanFeed1',
    7002: 'CleanFeed2',
    5010: 'ME1Program',
    5011: 'ME1Preview',
    8001: 'Auxiliary1', 8002: 'Auxiliary2', 8003: 'Auxiliary3',
    8004: 'Auxiliary4', 8005: 'Auxiliary5', 8006: 'Auxiliary6',
    9000: 'MultiViewer1',
    10010: 'ProgramOut',
    10011: 'PreviewOut',
}

def _resolve_source_to_symbol(source_id: int, mx: Optional[dict]) -> str:
    """Reverse of ``atemwire.profile._resolve_input_symbol``: turn a numeric
    source ID into the symbolic name Software Control uses in macro XML.

    Software Control's observed output uses position-based names
    (``Camera1``..``Camera10``) regardless of user renames, so we don't
    consult the live input-properties table here. ``mx`` is accepted
    for symmetry with the encoder side and forward-compatibility.
    """
    sid = int(source_id)
    if 1 <= sid <= 20:
        return f'Camera{sid}'
    if sid in _INTERNAL_SOURCE_NAMES:
        return _INTERNAL_SOURCE_NAMES[sid]
    return str(sid)

def _bool_str(v: int) -> str:
    return 'True' if v else 'False'

def _fmt_scalar(v: float, max_decimals: int = 6) -> str:
    """Format a float in the same compact form ATEM Software Control's XML
    uses: up to ``max_decimals`` digits, trailing zeros stripped, no
    trailing dot. ``0.0`` becomes ``"0"`` to match observed output."""
    if v == 0.0:
        return '0'
    s = f'{v:.{max_decimals}f}'.rstrip('0').rstrip('.')
    return s if s else '0'

# ----- Encoder helpers (used by encode_macro_bytecode) ----------------------

# Canonical symbolic-name → source-id mapping. Mirror of
# ``_INTERNAL_SOURCE_NAMES``: when multiple source IDs decode to the
# same symbol (e.g., 4000 and 10010 both → ``ProgramOut``), the table
# below picks the higher ID since that's the form the live ATEM
# emits in macros captured from Software Control.

_SYMBOL_TO_SOURCE = {
    'Black': 0, 'Bars': 1000, 'Color1': 2001, 'Color2': 2002,
    'MediaPlayer1': 3010, 'MediaPlayer1Key': 3011,
    'MediaPlayer2': 3020, 'MediaPlayer2Key': 3021,
    'XLRMic': 1301, 'ExternalTRS': 1401,
    'ProgramOut': 10010, 'PreviewOut': 10011,
    'CleanFeed1': 7001, 'CleanFeed2': 7002,
    'ME1Program': 5010, 'ME1Preview': 5011,
    'MultiViewer1': 9000,
    'Auxiliary1': 8001, 'Auxiliary2': 8002, 'Auxiliary3': 8003,
    'Auxiliary4': 8004, 'Auxiliary5': 8005, 'Auxiliary6': 8006,
}

def _resolve_symbol_to_source(name) -> int:
    """Inverse of ``_resolve_source_to_symbol``. Accepts the symbolic
    name Software Control uses in macro XML (``Camera1``, ``Black``,
    ``MediaPlayer1Key``, ...) or a numeric string. Raises
    ``ValueError`` on unknown input — the caller should turn that into
    a per-op encode failure."""
    s = str(name).strip()
    try:
        return int(s)
    except ValueError:
        pass
    if s.startswith('Camera'):
        tail = s[len('Camera'):]
        try:
            return int(tail)
        except ValueError:
            pass
    if s in _SYMBOL_TO_SOURCE:
        return _SYMBOL_TO_SOURCE[s]
    raise ValueError(f"can't resolve macro source symbol {name!r}")

def _u16le_bytes(v) -> bytes:
    return struct.pack('<H', int(v) & 0xFFFF)

def _u32le_bytes(v) -> bytes:
    return struct.pack('<I', int(v) & 0xFFFFFFFF)

def _i32le_bytes(v) -> bytes:
    # Python's struct '<i' clamps to int32 range; clamp explicitly so a
    # caller passing a value that overflows signed-32 raises here rather
    # than after silently truncating.
    iv = int(v)
    if iv < -0x80000000 or iv >= 0x80000000:
        raise ValueError(f"value {iv} out of int32 range for i32 LE encode")
    return struct.pack('<i', iv)

def _bool_byte(s) -> bytes:
    return b'\x01' if str(s).strip().lower() in ('true', '1', 'yes', 'on') else b'\x00'

def _pack_fixed_value(xml_value, divisor: float) -> bytes:
    """Inverse of ``_d_fixed_value`` — convert XML scalar back to the
    u32 LE wire form (``wire = round(xml * divisor)``)."""
    return _u32le_bytes(round(float(xml_value) * divisor))

def _pack_signed_fixed_value(xml_value, divisor: float) -> bytes:
    """Inverse of ``_d_signed_fixed_value`` — signed i32 LE."""
    return _i32le_bytes(round(float(xml_value) * divisor))

# Fixed-point decoders. Most "fractional" macro params use a 16.16-style
# encoding where the raw u32 LE value at offset 4 = wire_unit × 65.536.
# Different ops have different wire units (clip/gain are per-mille 0..1000,
# DVE size is 0..1000 too but XML scale is 0..2, mask edges are signed
# ±9000 etc.) — the XML scale factor differs per op and is captured in
# the per-op decoders below.

def _d_fixed_value(params, divisor):
    """Decode the trailing u32 LE value and divide by ``divisor`` to
    produce the XML-form scalar. ``params`` layout for these ops is
    [u8 mE, u8 keyer, u16 LE marker, u32 LE value, ...]."""
    return _u32le(params, 4) / divisor

def _i32le(buf, offset=0):
    return struct.unpack_from('<i', buf, offset)[0]

def _d_signed_fixed_value(params, divisor):
    """Signed variant for ops where the wire value can be negative
    (DVE positions, mask edges)."""
    return _i32le(params, 4) / divisor

# =============================================================================
# Tier 1 / 2 / 3 ops added 2026-04-30 — comprehensive coverage of the macro-
# recordable surface on the 1 M/E Constellation HD. Wire formats verified by
# recording each op via Software Control on a production switcher and aligning the resulting
# bytecode against the corresponding XML <Op> children. See
# tools/profile_research/Lots_2026-04-30_09-21-08.xml for the source data
# and /tmp/align_xml_bytecode.py for the alignment tool.
#
# Layout helpers used below:
#   me_marker_u16:      [u8 me, u8 family_marker, u16 LE value]      4 bytes
#   me_marker_source:   [u8 me, u8 family_marker, u16 LE source]     4 bytes
#   me_value_marker:    [u8 me, u8 value, u16 LE op_marker]          4 bytes
#   me_keyer_bool:      [u8 me, u8 keyer, u8 bool, u8 op_marker]     4 bytes
#   me_keyer_u16:       [u8 me, u8 keyer, u16 LE value]              4 bytes
#   me_keyer_marker_fp: [u8 me, u8 keyer, u16 LE marker,             8 bytes
#                        i32 LE × 65536]
#   me_marker2_fp:      [u8 me, u8 family_marker(0x88),              8 bytes
#                        u16 LE op_marker(0x026c), i32 LE × 65536]
#   gen_marker_fp:      [u8 generator, u8 _, u16 LE marker,          8 bytes
#                        i32 LE × 65536]   (color generator)
#   strip_prefix:       [u16 LE source, u16 LE family_marker,        12 bytes
#                        i64 LE sourceId]   (Fairlight)
# =============================================================================



def _d_me_marker_u16(params, mx, attr_name):
    """Decode `[u8 me, u8 _, u16 LE value]` into {mEBI, attr_name}."""
    return {
        'mixEffectBlockIndex': str(params[0]),
        attr_name: str(_u16le(params, 2)),
    }

def _e_me_marker_u16(attrs, attr_name, family_marker):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    return bytes([me, family_marker]) + _u16le_bytes(int(attrs[attr_name]))

def _d_me_marker_source(params, mx, attr_name='input'):
    return {
        'mixEffectBlockIndex': str(params[0]),
        attr_name: _resolve_source_to_symbol(_u16le(params, 2), mx),
    }

def _e_me_marker_source(attrs, family_marker, attr_name='input'):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    src = _resolve_symbol_to_source(attrs[attr_name])
    return bytes([me, family_marker]) + _u16le_bytes(src)

def _d_me_value_marker_bool(params, mx, attr_name):
    """Decode `[u8 me, u8 bool, u16 LE marker]` (Wipe/DVE bool ops)."""
    return {
        'mixEffectBlockIndex': str(params[0]),
        attr_name: 'True' if params[1] else 'False',
    }

def _e_me_value_marker_bool(attrs, attr_name, op_marker):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    val = 1 if str(attrs[attr_name]).strip().lower() in ('true', '1', 'yes', 'on') else 0
    return bytes([me, val]) + _u16le_bytes(op_marker)

def _d_me_keyer_bool(params, mx, attr_name='enable'):
    return {
        'mixEffectBlockIndex': str(params[0]),
        'keyIndex': str(params[1]),
        attr_name: 'True' if params[2] else 'False',
    }

def _e_me_keyer_bool(attrs, attr_name='enable', byte3_marker=0):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    keyer = int(attrs['keyIndex']) & 0xFF
    val = 1 if str(attrs[attr_name]).strip().lower() in ('true', '1', 'yes', 'on') else 0
    return bytes([me, keyer, val, byte3_marker])

def _d_me_keyer_u16(params, mx, attr_name):
    return {
        'mixEffectBlockIndex': str(params[0]),
        'keyIndex': str(params[1]),
        attr_name: str(_u16le(params, 2)),
    }

def _e_me_keyer_u16(attrs, attr_name):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    keyer = int(attrs['keyIndex']) & 0xFF
    return bytes([me, keyer]) + _u16le_bytes(int(attrs[attr_name]))

def _d_me_keyer_marker_fp(params, mx, attr_name, signed=True, divisor=65536.0):
    """Decode `[u8 me, u8 keyer, u16 LE marker, i32 LE × 65536]`."""
    val_int = (struct.unpack_from('<i', params, 4)[0] if signed
               else _u32le(params, 4))
    return {
        'mixEffectBlockIndex': str(params[0]),
        'keyIndex': str(params[1]),
        attr_name: _fmt_scalar(val_int / divisor),
    }

def _e_me_keyer_marker_fp(attrs, attr_name, marker_u16=0x000c,
                           signed=True, divisor=65536.0):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    keyer = int(attrs['keyIndex']) & 0xFF
    val_int = int(round(float(attrs[attr_name]) * divisor))
    return (bytes([me, keyer])
            + _u16le_bytes(marker_u16)
            + (_i32le_bytes(val_int) if signed
               else _u32le_bytes(val_int & 0xFFFFFFFF)))

def _d_me_double_marker_fp(params, mx, attr_name, signed=True, divisor=65536.0):
    """Decode `[u8 me, u8 fam_marker, u16 LE op_marker, i32 LE × 65536]`
    used by Wipe-border and Stinger-DVE fixed-point ops."""
    val_int = (struct.unpack_from('<i', params, 4)[0] if signed
               else _u32le(params, 4))
    return {
        'mixEffectBlockIndex': str(params[0]),
        attr_name: _fmt_scalar(val_int / divisor),
    }

def _e_me_double_marker_fp(attrs, attr_name, family_marker, op_marker,
                            signed=True, divisor=65536.0):
    me = int(attrs.get('mixEffectBlockIndex', 0)) & 0xFF
    val_int = int(round(float(attrs[attr_name]) * divisor))
    return (bytes([me, family_marker])
            + _u16le_bytes(op_marker)
            + (_i32le_bytes(val_int) if signed
               else _u32le_bytes(val_int & 0xFFFFFFFF)))

def _d_gen_marker_fp(params, mx, attr_name, signed=True, divisor=65536.0):
    """Color generator decoder. params[1]==0 always, no keyer."""
    val_int = (struct.unpack_from('<i', params, 4)[0] if signed
               else _u32le(params, 4))
    return {
        'colorGeneratorIndex': str(params[0]),
        attr_name: _fmt_scalar(val_int / divisor),
    }

def _e_gen_marker_fp(attrs, attr_name, marker_u16=0x026c,
                      signed=True, divisor=65536.0):
    gen = int(attrs.get('colorGeneratorIndex', 0)) & 0xFF
    val_int = int(round(float(attrs[attr_name]) * divisor))
    return (bytes([gen, 0])
            + _u16le_bytes(marker_u16)
            + (_i32le_bytes(val_int) if signed
               else _u32le_bytes(val_int & 0xFFFFFFFF)))

# --- Enum tables (verified empirically against the alignment output) ---

# Wipe pattern → wire u16 (used by TransitionWipePattern, op 0x007d).
# Names match the Software Control XML strings. Indices 2-17 were confirmed
# against ASC (HorizontalBarnDoor=2 was the original live pin); indices 0-1
# were CORRECTED 2026-08-31 — they were SDK names
# (HorizontalBar/VerticalBar) that ASC's actual Save Switcher State XML does
# NOT use; ASC spells wire 0/1 LeftToRightBar/TopToBottomBar. See
# atemwire/tests/test_wipe_pattern_names.py.

_WIPE_PATTERN_NAMES = {
    0: 'LeftToRightBar',
    1: 'TopToBottomBar',
    2: 'HorizontalBarnDoor',
    3: 'VerticalBarnDoor',
    4: 'CornersInFourBox',
    5: 'RectangleIris',
    6: 'DiamondIris',
    7: 'CircleIris',
    8: 'TopLeftBox',
    9: 'TopRightBox',
    10: 'BottomRightBox',
    11: 'BottomLeftBox',
    12: 'TopCentreBox',
    13: 'RightCentreBox',
    14: 'BottomCentreBox',
    15: 'LeftCentreBox',
    16: 'TopLeftDiagonal',
    17: 'TopRightDiagonal',
}

_WIPE_PATTERN_TO_INT = {v: k for k, v in _WIPE_PATTERN_NAMES.items()}

# DVE transition pattern → wire u8 (op TransitionDVEPattern, 0x0034).
#
# RECONCILED 2026-06-03 to the hardware-confirmed enum. The macro op stores
# the SAME wire byte as the live transition-dve.style — verified with a
# live probe against hardware (slot 10: macro byte == live style ==
# 16, which the switcher labels SqueezeTopLeft). So this MUST match
# atemwire.profile._enums.DVE_EFFECT_NAMES. The previous table's Swoosh/Spin/
# Squeeze/Push entries were inherited from a since-removed transition enum
# and proven wrong by that probe (e.g. it mislabelled 16 as
# 'SpinCWBottomLeft'); only GraphicLogoWipe=0x22 had ever been captured.
#
# Unmapped on purpose (no captured data): 0..15 (Swoosh/Spin slots on
# larger models, if they exist there), 32, 33, and 35+ (other Graphic
# wipes). Unknown bytes decode to 'Unknown_0xNNNN' and round-trip via
# rawParams rather than being guessed.

_DVE_PATTERN_NAMES = {
    # Squeeze 16..23 — hardware-confirmed (matches the profile enum).
    16: 'SqueezeTopLeft',
    17: 'SqueezeTop',
    18: 'SqueezeTopRight',
    19: 'SqueezeLeft',
    20: 'SqueezeRight',
    21: 'SqueezeBottomLeft',
    22: 'SqueezeBottom',
    23: 'SqueezeBottomRight',
    # Push 24..31 — hardware-confirmed (matches the profile enum).
    24: 'PushTopLeft',
    25: 'PushTop',
    26: 'PushTopRight',
    27: 'PushLeft',
    28: 'PushRight',
    29: 'PushBottomLeft',
    30: 'PushBottom',
    31: 'PushBottomRight',
    # 32, 33 — unknown (no captured data).
    34: 'GraphicLogoWipe',   # capture-verified (0x22).
    # 35+ — unknown (no captured data).
}

_DVE_PATTERN_TO_INT = {v: k for k, v in _DVE_PATTERN_NAMES.items()}

# FlyKey infinity location → wire u8. Captured live: TopLeft=1,
# CentreOfKey=0, BottomCentre=8, MiddleCentre=0 (alias of CentreOfKey).
# 3x3 grid positions verified empirically against the Lots XML.

_FLYKEY_LOCATION_NAMES = {
    # Decoder picks `CentreOfKey` as the canonical name for wire 0
    # (matches the older 'Deal Cam'-era convention). MiddleCentre is
    # a Software Control alias and the encoder accepts both.
    0: 'CentreOfKey',
    1: 'TopLeft',
    2: 'TopCentre',
    3: 'TopRight',
    4: 'MiddleLeft',
    6: 'MiddleRight',
    7: 'BottomLeft',
    8: 'BottomCentre',
    9: 'BottomRight',
}

_FLYKEY_LOCATION_TO_INT = {v: k for k, v in _FLYKEY_LOCATION_NAMES.items()}

# --- Tier 1: Transition family (Mix / Dip / Wipe / DVE / Stinger) ---

# Marker bytes per family (observed at param byte 1):

_FAM_MIX = 0x4D

_FAM_DIP = 0x4E

_FAM_WIPE = 0x88

_FAM_DVE = 0x88

_FAM_STINGER = 0x88

_FAM_FTB_RATE = 0x00

# Trailing op-marker u16 for fixed-point border/position ops:

_OP_MARKER_026C = 0x026C

# --- Tier 3: Fairlight EQ + dynamics + headphones ---

# Family marker bytes at strip-prefix offset 2-3:

_FAIRLIGHT_FAM_4346 = 0x4346  # FaderGain, MixType, EQGain, Compressor*, Dyn/InputGain

_FAIRLIGHT_FAM_03E1 = 0x03E1  # EQ Band*, Limiter*

_FAIRLIGHT_FAM_024A = 0x024A  # Expander*


# --- Upstream keyer + DVE type tables ---

_USK_TYPE_NAMES = {0: 'Luma', 1: 'Chroma', 2: 'Pattern', 3: 'DVE'}
_USK_TYPE_TO_INT = {v: k for k, v in _USK_TYPE_NAMES.items()}

# Trailing marker for DVE luma-clip / mask-pre-multiply ops:
_DVE_MARKER = 0x000C
