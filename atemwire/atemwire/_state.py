# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""Private state-helper primitives + full-state assembler.

Canonical home for everything that used to live in the deleted
``atemwire.state`` shim. Holds the bytes/scale/coerce helpers and the
mixerstate navigation primitives that bucket-B readers across
``atemwire.messages.*`` rely on, plus the ``ATEMStateMixin`` /
``build_full_state`` assembler that composes per-feature readers
into the full UI snapshot dict.

Invariant on the primitives section (top of file): imports nothing
from ``atemwire.messages.*``. Each ``messages/<feature>.py`` can import
freely from here without creating a cycle. The ``ATEMStateMixin``
section below the primitives imports per-feature readers after the
primitives are defined; that ordering keeps the dependency graph
acyclic.

Contents:

  - Coercion primitives:  decode_name, safe_bool, safe_float,
                          safe_int, md5_hex.
  - Scale primitives:     percent_from_thousandths, unit_from_thousandths,
                          value_from_thousandths, value_from_tenths,
                          percent_from_unit. All actively used by
                          messages/ readers (verified 2026-08-19) —
                          the DSL ``scale=`` covers Send/Recv fields,
                          not reader-side conversions.
  - Navigation:           _kv, _bare. Walk mx[key][i1][i2]... safely.
  - Fairlight tables:     _MIX_STATE_NAMES, _EQ_BAND_FILTER_NAMES,
                          _EQ_FREQ_RANGE_NAMES.
  - Fairlight helpers:    _wire_db_to_float, _strip_id_to_addressing,
                          _fairlight_strip_dict, _eq_band_dict,
                          _strip_id_for, _dynamics_block_dict. Used by
                          the fairlight aggregators in
                          ``messages/fairlight.py``.
  - State assembler:      ``ATEMStateMixin`` + module-level
                          ``build_full_state(connection)``. Composes
                          the per-feature readers into the dict shape
                          the WebSocket consumer pushes to the browser.
"""

from typing import Any, Optional


# =============================================================================
# Coercion primitives
# =============================================================================


def decode_name(raw: Any, default: str = '') -> str:
    """atemwire returns slot/macro/file names as zero-padded bytes. PyATEMMax
    surfaced them as Python str. Decode + strip the trailing NULs."""
    if raw is None:
        return default
    if isinstance(raw, bytes):
        try:
            return raw.split(b'\x00', 1)[0].decode('utf-8', 'replace').strip()
        except Exception:
            return default
    return str(raw).strip()


def safe_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    try:
        return bool(v)
    except Exception:
        return default


def safe_float(v: Any, default: float = 0.0, places: Optional[int] = None) -> float:
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return round(f, places) if places is not None else f


def safe_int(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def md5_hex(raw: Any, default: str = '') -> str:
    """MD5 hashes come in as 16-byte blobs; display as hex. Empty blobs (all
    zeros, pre-hash) are reported as an empty string so downstream "hash
    computed yet?" checks work cleanly."""
    if not raw:
        return default
    try:
        b = bytes(raw)
    except Exception:
        return default
    if not any(b):
        return default
    return b.hex()


# =============================================================================
# Scale primitives
# -----------------------------------------------------------------------------
# All actively used by messages/ readers (verified 2026-08-19): the DSL
# ``scale=`` covers Send/Recv wire fields, while these convert on the
# reader side. Not dead code — do not queue them for removal.
# =============================================================================


def percent_from_thousandths(wire: Any, default: float = 0.0, places: int = 2) -> float:
    """Wire value 0..10000 → percentage 0..100.

    Used for: wipe symmetry, DVE clip/gain, stinger clip/gain.
    PyATEMMax already normalized these; atemwire exposes raw."""
    if wire is None:
        return default
    try:
        return round(float(wire) / 100.0, places)
    except (TypeError, ValueError):
        return default


def unit_from_thousandths(wire: Any, default: float = 0.0, places: int = 4) -> float:
    """Wire value 0..10000 → unit scalar 0..1.

    Used for: wipe positionx / positiony."""
    if wire is None:
        return default
    try:
        return round(float(wire) / 10000.0, places)
    except (TypeError, ValueError):
        return default


def value_from_thousandths(wire: Any, default: float = 0.0, places: int = 2) -> float:
    """Wire ÷ 1000. Used where ATEM emits a value in thousandths of the
    display unit — wire 0..N where N/1000 is what the operator sees in
    ATEM Software Control.

    Output is unit-typed (not percent) for fields like saturation
    (0..2.0) and color-correction (signed, roughly -1..1). For chroma
    key fields the operator sees what looks like percent but the wire
    scale is the same; caller picks the semantic from the destination
    key.

    Typical `places` mappings:
      - color-correction (brightness, contrast, red, green, blue): 3
      - saturation: 3
      - chroma key 5 fields (foreground, background, key_edge, spill,
        flare_suppression — pending Bug A): TBD on empirical verify;
        likely 3 if the frontend display multiplies unit by 100

    PRECISION RULE: when the frontend displays the result as percent
    (`value * 100` somewhere in the template), `places` must be at least
    one more than the percent-decimals shown to the operator. Wire 837
    represents ATEM's "83.7%", which is one decimal in percent — needs
    places=3 in unit to preserve through the unit→percent multiplication.
    places=2 collapses 0.837 → 0.84, displaying 84.0 instead of 83.7.

    Counterpart on the write side: operations.py _value_to_thousandths."""
    if wire is None:
        return default
    try:
        return round(float(wire) / 1000.0, places)
    except (TypeError, ValueError):
        return default


def value_from_tenths(wire: Any, default: float = 0.0, places: int = 2) -> float:
    """Wire ÷ 10. Used where ATEM emits a value with ×10 wire scale —
    wire 0..N where N/10 is what the operator sees in ATEM Software
    Control. Output is percent-typed (0..100) for current callers
    (DVE-tx clip/gain, USK luma clip/gain, stinger clip/gain), but the
    helper itself is type-agnostic — caller picks the semantic.

    Typical `places` mappings:
      - DVE-tx / luma / stinger clip / gain (percent 0..100, wire is
        in tenths of percent): 2 (matches existing default behavior
        from the prior `percent_from_thousandths(wire * 10)` workaround).

    Counterpart on the write side: operations.py _percent_to_tenths
    (clamp 0..1000 since current callers are all percent-scoped)."""
    if wire is None:
        return default
    try:
        return round(float(wire) / 10.0, places)
    except (TypeError, ValueError):
        return default


def percent_to_thousandths(v) -> int:
    """0..100 percent → 0..10000 wire (×100), clamped. Used for wipe
    softness/symmetry/width and other fields where ATEM's wire scale is
    ×100 of percent."""
    return max(0, min(10000, int(round(float(v) * 100))))


def percent_to_tenths(v) -> int:
    """0..100 percent → 0..1000 wire (×10), clamped. Used for fields
    where ATEM's wire scale is ×10 of percent (DVE-transition / USK
    luma / stinger clip/gain). NOTE: ``set_usk_pattern_size`` and
    siblings use ``percent_to_thousandths`` (×100) because their wire
    field is u16 0..10000 — that is correct, NOT a bug (it was reviewed
    and ruled a false positive; do not "fix" them to ×10)."""
    return max(0, min(1000, int(round(float(v) * 10))))


def unit_to_thousandths(v) -> int:
    """0..1 unit → 0..10000 wire (×10000), clamped. Used for wipe
    positions."""
    return max(0, min(10000, int(round(float(v) * 10000))))


def value_to_thousandths(v) -> int:
    """Display value × 1000 → wire, NOT clamped. Used for fields where
    the ATEM wire is in thousandths of the display unit and the value
    can be signed (USK chroma color-correction — brightness, contrast,
    red, green, blue, saturation). Counterpart on the read side:
    ``value_from_thousandths``."""
    return int(round(float(v) * 1000))


def size_thousandths(v) -> int:
    """0..2 → 0..2000 wire, clamped. USK DVE size."""
    return max(0, min(2000, int(round(float(v) * 1000))))


def percent_from_unit(wire: Any, default: float = 0.0, places: int = 2) -> float:
    """Wire unit scalar 0..1 → percentage 0..100.

    atemwire normalizes color generator saturation/luma to 0..1 in the field
    class itself; PyATEMMax exposes 0..100. Scale up so the frontend sees
    the PyATEMMax convention."""
    if wire is None:
        return default
    try:
        return round(float(wire) * 100.0, places)
    except (TypeError, ValueError):
        return default


# =============================================================================
# Mixerstate navigation
# =============================================================================


def _kv(mx: dict, key: str, *indices, attr: Optional[str] = None, default: Any = None):
    """Walk mx[key][i1][i2]... and optionally return an attribute on the leaf.

    atemwire stores indexed field classes under nested dicts like
    mx['key-on-air'][0][2]. Non-indexed fields are stored directly:
    mx['fade-to-black-enabled']."""
    node = mx.get(key)
    if node is None:
        return default
    for idx in indices:
        if not isinstance(node, dict):
            return default
        node = node.get(idx)
        if node is None:
            return default
    if attr is None:
        return node
    return getattr(node, attr, default)


def display_fps(mx, default: int = 25) -> int:
    """Display FPS for the ATEM's rate fields (MixSettingsCommand.rate,
    FadeToBlackConfigCommand.frames, DkeyRateCommand.rate, etc.).

    Empirically verified against an ATEM at mode 27 (1080p60, field
    rate 60): setting FtB rate in ASC to ``1:00`` stored 30 frames —
    i.e. ASC displays the doubled-rate modes (50/59.94/60) at HALF the
    field rate.

    Rule:
        - field rate < 48 fps → display at field rate (25, 30, 24)
        - field rate ≥ 48 fps → display at half the field rate
          (50→25, 59.94→30, 60→30)

    Falls back to ``default`` (25) if video-mode hasn't been received yet.

    Lives in ``_state`` rather than ``messages/system_info.py`` (where
    the rest of the system_info readers live) because every rate-
    resolving operation needs it — FtB, all transition rates, DSK rate,
    USK DVE rate. Co-locating it with the cross-cutting primitives
    keeps the per-feature modules from needing back-edges into
    system_info."""
    node = mx.get('video-mode')
    if node is None:
        return default
    rate = getattr(node, 'rate', None)
    if rate is None or rate <= 0:
        return default
    if rate >= 48:
        return int(round(rate / 2))
    return int(round(rate))


def _bare(mx: dict, key: str, attr: Optional[str] = None, default: Any = None):
    """Read a field that atemwire stores bare (not in FIELDNAME_UNIQUE) —
    mx[key] is the Field object directly, not a dict keyed by index.

    Used for: fade-to-black-enabled, macro-play-status. These are keys
    where the upstream decoder didn't register an index-struct, so
    each incoming packet overwrites the previous state in-place.
    (transition-settings moved off this list in Stage 4A — TrSS is keyed
    per M/E now.)
    """
    node = mx.get(key)
    if node is None:
        return default
    if attr is None:
        return node
    return getattr(node, attr, default)


def _me_count(mx: dict) -> int:
    """How many M/Es the connected switcher has. ``_top.me_units`` when
    the topology packet has arrived, falling back to the ``_MeC`` entry
    count (same derivation as topology['meCount']), falling back to 1
    during the handshake window — every switcher has M/E 1, and a
    single default-shaped entry is what the pre-4A builds emitted.

    Shared by the state assembler (``mes[]`` sizing) and the profile
    save/apply loops (Stage 4C-1)."""
    top = mx.get('topology')
    count = safe_int(getattr(top, 'me_units', 0), 0) if top is not None else 0
    if count <= 0:
        mec = mx.get('mixer-effect-config') or {}
        count = (max(mec) + 1) if mec else 0
    return count if count > 0 else 1


def _me_keyer_count(mx: dict, me: int) -> int:
    """Keyer count for one M/E from its ``_MeC.keyers`` entry, falling
    back to 4 during the handshake window (pre-``_MeC``). Same
    derivation the state assembler uses to size ``mes[me].usk``."""
    mec = mx.get('mixer-effect-config') or {}
    keyers = safe_int(getattr(mec.get(me), 'keyers', 0), 0)
    return keyers if keyers > 0 else 4


def _dsk_count(mx: dict) -> int:
    """How many downstream keyers the connected switcher has.
    ``_top.downstream_keyers`` when the topology packet has arrived,
    falling back to the ``dkey-state`` entry count, falling back to 1
    during the handshake window — every model has at least one DSK.

    Shared by the state assembler (``dsks[]`` sizing) and the profile
    save loop (mirrors ``_me_count``)."""
    top = mx.get('topology')
    count = safe_int(getattr(top, 'downstream_keyers', 0), 0) if top is not None else 0
    if count <= 0:
        dks = mx.get('dkey-state') or {}
        count = (max(dks) + 1) if dks else 0
    return count if count > 0 else 1


# =============================================================================
# Fairlight name tables + dict helpers
# -----------------------------------------------------------------------------
# Used by fairlight bucket-B aggregators (fairlight_strips,
# fairlight_eq_bands, fairlight_compressor / limiter / expander). These
# aggregators migrate to messages/fairlight.py late in Phase 3; the helpers
# here let that migration happen without further refactoring.
# =============================================================================

_MIX_STATE_NAMES = {1: 'Off', 2: 'On', 4: 'AudioFollowVideo'}
_EQ_BAND_FILTER_NAMES = {
    0x01: 'LowShelf', 0x02: 'LowPass', 0x04: 'BandPass',
    0x08: 'Notch', 0x10: 'HighPass', 0x20: 'HighShelf',
}
_EQ_FREQ_RANGE_NAMES = {1: 'Low', 2: 'MidLow', 4: 'MidHigh', 8: 'High'}


def _wire_db_to_float(wire: Any, lo_silenced: Optional[int] = -10000,
                      places: int = 2):
    """Convert a wire 0.01-dB integer to a Python float in dB.

    ``lo_silenced`` defines the value the ATEM uses for "fully silenced".
    When the wire value is at or below ``lo_silenced`` we return ``None``
    so the JSON payload carries `null` (the frontend renders this as
    "MUTE" / "−∞"). Pass ``None`` to disable the silenced check (EQ
    gains / pan don't have a silenced state).
    """
    iv = safe_int(wire, lo_silenced if lo_silenced is not None else 0)
    if lo_silenced is not None and iv <= lo_silenced:
        return None
    return round(iv / 100.0, places)


def _strip_id_to_addressing(strip_id: str, is_split: int) -> dict:
    """Translate the mixerstate key (e.g. '1.0', '1.1') into the
    (source, channel) pair the operations setters expect, plus a
    stable id the frontend uses."""
    try:
        base, sub = strip_id.split('.', 1)
        source = int(base)
        subchannel = int(sub)
    except (ValueError, AttributeError):
        return {'source': 0, 'channel': -1, 'subchannel': 0,
                'split': False, 'strip_id': str(strip_id)}
    split = (is_split == 0xff)
    # Stereo combined: channel = -1. Split-mono: channel = subchannel (0/1).
    channel = subchannel if split else -1
    return {
        'source': source,
        'channel': channel,
        'subchannel': subchannel,
        'split': split,
        'strip_id': strip_id,
    }


def _fairlight_strip_dict(strip_id: str, sp) -> dict:
    """Translate a single FASP record into the UI-friendly dict."""
    is_split = safe_int(getattr(sp, 'is_split', 0x01), 0x01)
    addr = _strip_id_to_addressing(strip_id, is_split)
    state_int = safe_int(getattr(sp, 'state', 1), 1)
    return {
        **addr,
        'volume_db': _wire_db_to_float(getattr(sp, 'volume', -10000)),
        'input_gain_db': _wire_db_to_float(getattr(sp, 'gain', 0),
                                           lo_silenced=None),
        'pan': round(safe_int(getattr(sp, 'pan', 0), 0) / 100.0, 2),
        'mix_type': _MIX_STATE_NAMES.get(state_int, str(state_int)),
        'mix_state': state_int,
        'delay_frames': safe_int(getattr(sp, 'delay', 0), 0),
        'eq_enable': safe_bool(getattr(sp, 'eq_enable', False), False),
        'eq_gain_db': _wire_db_to_float(getattr(sp, 'eq_gain', 0),
                                         lo_silenced=None),
        'dynamics_makeup_db': _wire_db_to_float(
            getattr(sp, 'dynamics_gain', 0), lo_silenced=None),
    }


def _eq_band_dict(band) -> dict:
    filter_int = safe_int(getattr(band, 'band_filter', 0), 0)
    range_int = safe_int(getattr(band, 'band_freq_range', 0), 0)
    return {
        'index': safe_int(getattr(band, 'band_index', 0), 0),
        'enabled': safe_bool(getattr(band, 'band_enabled', False), False),
        'filter': _EQ_BAND_FILTER_NAMES.get(filter_int, str(filter_int)),
        'filter_int': filter_int,
        'possible_filters': safe_int(
            getattr(band, 'band_possible_filters', 0), 0),
        'frequency': safe_int(getattr(band, 'band_frequency', 0), 0),
        'gain_db': round(safe_int(getattr(band, 'band_gain', 0), 0)
                         / 100.0, 2),
        'q': round(safe_int(getattr(band, 'band_q', 100), 100) / 100.0, 2),
        'range': _EQ_FREQ_RANGE_NAMES.get(range_int, str(range_int)),
        'range_int': range_int,
    }


def _strip_id_for(field) -> str:
    """Build the canonical ``<source>.<sub>`` strip id from any
    Fairlight field that has ``index``, ``is_split``, ``subchannel``."""
    src = safe_int(getattr(field, 'index', 0), 0)
    is_split = safe_int(getattr(field, 'is_split', 0x01), 0x01)
    sub = safe_int(getattr(field, 'subchannel', 0), 0)
    if is_split == 0xff:
        return f'{src}.{sub}'
    return f'{src}.0'


def _dynamics_block_dict(field, *, with_ratio: bool = False,
                         with_range: bool = False) -> dict:
    """Shared dict shape for compressor / limiter / expander blocks.
    Reads the wire ints (× 100 in atemwire.messages.fairlight) and returns
    operator-friendly floats. ``with_ratio`` and ``with_range`` toggle
    the optional fields per block."""
    out = {
        'enabled': safe_bool(getattr(field, 'enabled', False), False),
        'threshold_db': round(safe_int(getattr(field, 'threshold', 0), 0)
                              / 100.0, 2),
        'attack_ms': round(safe_int(getattr(field, 'attack', 0), 0)
                           / 100.0, 2),
        'hold_ms': round(safe_int(getattr(field, 'hold', 0), 0)
                         / 100.0, 2),
        'release_ms': round(safe_int(getattr(field, 'release', 0), 0)
                            / 100.0, 2),
    }
    if with_ratio:
        out['ratio'] = round(safe_int(getattr(field, 'ratio', 0), 0)
                             / 100.0, 2)
    if with_range:
        out['range_db'] = round(safe_int(getattr(field, 'range', 0), 0)
                                / 100.0, 2)
    return out


# =============================================================================
# State assembler — ATEMStateMixin + build_full_state free function
# -----------------------------------------------------------------------------
# Owns the shape of the state dict the WebSocket consumer emits to the
# frontend. Shape is preserved from the PyATEMMax era — control.html and
# atem_control.js key off these exact field names.
#
# The internal machinery lives on ``ATEMStateMixin`` (many private
# ``_build_*`` helpers share ``self._fps``). Most callers should reach
# this through the module-level ``build_full_state(connection)`` function
# defined at the bottom — the mixin form is preserved for
# ``ATEMConsumer``'s mixin-style usage.
#
# ``connection`` is duck-typed: ``build_full_state`` only reads
# ``.mixerstate`` (a atemwire mixerstate dict) and ``.last_run_macro_index``
# (a latched int updated by the connection's 'change:macro-play-status'
# listener).
#
# format_rate is used only by the _build_<feature> methods below; they're
# the only place that knows the per-build resolved FPS (self._fps), so they
# format the seconds:frames strings centrally instead of pushing FPS
# through every reader signature.
# =============================================================================

from atemwire.helpers import format_rate  # noqa: E402
from atemwire.messages.color_generator import color_generator  # noqa: E402
from atemwire.messages.downstream_keyer import dsk_state  # noqa: E402
from atemwire.messages.fade_to_black import (  # noqa: E402
    ftb_active, ftb_disabled, ftb_frames_remaining, ftb_in_transition,
    ftb_rate,
)
from atemwire.messages.fairlight import (  # noqa: E402
    fairlight_audio_inputs, fairlight_compressor, fairlight_eq_bands,
    fairlight_expander, fairlight_headphones, fairlight_limiter,
    fairlight_master, fairlight_solo, fairlight_strips,
)
from atemwire.messages.input_video import (  # noqa: E402
    all_input_sources, renamable_inputs,
)
from atemwire.messages.hyperdeck import hyperdeck_settings  # noqa: E402
from atemwire.messages.macros import macro_entry  # noqa: E402
from atemwire.messages.switching import (  # noqa: E402
    aux_source, preview_source, program_source,
)
from atemwire.messages.system_info import (  # noqa: E402
    available_video_modes, device_name, product_name, video_mode,
)
from atemwire.messages.transition import (  # noqa: E402
    active_transition_rate, dip_rate, dip_source, dve_clip,
    dve_enable_key, dve_fill_source, dve_flip_flop, dve_gain,
    dve_invert_key, dve_key_source, dve_pre_multiplied, dve_rate,
    dve_reverse, dve_style, mix_rate, stinger_clip,
    stinger_clip_duration, stinger_gain, stinger_invert_key,
    stinger_pre_multiplied, stinger_pre_roll, stinger_rate,
    stinger_source, stinger_trigger_point, transition_frames_remaining,
    transition_in_transition, transition_position,
    transition_selection, transition_style, wipe_fill_source,
    wipe_flip_flop, wipe_pattern, wipe_position_x, wipe_position_y,
    wipe_rate, wipe_reverse, wipe_softness, wipe_symmetry, wipe_width,
)
from atemwire.messages.upstream_keyer import (  # noqa: E402
    usk_chroma, usk_dve, usk_fill_source, usk_fly, usk_key_source,
    usk_luma, usk_mask, usk_on_air, usk_pattern, usk_type,
)


class ATEMStateMixin:
    """
    Mixin providing all ATEM state extraction and assembly.

    The main entry point is ``build_full_state(connection)``. It reads
    everything from ``connection.mixerstate`` via the per-feature reader
    functions and returns the dict consumers emit over the WebSocket.

    Most callers should use the module-level ``build_full_state(conn)``
    function — only ATEMConsumer needs the mixin form (so it can call
    ``self.build_full_state(self.connection)``).
    """

    # =========================================================================
    # Derived-value helpers (kept here because they don't belong in readers)
    # =========================================================================

    @staticmethod
    def _hsl_to_rgb_hex(hue: float, saturation: float, luma: float) -> str:
        """Convert HSL values (hue 0..360, sat/luma 0..100) to a '#rrggbb' hex
        string. Used for the color generator swatch on the frontend."""
        try:
            h = hue / 360.0
            s = saturation / 100.0
            l = luma / 100.0

            if s == 0:
                r = g = b = l
            else:
                def hue_to_rgb(p, q, t):
                    if t < 0:
                        t += 1
                    if t > 1:
                        t -= 1
                    if t < 1 / 6:
                        return p + (q - p) * 6 * t
                    if t < 1 / 2:
                        return q
                    if t < 2 / 3:
                        return p + (q - p) * (2 / 3 - t) * 6
                    return p

                q = l * (1 + s) if l < 0.5 else l + s - l * s
                p = 2 * l - q
                r = hue_to_rgb(p, q, h + 1 / 3)
                g = hue_to_rgb(p, q, h)
                b = hue_to_rgb(p, q, h - 1 / 3)

            return '#{:02x}{:02x}{:02x}'.format(
                int(round(r * 255)), int(round(g * 255)), int(round(b * 255))
            )
        except Exception:
            return '#808080'

    # =========================================================================
    # Full State Assembly
    # =========================================================================

    def build_full_state(self, connection) -> dict:
        """Assemble the complete ATEM state dict from a connection's mixerstate.

        ``connection`` is duck-typed — we only read ``.mixerstate`` (plus
        pass it to the latched-macro lookup, which reads
        ``.last_run_macro_index``). Per-M/E state (program / preview /
        transition / usk / ftb) lives under ``mes[me]`` since Stage 3C of
        the control-topology refactor; the sub-shapes inside each entry
        (and every global key) are the PyATEMMax-era output, unchanged.
        """
        if connection is None:
            return {'is_connected': False}
        mx = connection.mixerstate
        if not mx:
            return {'is_connected': False}

        # Resolve the ATEM's display FPS ONCE per state-build and reuse for
        # every seconds:frames formatting. Falls back to 25 during the
        # handshake window before the first video-mode packet arrives.
        self._fps = display_fps(mx)

        state = {'is_connected': True}
        # Per-M/E state lives under mes[me] (Stage 3C of the
        # control-topology refactor); every M/E is populated since the
        # Stage 4A multi-M/E flip. The entry shapes are byte-identical to
        # the pre-3C flat program/preview/transition/usk/ftb keys.
        state['mes'] = [{
            'program': program_source(mx, me),
            'preview': preview_source(mx, me),
            'transition': self._build_transition(mx, me),
            'usk': self._build_usk(mx, me),
            'ftb': self._build_ftb(mx, me),
        } for me in range(_me_count(mx))]
        state['dsks'] = self._build_dsks(mx)
        state['colorGenerators'] = self._build_color_generators(mx)
        state['macros'] = self._build_macros(mx)
        state['lastRunMacro'] = self._build_last_run_macro(mx, connection)
        state['auxOutputs'] = self._build_aux_outputs(mx)
        state['hyperdecks'] = self._build_hyperdecks(mx)
        state['audio'] = self._build_audio(mx)
        state['videoMode'] = video_mode(mx)

        # Expose the resolved FPS so the frontend can do its own rate math.
        if state['videoMode'] is None:
            state['videoMode'] = {'format': '', 'id': -1, 'fps': self._fps}
        else:
            state['videoMode']['fps'] = self._fps

        # Available video modes for this hardware (drives the Video Mode
        # dropdown in settings). Empty until the _VMC field arrives —
        # the polling loop picks it up automatically.
        state['videoModes'] = available_video_modes(mx)

        # Renamable inputs (cameras) for the Input Labels section in
        # settings. Each entry: {source, long, short}.
        state['inputLabels'] = renamable_inputs(mx)

        # Full source list for dynamic source-picker dropdowns. Carries
        # the available_* flags so the frontend can filter per dropdown
        # context (aux output / key source / fill).
        state['sources'] = all_input_sources(mx)

        # Hardware capability counts (Stage 2 of the control-topology
        # refactor). Counts only — source/input data stays in
        # state['sources'] / state['inputLabels'] above.
        state['topology'] = self._build_topology(mx)

        # The switcher's self-assigned name (ATEM Setup, via WhoI), falling
        # back to the model string. Added 2026-08-23 as a NEW top-level
        # key; consumers that whitelist snapshot keys must declare it.
        state['atem_name'] = device_name(mx) or product_name(mx)

        return state

    # =========================================================================
    # Transition
    # =========================================================================

    def _build_transition(self, mx, me) -> dict:
        """Build the full transition sub-dict (style + rates + selection +
        wipe/DVE/stinger detail) for one M/E. Shape preserved from the
        PyATEMMax era."""
        return {
            'style': transition_style(mx, me),
            'rate': format_rate(active_transition_rate(mx, me), self._fps),
            'in_transition': transition_in_transition(mx, me),
            'position': transition_position(mx, me),
            'frames_remaining': transition_frames_remaining(mx, me),
            'selection': transition_selection(mx, me),
            'wipe_pattern': wipe_pattern(mx, me),
            'dip_source': dip_source(mx, me),

            'mix_rate': format_rate(mix_rate(mx, me), self._fps),
            'dip_rate': format_rate(dip_rate(mx, me), self._fps),
            'wipe_rate': format_rate(wipe_rate(mx, me), self._fps),
            'dve_rate': format_rate(dve_rate(mx, me), self._fps),
            'stinger_rate': format_rate(stinger_rate(mx, me), self._fps),

            'wipe_fill_source': wipe_fill_source(mx, me),
            'wipe_flip_flop': wipe_flip_flop(mx, me),
            'wipe_position_x': wipe_position_x(mx, me),
            'wipe_position_y': wipe_position_y(mx, me),
            'wipe_reverse': wipe_reverse(mx, me),
            'wipe_softness': wipe_softness(mx, me),
            'wipe_symmetry': wipe_symmetry(mx, me),
            'wipe_width': wipe_width(mx, me),

            'stinger': {
                'source': stinger_source(mx, me),
                'clip_duration': stinger_clip_duration(mx, me),
                'trigger_point': stinger_trigger_point(mx, me),
                'mix_rate': stinger_rate(mx, me),
                'pre_roll': stinger_pre_roll(mx, me),
                'clip': stinger_clip(mx, me),
                'gain': stinger_gain(mx, me),
                'pre_multiplied': stinger_pre_multiplied(mx, me),
                'invert_key': stinger_invert_key(mx, me),
                'clip_duration_str': format_rate(stinger_clip_duration(mx, me), self._fps),
                'trigger_point_str': format_rate(stinger_trigger_point(mx, me), self._fps),
                'mix_rate_str': format_rate(stinger_rate(mx, me), self._fps),
                'pre_roll_str': format_rate(stinger_pre_roll(mx, me), self._fps),
            },

            'dve': {
                'fill_source': dve_fill_source(mx, me),
                'key_source': dve_key_source(mx, me),
                'enable_key': dve_enable_key(mx, me),
                'clip': dve_clip(mx, me),
                'gain': dve_gain(mx, me),
                'pre_multiplied': dve_pre_multiplied(mx, me),
                'invert_key': dve_invert_key(mx, me),
                'style': dve_style(mx, me),
                'reverse': dve_reverse(mx, me),
                'flip_flop': dve_flip_flop(mx, me),
            },
        }

    # =========================================================================
    # Upstream Keyers (per-M/E count from _MeC)
    # =========================================================================

    def _build_usk(self, mx, me) -> dict:
        """Assemble upstream keyer state for one M/E: on-air, type, and
        full per-key data. Sized to the M/E's ``_MeC.keyers`` count;
        falls back to 4 during the handshake window before ``_MeC``
        arrives (the pre-4A hardcoded count, correct for every 1-M/E
        model in production)."""
        keyers = _me_keyer_count(mx, me)
        states = [usk_on_air(mx, me, k) for k in range(keyers)]
        types = [usk_type(mx, me, k) for k in range(keyers)]
        data = []
        for k in range(keyers):
            item = {
                'type': usk_type(mx, me, k),
                'fill_source': usk_fill_source(mx, me, k),
                'key_source': usk_key_source(mx, me, k),
            }
            item.update(usk_luma(mx, me, k))
            item.update(usk_chroma(mx, me, k))
            item.update(usk_pattern(mx, me, k))
            item.update(usk_mask(mx, me, k))
            item.update(usk_dve(mx, me, k))
            item.update(usk_fly(mx, me, k))
            data.append(item)

        return {'states': states, 'types': types, 'data': data}

    # =========================================================================
    # Fade to Black
    # =========================================================================

    def _build_ftb(self, mx, me) -> dict:
        return {
            'active': ftb_active(mx, me),
            'rate_str': format_rate(ftb_rate(mx, me), self._fps),
            'in_transition': ftb_in_transition(mx, me),
            'frames_remaining': ftb_frames_remaining(mx, me),
            # FEna carries no usable M/E index (the RE'd byte heuristic is
            # ambiguous on multi-M/E — see FadeToBlackEnabledField). Kept
            # global until verified against real multi-M/E hardware.
            'disabled': ftb_disabled(mx),
        }

    # =========================================================================
    # Downstream Keyers
    # =========================================================================

    def _build_dsks(self, mx) -> list:
        """One entry per DSK — dsk_state() plus a seconds:frames formatted
        rate_str (the shape of the pre-3B single ``dsk`` key). Sized via
        ``_dsk_count`` (``_top`` → ``dkey-state`` entries → 1), shared
        with the profile save loop."""
        dsks = []
        for i in range(_dsk_count(mx)):
            dsk = dsk_state(mx, i)
            dsk['rate_str'] = format_rate(dsk.get('rate', 25), self._fps)
            dsks.append(dsk)
        return dsks

    # =========================================================================
    # Color Generators
    # =========================================================================

    def _build_color_generators(self, mx) -> dict:
        # Sized to the per-unit ColV entries — the same derivation
        # topology['colorGenerators'] uses (the current ``_top`` layout
        # carries no color-generator byte). Empty until the first ColV
        # packets arrive.
        entries = mx.get('color-generator') or {}
        count = (max(entries) + 1) if entries else 0
        gens = {}
        for i in range(count):
            cg = color_generator(mx, i)
            gens[i] = {
                **cg,
                'hex': self._hsl_to_rgb_hex(cg['hue'], cg['saturation'], cg['luma']),
            }
        return gens

    # =========================================================================
    # Macros (first 12 slots)
    # =========================================================================

    def _build_macros(self, mx) -> list:
        macros = []
        for i in range(12):
            entry = macro_entry(mx, i)
            name = entry['name'] if (entry['is_used'] and entry['name']) else f"Macro {i + 1}"
            macros.append({'number': i + 1, 'name': name})
        return macros

    def _build_last_run_macro(self, mx, connection) -> dict:
        """Read the latched "last run macro" index from the connection.
        ATEMConnection watches MRPr events and updates ``last_run_macro_index``
        whenever a macro is observed playing (running=True, index!=0xFFFF)."""
        latched = getattr(connection, 'last_run_macro_index', -1)
        if latched < 0 or latched == 0xFFFF:
            return {'index': -1, 'name': None}
        entry = macro_entry(mx, latched)
        name = entry['name'] if (entry['is_used'] and entry['name']) else f"Macro {latched + 1}"
        return {'index': latched, 'name': name}

    # =========================================================================
    # Aux Outputs
    # =========================================================================

    def _build_aux_outputs(self, mx) -> dict:
        # Sized to the ``_top`` aux count; falls back to 6 (the historical
        # hardcoded count) during the handshake window before ``_top``
        # arrives. Keys stay ``aux1..auxN`` (1-based, matching the UI's
        # Output numbering).
        top = mx.get('topology')
        count = safe_int(getattr(top, 'aux_outputs', 0), 0) if top is not None else 0
        if count <= 0:
            count = 6
        return {f'aux{i + 1}': aux_source(mx, i) for i in range(count)}

    def _build_hyperdecks(self, mx) -> list:
        # HyperDeck binding slots (IP + switcher input). The count is the
        # model's capability, reported by the ATEM in the topology message
        # (``_top`` offset 8), so the config panel scales per model instead of
        # always showing 10. Falls back to however many slots the ATEM actually
        # emitted via RXMS, then to 10. Unconfigured slots have IP 0.0.0.0.
        top = mx.get('topology')
        count = safe_int(getattr(top, 'hyperdecks', 0), 0) if top is not None else 0
        if count <= 0:
            emitted = mx.get('hyperdeck-settings') or {}
            count = (max(emitted) + 1) if emitted else 10
        return [hyperdeck_settings(mx, i) for i in range(count)]

    # =========================================================================
    # Hardware topology (capability counts)
    # =========================================================================

    @staticmethod
    def _build_topology(mx) -> dict:
        """Capability counts for the dynamic-count frontend surfaces.

        Counts come from the parsed capability packets where atemwire has a
        parser: ``_top`` (TopologyField), ``_MeC`` (MixerEffectConfigField,
        one per M/E), ``_mpl`` (MediaplayerSlotsField). The ATEM also emits
        ``_FAC`` / ``_MAC`` / ``_MvC`` capability packets, but atemwire has no
        Recv class for them (their raw bytes land in mixerstate under the
        4-char wire code), and the current ``_top`` layout carries no color
        generator count either. Those four counts are derived from the
        per-unit state packets the ATEM dumps at connect instead — one
        indexed ColV / MPrp / MvIn / FASP packet per unit. fairlightStrips
        is therefore the live strip count, not the input capacity.
        """
        top = mx.get('topology')
        mec = mx.get('mixer-effect-config') or {}
        mpl = mx.get('mediaplayer-slots')

        def _top_count(attr):
            return safe_int(getattr(top, attr, 0), 0) if top is not None else 0

        def _index_count(entries):
            # Indexed per-unit packets: capacity = highest index + 1.
            return (max(entries) + 1) if entries else 0

        def _port_type_count(port_type):
            # Physical-connector counts (capability info, not render
            # drivers): one InPr entry per source, bucketed by port_type
            # the same way renamable_inputs does (0 = external input,
            # 129 = aux output connector).
            count = 0
            for node in (mx.get('input-properties') or {}).values():
                if node is None:
                    continue
                if safe_int(getattr(node, 'port_type', -1), -1) == port_type:
                    count += 1
            return count

        me_count = _top_count('me_units')
        if me_count <= 0:
            me_count = _index_count(mec)

        return {
            'meCount': me_count,
            'usksPerMe': [
                safe_int(getattr(mec.get(i), 'keyers', 0), 0)
                for i in range(me_count)
            ],
            'dsks': _top_count('downstream_keyers'),
            'auxOutputs': _top_count('aux_outputs'),
            'inputs': _port_type_count(0),
            'outputs': _port_type_count(129),
            'mediaPlayers': _top_count('mediaplayers'),
            'mediaPoolStills': safe_int(getattr(mpl, 'stills', 0), 0) if mpl is not None else 0,
            'mediaPoolClips': safe_int(getattr(mpl, 'clips', 0), 0) if mpl is not None else 0,
            'colorGenerators': _index_count(mx.get('color-generator') or {}),
            'multiviewWindows': _index_count(
                (mx.get('multiviewer-input') or {}).get(0) or {}),
            'fairlightStrips': len(mx.get('fairlight-strip-properties') or {}),
            'macros': _index_count(mx.get('macro-properties') or {}),
        }

    # =========================================================================
    # Fairlight audio
    # =========================================================================

    def _build_audio(self, mx) -> dict:
        """Assemble the Fairlight section of the state dict.

        Shape (the frontend keys off these names):

            {
              'present': bool,         # False during the handshake gap
                                       # before FAMP/FASP arrive
              'master': {present, volume_db, eq_enable, eq_gain_db,
                         dynamics_makeup_db, afv,
                         eq_bands: [{index, enabled, filter, frequency,
                                     gain_db, q, range}, ...]},
              'strips': [{source, channel, subchannel, split, strip_id,
                          name, short_name, mix_type, mix_state,
                          volume_db, input_gain_db, pan, delay_frames,
                          eq_enable, eq_gain_db, eq_bands: [...],
                          dynamics_makeup_db, soloed, input_type}, ...],
              'headphones': {present, volume_db, unmuted},  # read-only
              'solo': {any_soloed, strip_id},               # read-only
            }

        Master / per-strip EQ band lists are 6 entries each, filtered
        to the strip via the "<source>.<sub>" key the AEBP decoder
        attaches.
        """
        master = fairlight_master(mx)
        master['eq_bands'] = fairlight_eq_bands(mx, strip_id=None)

        strips = fairlight_strips(mx)
        audio_inputs = fairlight_audio_inputs(mx)
        solo = fairlight_solo(mx)
        soloed_id = solo.get('strip_id')
        compressor_by_strip = fairlight_compressor(mx)
        limiter_by_strip = fairlight_limiter(mx)
        expander_by_strip = fairlight_expander(mx)

        for s in strips:
            sid = s['strip_id']
            # `display_name` was set in fairlight_strips() based on FAIP
            # type. Keep the input-properties name available too for
            # operators who renamed their cameras and want to see the
            # full name.
            s['name'] = s.get('display_name_long', s.get('display_name', ''))
            s['short_name'] = s.get('display_name', '')
            s['eq_bands'] = fairlight_eq_bands(mx, strip_id=sid)
            s['soloed'] = (sid == soloed_id)
            # Dynamics blocks. Empty dict if the strip's AICP/AILP/AIXP
            # field hasn't arrived yet (Fairlight state dumps trail the
            # rest of the handshake).
            s['compressor'] = compressor_by_strip.get(sid, {})
            s['limiter'] = limiter_by_strip.get(sid, {})
            s['expander'] = expander_by_strip.get(sid, {})
            ai = audio_inputs.get(s['source'], {})
            s['input_type'] = ai.get('type', 0)
            s['analog_level'] = ai.get('level', 0)
            # Strip the underscore-prefixed sort keys before sending to
            # the frontend.
            for k in ('_ai_type', '_ai_number', '_ai_level',
                       'display_name_long'):
                s.pop(k, None)

        return {
            # 'present' is the UI's loading gate — the Fairlight state
            # dump arrives after the rest of the handshake completes.
            'present': master['present'] and len(strips) > 0,
            'master': master,
            'strips': strips,
            'headphones': fairlight_headphones(mx),
            'solo': solo,
        }


def build_full_state(connection) -> dict:
    """Assemble the complete ATEM state dict from a connection's mixerstate.

    Module-level convenience wrapper around ``ATEMStateMixin.build_full_state``.
    Most callers should use this; ``ATEMConsumer`` keeps the mixin form
    so it can call ``self.build_full_state(self.connection)`` directly.
    """
    return ATEMStateMixin().build_full_state(connection)
