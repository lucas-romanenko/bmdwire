#!/usr/bin/env python3
"""Automated profile save/restore round-trip validator (TEST ATEM ONLY).

Catches save/restore correctness gaps (like the master-EQ-gain
sign-extended-i32 bug) automatically instead of by hand. Drives a real
ATEM: sets every settable parameter to distinctive off-round values,
saves a profile, scrambles to different values, restores, and diffs.

Three correctness guarantees:

  1. SAVE reads every value correctly — set a known value A via a granular
     set-op, ``Profile.from_atem``, then assert the XML at that field equals
     an INDEPENDENT oracle of A (the value-table author hand-encodes the
     save-side scale; the harness never calls the save code to compute its
     own expectation). Because "set A then read back" also exercises the
     send op, this catches send bugs too — e.g. set master eq_gain_db=-7.25,
     a buggy i32 encode clamps to +20, save reads +20 != -7.25 -> flagged.

  2. RESTORE writes every value correctly — scramble to B, ``profile_A.apply``,
     save again, assert each field's XML matches the pre-scramble profile_A
     XML. Exercises the apply path (which uses different ops than the granular
     setters in places: configure_usk_luma, the master CFMP one-field-per-
     packet path, etc.).

  3. SECTION SELECTION honors the checkboxes —
       - save-sections: save with only section S on -> S's element present,
         every other section's element absent.
       - no-leak restore (broadcast safety): scramble all to B, restore with
         only S selected, save -> S's fields reverted to A AND every other
         field STILL B. A leak = "restore audio" silently moving program.

The value table is hand-curated per feature (NOT a walk of the ops
registry) — each row carries the set-op, the XML path, A/B inputs, and an
oracle of the save-side scale. Legitimately-non-round-trip fields
(program/preview, video_mode, camera-control/talkback/counters defaults,
macros, fly keyframes) are whitelisted out of the value assertions.

  *** DESTRUCTIVE — changes EVERYTHING on the switcher including on-air
  buses (aux, USK on-air, DSK tie). Run ONLY against a TEST ATEM. ***
  The original state is snapshotted at the start (kept in memory AND written
  to disk) and restored at the end.

Usage:
    python tools/roundtrip_smoke.py <ATEM_IP> [options]

      --mode roundtrip|save-sections|no-leak|all   (default: roundtrip)
      --features colorgen,aux,ftb,...   limit to a subset (incremental build)
      --sections aux,color_generators   limit no-leak/save-sections scope
      --include-program-preview         also round-trip program/preview (cuts to air)
      --dry-run     save current state, check every XPath resolves, print
                    planned A/B — mutates NOTHING (no confirmation needed)
      --keep-going  don't stop after the first guarantee with findings
      --no-restore  skip the final restore-to-original (debugging)
      --settle N    seconds to wait for echoes after each bulk set (default 2.5)
      --force       skip the interactive TEST-ATEM confirmation
"""

import argparse
import datetime
import logging
import os
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

logging.basicConfig(level=logging.WARNING)
logging.getLogger('atemwire').setLevel(logging.WARNING)

# From a checkout (python tools/<script>.py) the packages beside tools/
# import without an install.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from atemwire import ApplyOptions, Profile                 # noqa: E402
from atemwire.profile import MixEffectOptions, SaveOptions  # noqa: E402
from atemwire.pool import acquire_connection               # noqa: E402
from atemwire.ready import wait_state_settled  # noqa: E402

# Granular set-ops used by the value table. Imported per-feature; later
# increments extend this block as the table grows.
from atemwire.messages.color_generator import (            # noqa: E402
    set_color_generator_hue, set_color_generator_luma,
    set_color_generator_saturation,
)
from atemwire.messages.downstream_keyer import (            # noqa: E402
    configure_dsk_gain, set_dsk_fill_source, set_dsk_key_source,
    set_dsk_mask_bottom, set_dsk_mask_enabled, set_dsk_mask_left,
    set_dsk_mask_right, set_dsk_mask_top, set_dsk_on_air, set_dsk_rate,
    set_dsk_tie,
)
from atemwire.messages.fade_to_black import (               # noqa: E402
    set_ftb_disabled, set_ftb_rate,
)
from atemwire.messages.hyperdeck import set_hyperdeck_settings  # noqa: E402
from atemwire.messages.input_video import set_input_label   # noqa: E402
from atemwire.messages.media import set_media_player_still  # noqa: E402
from atemwire.messages.fairlight import (                   # noqa: E402
    set_fairlight_compressor, set_fairlight_eq_band, set_fairlight_expander,
    set_fairlight_limiter, set_fairlight_master,
    set_fairlight_master_compressor, set_fairlight_master_eq_band,
    set_fairlight_master_limiter, set_fairlight_strip,
)
from atemwire.messages.switching import set_aux_output      # noqa: E402
from atemwire.messages.transition import (                  # noqa: E402
    set_dip_rate, set_dip_source, set_dve_clip, set_dve_enable_key,
    set_dve_fill_source, set_dve_flip_flop, set_dve_gain, set_dve_invert_key,
    set_dve_key_source, set_dve_pre_multiplied, set_dve_rate, set_dve_reverse,
    set_dve_style, set_mix_rate, set_next_transition_layers, set_stinger_clip,
    set_stinger_clip_duration, set_stinger_gain, set_stinger_invert_key,
    set_stinger_mix_rate, set_stinger_pre_multiplied, set_stinger_pre_roll,
    set_stinger_source, set_stinger_trigger_point, set_transition_style,
    set_wipe_fill_source, set_wipe_flip_flop, set_wipe_pattern,
    set_wipe_position_x, set_wipe_position_y, set_wipe_rate, set_wipe_reverse,
    set_wipe_softness, set_wipe_symmetry, set_wipe_width,
)
from atemwire.messages.upstream_keyer import (              # noqa: E402
    configure_usk_luma, set_usk_chroma_background, set_usk_chroma_blue,
    set_usk_chroma_brightness, set_usk_chroma_contrast,
    set_usk_chroma_flare_suppression, set_usk_chroma_foreground,
    set_usk_chroma_green, set_usk_chroma_key_edge, set_usk_chroma_red,
    set_usk_chroma_sample_position, set_usk_chroma_sample_size,
    set_usk_chroma_sampled_color, set_usk_chroma_saturation,
    set_usk_chroma_spill, set_usk_dve_border_enabled, set_usk_dve_border_hue,
    set_usk_dve_border_inner_width, set_usk_dve_border_luma,
    set_usk_dve_border_opacity, set_usk_dve_border_outer_width,
    set_usk_dve_border_saturation, set_usk_dve_bottom, set_usk_dve_left,
    set_usk_dve_light_altitude, set_usk_dve_light_direction,
    set_usk_dve_masked, set_usk_dve_position_x, set_usk_dve_position_y,
    set_usk_dve_rate, set_usk_dve_right, set_usk_dve_rotation,
    set_usk_dve_shadow, set_usk_dve_size_x, set_usk_dve_size_y,
    set_usk_dve_top, set_usk_fill_source, set_usk_fly_enabled,
    set_usk_key_source, set_usk_mask_bottom, set_usk_mask_enabled,
    set_usk_mask_left, set_usk_mask_right, set_usk_mask_top, set_usk_on_air,
    set_usk_pattern_invert, set_usk_pattern_position_x,
    set_usk_pattern_position_y, set_usk_pattern_size, set_usk_pattern_softness,
    set_usk_pattern_style, set_usk_pattern_symmetry, set_usk_type,
)
from atemwire.profile._enums import (                       # noqa: E402
    DVE_EFFECT_TO_INT, TRANS_STYLE_TO_INT, USK_TYPE_TO_INT,
    WIPE_PATTERN_TO_INT,
)


# =============================================================================
# Section registry — maps a value-table ``section`` key to its SaveOptions /
# ApplyOptions field and its top-level XML element (for selective modes).
#
#   save:  SaveOptions attribute that gates this section's emission
#   apply: ApplyOptions attribute that gates this section's restore
#   xml:   element path whose presence/absence proves the section is in/out
#   per_me: the save/apply attributes live on mes[0] (MixEffectOptions,
#           bare names both sides — Stage 4C-1) instead of the top level
#
# usk_1..usk_4 are special-cased (they share the mes[0].usk[] list and a
# child of <Keys>); handled in the option builders, not here.
# =============================================================================

SECTIONS = {
    'aux': {
        'save': 'auxiliaries', 'apply': 'restore_aux', 'xml': 'Auxiliaries'},
    'color_generators': {
        'save': 'color_generators', 'apply': 'restore_color_generators',
        'xml': 'ColorGenerators'},
    'fade_to_black': {
        'save': 'fade_to_black', 'apply': 'fade_to_black', 'per_me': True,
        'xml': 'MixEffectBlocks/MixEffectBlock/FadeToBlack'},
    'settings_flags': {
        'save': 'settings', 'apply': 'restore_settings_flags',
        'xml': 'Settings'},
    'transition_style': {
        'save': 'transition_style', 'apply': 'transition_style',
        'per_me': True,
        'xml': 'MixEffectBlocks/MixEffectBlock/TransitionStyle'},
    'next_transition': {
        'save': 'next_transition', 'apply': 'next_transition',
        'per_me': True,
        'xml': 'MixEffectBlocks/MixEffectBlock/NextTransition'},
    'downstream_keys': {
        'save': 'downstream_keys', 'apply': 'restore_downstream_keys',
        'xml': 'DownstreamKeys'},
    'audio': {
        'save': 'fairlight', 'apply': 'restore_audio',
        'xml': 'FairlightAudioMixer'},
    'media_players': {
        'save': 'media_players', 'apply': 'restore_media_players',
        'xml': 'MediaPlayers'},
    # Input renames ride under <Settings> (no separate save flag), so selective
    # SAVE can't isolate them from settings_flags; selective RESTORE can
    # (restore_inputs is its own ApplyOptions flag).
    'inputs': {
        'save': 'settings', 'apply': 'restore_inputs',
        'xml': 'Settings/Inputs'},
    'hyperdecks': {
        'save': 'hyperdecks', 'apply': 'restore_hyperdecks',
        'xml': 'HyperDecks'},
}


# =============================================================================
# Value table
# -----------------------------------------------------------------------------
# VALUE_TABLE maps feature-name -> list of rows. Each row:
#
#   field   : stable id for reporting
#   section : key into SECTIONS (which save/apply flag owns this field)
#   set     : fn(conn, value) — push a value via a granular set-op
#   xml     : "element/path@attribute" read back after save
#   a, b    : the two set-op input values (off-round; A != B)
#   ONE oracle of the save-side scale (how A/B appears in the XML):
#     (default)     identity — XML == input value
#     save_scale=k  XML == input * k     (linear scale fields)
#     a_xml/b_xml   explicit literals     (sign-ext, offset, -inf, enum names)
#   tol     : numeric compare tolerance (default 0.05)
#   note    : informational annotation, printed in --dry-run; still ASSERTED
#   quirk   : exclusion string; if set the field is EXCLUDED from value asserts
#             (legitimately-non-round-trip / whitelist) but still printed
# =============================================================================

def _build_value_table():
    table = {}

    # ---- color_generators (idx 0 + 1) — solid-colour sources; not on-air ----
    # hue: degrees 0..359.9 (wire x10). sat/luma: percent 0..100 (wire x1000,
    # reader returns percent). All identity round-trip.
    cg_rows = []
    for idx, (ha, hb, sa, sb, la, lb) in {
        0: (217.3, 42.6, 73.0, 21.0, 58.0, 12.0),
        1: (314.7, 88.1, 9.0, 64.0, 77.0, 33.0),
    }.items():
        base = f"ColorGenerators/ColorGenerator[@index='{idx}']"
        cg_rows += [
            {'field': f'colorgen{idx}.hue', 'section': 'color_generators',
             'set': (lambda c, v, i=idx: set_color_generator_hue(c, i, v)),
             'xml': base + '@hue', 'a': ha, 'b': hb, 'tol': 0.1},
            {'field': f'colorgen{idx}.saturation', 'section': 'color_generators',
             'set': (lambda c, v, i=idx: set_color_generator_saturation(c, i, v)),
             'xml': base + '@saturation', 'a': sa, 'b': sb, 'tol': 0.2},
            {'field': f'colorgen{idx}.luma', 'section': 'color_generators',
             'set': (lambda c, v, i=idx: set_color_generator_luma(c, i, v)),
             'xml': base + '@luma', 'a': la, 'b': lb, 'tol': 0.2},
        ]
    table['colorgen'] = cg_rows

    # ---- aux outputs — route AUX 0 to a color generator (always present) ----
    # NOTE: aux IS a real output; on a test ATEM that's the point. id = 8001+idx.
    table['aux'] = [
        {'field': 'aux0', 'section': 'aux',
         'set': (lambda c, v: set_aux_output(c, aux=0, source=int(v))),
         'xml': "Auxiliaries/Auxiliary[@id='8001']@input",
         'a': 2001, 'b': 2002, 'tol': 0},
    ]

    # ---- fade to black: rate (M/E cell) + ftbEnabled (Settings flag) ----
    # rate is a frame count; set_ftb_rate parses the bare int string.
    table['ftb'] = [
        {'field': 'ftb.rate', 'section': 'fade_to_black',
         'set': (lambda c, v: set_ftb_rate(c, rate_str=str(int(v)), me=0)),
         'xml': 'MixEffectBlocks/MixEffectBlock/FadeToBlack@rate',
         'a': 37, 'b': 11, 'tol': 0},
        {'field': 'settings.ftbEnabled', 'section': 'settings_flags',
         # value is the desired ftbEnabled bool; set op takes disabled=!enabled.
         'set': (lambda c, v: set_ftb_disabled(c, disabled=(not v))),
         'xml': 'Settings@ftbEnabled',
         'a': False, 'b': True, 'a_xml': 'False', 'b_xml': 'True', 'tol': 0},
    ]

    table['transition'] = _transition_rows()
    table['dsk'] = _dsk_rows()
    table['fairlight'] = _fairlight_rows()
    table['media_players'] = _media_player_rows()
    table['inputs'] = _input_rows()
    table['usk'] = _usk_rows()
    table['hyperdecks'] = _hyperdeck_rows()
    return table


def _hyperdeck_rows():
    # Test slot 1 (leaves the real deck on slot 0 untouched). IP + switcher
    # input round-trip via CXMS (set) -> RXMS (read) -> profile XML. Needs an
    # ATEM with HyperDeck support (e.g. 192.0.2.11).
    base = "HyperDecks/HyperDeck[@id='1']"
    return [
        {'field': 'hd1.networkAddress', 'section': 'hyperdecks',
         'set': (lambda c, v: set_hyperdeck_settings(c, slot=1, network_address=v)),
         'xml': base + '@networkAddress', 'a': '10.20.30.40', 'b': '10.20.30.50',
         'tol': 0},
        {'field': 'hd1.input', 'section': 'hyperdecks',
         'set': (lambda c, v: set_hyperdeck_settings(c, slot=1, switcher_input=int(v))),
         'xml': base + '@input', 'a': 3, 'b': 5, 'tol': 0},
    ]


# --- Increment 3: Upstream keyers -------------------------------------------
# Keyer 0 covered fully across basics/mask/luma/pattern/DVE/fly; keyers 1-3 get
# a light triplet to exercise per-keyer indexing + the no-leak gate. Scales
# (verified against readers): USK+DVE mask edges x1000; luma clip/gain percent
# (IQ-2 area); pattern size/symmetry/softness percent (IQ-4 area), position
# 0..1; DVE border widths percent (the historical 10x area), sat/luma percent,
# hue/opacity identity; fly position/size /1000, rotation /10. NOT covered yet:
# AdvancedChroma (cursor/sampled scale complexity) + DVE light direction/altitude.

def _key_row(k, attr, setfn, a, b, *, axml=None, bxml=None, tol=0.05,
             note=None, sub=None, quirk=None):
    xbase = f"MixEffectBlocks/MixEffectBlock/Keys/Key[@index='{k}']"
    xml = f"{xbase}/{sub}@{attr}" if sub else f"{xbase}@{attr}"
    d = {'field': f'usk{k}.{(sub + ".") if sub else ""}{attr}',
         'section': f'usk_{k + 1}', 'set': setfn, 'xml': xml,
         'a': a, 'b': b, 'tol': tol}
    if axml is not None:
        d['a_xml'], d['b_xml'] = axml, bxml
    if note:
        d['note'] = note
    if quirk:
        d['quirk'] = quirk
    return d


def _key_bool(k, attr, setfn, a, b, sub=None, note=None):
    return _key_row(k, attr, setfn, a, b, axml=str(a), bxml=str(b), tol=0,
                    sub=sub, note=note)


def _keyer_rows(k):
    """Full field coverage for one upstream keyer. Called for k=0..3 — each
    invocation is its own scope so the lambdas capture this call's k. Values
    are the same per keyer (independent keyers, distinct XML paths). A=Chroma
    so the chroma sampler is live. onAir alternates per keyer to stay within
    the simultaneous-on-air limit; fly-enable is keyer-0-only (single DVE)."""
    AC = 'AdvancedChromaParameters'
    on_a, on_b = (False, True) if k % 2 == 0 else (True, False)
    _cur_a, _cur_b = {'x': 0.65, 'y': 0.72}, {'x': 0.30, 'y': 0.45}
    _smp_a, _smp_b = {'y': 0.45, 'cb': 0.62, 'cr': 0.38}, {'y': 0.20, 'cb': 0.40, 'cr': 0.70}

    def _cur(c, v):
        set_usk_chroma_sample_position(c, keyer=k, me=0, x=v['x'], y=v['y'])

    def _smp(c, v):
        set_usk_chroma_sampled_color(c, keyer=k, me=0, y=v['y'], cb=v['cb'], cr=v['cr'])

    rows = [
        # Basics.
        _key_row(k, 'type', lambda c, v: set_usk_type(c, keyer=k, key_type=USK_TYPE_TO_INT[v], me=0), 'Chroma', 'Luma'),
        _key_row(k, 'inputFill', lambda c, v: set_usk_fill_source(c, keyer=k, source=int(v), me=0), 2001, 2002, tol=0),
        _key_row(k, 'inputCut', lambda c, v: set_usk_key_source(c, keyer=k, source=int(v), me=0), 1, 2, tol=0),
        _key_bool(k, 'onAir', lambda c, v: set_usk_on_air(c, keyer=k, enabled=v, me=0), on_a, on_b, note='on-air, alternated per keyer to stay within the simultaneous-on-air limit'),
        _key_bool(k, 'masked', lambda c, v: set_usk_mask_enabled(c, keyer=k, enabled=v, me=0), True, False),
        _key_row(k, 'maskTop', lambda c, v: set_usk_mask_top(c, keyer=k, top=v, me=0), 3.250, 1.125, tol=0.002),
        _key_row(k, 'maskBottom', lambda c, v: set_usk_mask_bottom(c, keyer=k, bottom=v, me=0), 2.500, 4.750, tol=0.002),
        _key_row(k, 'maskLeft', lambda c, v: set_usk_mask_left(c, keyer=k, left=v, me=0), 5.125, 0.875, tol=0.002),
        _key_row(k, 'maskRight', lambda c, v: set_usk_mask_right(c, keyer=k, right=v, me=0), 1.750, 6.250, tol=0.002),
        # Luma (configure_usk_luma — the IQ-2 fixed-scale path).
        _key_row(k, 'clip', lambda c, v: configure_usk_luma(c, keyer=k, clip=v, me=0), 63.5, 22.0, sub='LumaParameters'),
        _key_row(k, 'gain', lambda c, v: configure_usk_luma(c, keyer=k, gain=v, me=0), 71.5, 18.0, sub='LumaParameters'),
        _key_bool(k, 'preMultiplied', lambda c, v: configure_usk_luma(c, keyer=k, pre_multiplied=v, me=0), True, False, sub='LumaParameters'),
        _key_bool(k, 'inverse', lambda c, v: configure_usk_luma(c, keyer=k, invert=v, me=0), True, False, sub='LumaParameters'),
        # Pattern.
        _key_row(k, 'style', lambda c, v: set_usk_pattern_style(c, keyer=k, pattern=WIPE_PATTERN_TO_INT[v], me=0), 'DiamondIris', 'CircleIris', sub='PatternParameters'),
        _key_bool(k, 'inverse', lambda c, v: set_usk_pattern_invert(c, keyer=k, invert=v, me=0), True, False, sub='PatternParameters'),
        _key_row(k, 'size', lambda c, v: set_usk_pattern_size(c, keyer=k, size=v, me=0), 63.5, 21.0, sub='PatternParameters'),
        _key_row(k, 'symmetry', lambda c, v: set_usk_pattern_symmetry(c, keyer=k, symmetry=v, me=0), 58.5, 22.0, sub='PatternParameters'),
        _key_row(k, 'softness', lambda c, v: set_usk_pattern_softness(c, keyer=k, softness=v, me=0), 12.5, 44.0, sub='PatternParameters'),
        _key_row(k, 'xPosition', lambda c, v: set_usk_pattern_position_x(c, keyer=k, position_x=v, me=0), 0.371, 0.628, tol=0.001, sub='PatternParameters'),
        _key_row(k, 'yPosition', lambda c, v: set_usk_pattern_position_y(c, keyer=k, position_y=v, me=0), 0.442, 0.118, tol=0.001, sub='PatternParameters'),
        # DVE border/mask/shadow/light.
        _key_bool(k, 'maskEnabled', lambda c, v: set_usk_dve_masked(c, keyer=k, masked=v, me=0), True, False, sub='DVEParameters'),
        _key_row(k, 'maskTop', lambda c, v: set_usk_dve_top(c, keyer=k, top=v, me=0), 1.250, 3.500, tol=0.002, sub='DVEParameters'),
        _key_row(k, 'maskBottom', lambda c, v: set_usk_dve_bottom(c, keyer=k, bottom=v, me=0), 2.750, 0.500, tol=0.002, sub='DVEParameters'),
        _key_row(k, 'maskLeft', lambda c, v: set_usk_dve_left(c, keyer=k, left=v, me=0), 4.125, 1.875, tol=0.002, sub='DVEParameters'),
        _key_row(k, 'maskRight', lambda c, v: set_usk_dve_right(c, keyer=k, right=v, me=0), 0.750, 5.250, tol=0.002, sub='DVEParameters'),
        _key_bool(k, 'shadowEnabled', lambda c, v: set_usk_dve_shadow(c, keyer=k, shadow=v, me=0), True, False, sub='DVEParameters'),
        _key_bool(k, 'borderEnabled', lambda c, v: set_usk_dve_border_enabled(c, keyer=k, enabled=v, me=0), True, False, sub='DVEParameters'),
        _key_row(k, 'borderBevelHue', lambda c, v: set_usk_dve_border_hue(c, keyer=k, hue=v, me=0), 120.5, 240.0, tol=0.2, sub='DVEParameters'),
        _key_row(k, 'borderBevelSaturation', lambda c, v: set_usk_dve_border_saturation(c, keyer=k, saturation=v, me=0), 63.0, 21.0, tol=0.2, sub='DVEParameters'),
        _key_row(k, 'borderBevelLuma', lambda c, v: set_usk_dve_border_luma(c, keyer=k, luma=v, me=0), 58.0, 12.0, tol=0.2, sub='DVEParameters'),
        _key_row(k, 'borderBevelOpacity', lambda c, v: set_usk_dve_border_opacity(c, keyer=k, opacity=v, me=0), 75.0, 40.0, tol=0.2, sub='DVEParameters'),
        _key_row(k, 'borderWidthOut', lambda c, v: set_usk_dve_border_outer_width(c, keyer=k, outer_width=v, me=0), 2.50, 0.50, tol=0.05, sub='DVEParameters'),
        _key_row(k, 'borderWidthIn', lambda c, v: set_usk_dve_border_inner_width(c, keyer=k, inner_width=v, me=0), 1.50, 3.00, tol=0.05, sub='DVEParameters'),
        _key_row(k, 'lightSourceDirection', lambda c, v: set_usk_dve_light_direction(c, keyer=k, direction=v, me=0), 137.5, 288.0, tol=0.1, sub='DVEParameters'),
        _key_row(k, 'lightSourceAltitude', lambda c, v: set_usk_dve_light_altitude(c, keyer=k, altitude=v, me=0), 45, 78, tol=0, sub='DVEParameters'),
        # Chroma (AdvancedChromaParameters) — key adjust + sampler. fg/bg/edge/
        # spill/flare/saturation save x100 of the 0..1 op input; brightness/
        # contrast/rgb are raw signed (negative A guards a sign-encode bug);
        # cursor=(xy-0.5)*32/18; sampled color 0..1 identity. Cursor set before
        # sampled colour so an auto-resample doesn't clobber it (apply order).
        _key_row(k, 'foregroundLevel', lambda c, v: set_usk_chroma_foreground(c, keyer=k, foreground=v, me=0), 0.617, 0.234, axml=61.7, bxml=23.4, tol=0.1, sub=AC),
        _key_row(k, 'backgroundLevel', lambda c, v: set_usk_chroma_background(c, keyer=k, background=v, me=0), 0.452, 0.789, axml=45.2, bxml=78.9, tol=0.1, sub=AC),
        _key_row(k, 'keyEdge', lambda c, v: set_usk_chroma_key_edge(c, keyer=k, key_edge=v, me=0), 0.288, 0.555, axml=28.8, bxml=55.5, tol=0.1, sub=AC),
        _key_row(k, 'spillSuppress', lambda c, v: set_usk_chroma_spill(c, keyer=k, spill=v, me=0), 0.733, 0.122, axml=73.3, bxml=12.2, tol=0.1, sub=AC),
        _key_row(k, 'flareSuppress', lambda c, v: set_usk_chroma_flare_suppression(c, keyer=k, flare_suppression=v, me=0), 0.171, 0.640, axml=17.1, bxml=64.0, tol=0.1, sub=AC),
        _key_row(k, 'brightness', lambda c, v: set_usk_chroma_brightness(c, keyer=k, brightness=v, me=0), -0.300, 0.450, tol=0.01, sub=AC),
        _key_row(k, 'contrast', lambda c, v: set_usk_chroma_contrast(c, keyer=k, contrast=v, me=0), 0.200, -0.350, tol=0.01, sub=AC),
        _key_row(k, 'saturation', lambda c, v: set_usk_chroma_saturation(c, keyer=k, saturation=v, me=0), 1.350, 0.650, axml=135.0, bxml=65.0, tol=0.2, sub=AC),
        _key_row(k, 'red', lambda c, v: set_usk_chroma_red(c, keyer=k, red=v, me=0), -0.250, 0.400, tol=0.01, sub=AC),
        _key_row(k, 'green', lambda c, v: set_usk_chroma_green(c, keyer=k, green=v, me=0), 0.300, -0.200, tol=0.01, sub=AC),
        _key_row(k, 'blue', lambda c, v: set_usk_chroma_blue(c, keyer=k, blue=v, me=0), -0.150, 0.350, tol=0.01, sub=AC),
        _key_row(k, 'cursorSize', lambda c, v: set_usk_chroma_sample_size(c, keyer=k, size=v, me=0), 0.250, 0.600, axml=25.0, bxml=60.0, tol=0.5, sub=AC),
        _key_row(k, 'cursorXPosition', _cur, _cur_a, _cur_b, axml=4.8, bxml=-6.4, tol=0.05, sub=AC),
        _key_row(k, 'cursorYPosition', _cur, _cur_a, _cur_b, axml=3.96, bxml=-0.9, tol=0.05, sub=AC),
        _key_row(k, 'sampledY', _smp, _smp_a, _smp_b, axml=0.45, bxml=0.20, tol=0.002, sub=AC),
        _key_row(k, 'sampledCb', _smp, _smp_a, _smp_b, axml=0.62, bxml=0.40, tol=0.002, sub=AC),
        _key_row(k, 'sampledCr', _smp, _smp_a, _smp_b, axml=0.38, bxml=0.70, tol=0.002, sub=AC),
        # Fly geometry (reads DVE position/size/rotation/rate; stored per keyer).
        _key_row(k, 'xPosition', lambda c, v: set_usk_dve_position_x(c, keyer=k, position_x=v, me=0), 1.250, -2.500, tol=0.002, sub='FlyParameters'),
        _key_row(k, 'yPosition', lambda c, v: set_usk_dve_position_y(c, keyer=k, position_y=v, me=0), -0.750, 3.250, tol=0.002, sub='FlyParameters'),
        _key_row(k, 'xSize', lambda c, v: set_usk_dve_size_x(c, keyer=k, size_x=v, me=0), 0.750, 1.250, tol=0.002, sub='FlyParameters'),
        _key_row(k, 'ySize', lambda c, v: set_usk_dve_size_y(c, keyer=k, size_y=v, me=0), 0.500, 1.500, tol=0.002, sub='FlyParameters'),
        _key_row(k, 'rotation', lambda c, v: set_usk_dve_rotation(c, keyer=k, rotation=v, me=0), 37.5, -22.0, tol=0.05, sub='FlyParameters',
                 quirk='fly rotation stays 0 unless the keyer is a DVE-type key '
                       '(A=Chroma here). Scale (/10) is correct; ATEM constraint, '
                       'not a save/restore bug.'),
        _key_row(k, 'rate', lambda c, v: set_usk_dve_rate(c, keyer=k, rate_str=str(int(v)), me=0), 33, 9, tol=0, sub='FlyParameters'),
    ]
    # Fly enable is keyer-0-only — only one keyer can own the DVE/fly channel.
    if k == 0:
        rows.append(_key_bool(0, 'enabled', lambda c, v: set_usk_fly_enabled(c, keyer=0, enabled=v, me=0), True, False, sub='FlyParameters'))
    return rows


def _usk_rows():
    rows = []
    for k in range(4):
        rows += _keyer_rows(k)
    return rows


def _media_player_rows():
    rows = []
    for mp in (0, 1):
        base = f"MediaPlayers/MediaPlayer[@index='{mp}']"
        rows.append({
            'field': f'mp{mp}.sourceIndex', 'section': 'media_players',
            'set': (lambda c, v, p=mp: set_media_player_still(c, player=p, slot=int(v))),
            'xml': base + '@sourceIndex', 'a': 0, 'b': 1, 'tol': 0})
    return rows


def _input_rows():
    # Rename input source 1 (long<=20 bytes, short<=4). Identity strings.
    base = "Settings/Inputs/Input[@id='1']"
    return [
        {'field': 'input1.longName', 'section': 'inputs',
         'set': (lambda c, v: set_input_label(c, source=1, long_name=v)),
         'xml': base + '@longName', 'a': 'RoundtripLongA', 'b': 'RoundtripLongB',
         'tol': 0},
        {'field': 'input1.shortName', 'section': 'inputs',
         'set': (lambda c, v: set_input_label(c, source=1, short_name=v)),
         'xml': base + '@shortName', 'a': 'RTA', 'b': 'RTB', 'tol': 0},
    ]


def _R(field, xml, setfn, a, b, *, axml=None, bxml=None, tol=0.05, note=None):
    """Compact value-table row builder for fairlight (section 'audio')."""
    d = {'field': field, 'section': 'audio', 'set': setfn, 'xml': xml,
         'a': a, 'b': b, 'tol': tol}
    if axml is not None:
        d['a_xml'], d['b_xml'] = axml, bxml
    if note:
        d['note'] = note
    return d


def _bool_row(field, xml, setfn, a, b):
    return _R(field, xml, setfn, a, b, axml=str(a), bxml=str(b), tol=0)


# --- Increment 2 -----------------------------------------------------------
# Reader/setter pairs are inverse, so per-style params round-trip identity in
# operator units: rates=frames, clip/gain=percent (wire x10), wipe symmetry/
# softness/width=percent (wire x100), wipe position=0..1 (wire x10000), DSK
# masks=value (wire x1000). enums (style/pattern/effect) are symbolic names.

_TS = 'MixEffectBlocks/MixEffectBlock/TransitionStyle'


def _transition_rows():
    rows = [
        # Active transition style (Mix/Dip/Wipe/DVE/Stinger).
        {'field': 'tx.style', 'section': 'transition_style',
         'set': (lambda c, v: set_transition_style(c, style=TRANS_STYLE_TO_INT[v], me=0)),
         'xml': _TS + '@style', 'a': 'Wipe', 'b': 'DVE'},
        {'field': 'tx.mix.rate', 'section': 'transition_style',
         'set': (lambda c, v: set_mix_rate(c, rate_str=str(int(v)), me=0)),
         'xml': _TS + '/MixParameters@rate', 'a': 37, 'b': 11, 'tol': 0},
        {'field': 'tx.dip.rate', 'section': 'transition_style',
         'set': (lambda c, v: set_dip_rate(c, rate_str=str(int(v)), me=0)),
         'xml': _TS + '/DipParameters@rate', 'a': 44, 'b': 8, 'tol': 0},
        {'field': 'tx.dip.source', 'section': 'transition_style',
         'set': (lambda c, v: set_dip_source(c, source=int(v), me=0)),
         'xml': _TS + '/DipParameters@input', 'a': 2001, 'b': 2002, 'tol': 0},
    ]
    # Wipe.
    wp = _TS + '/WipeParameters'
    rows += [
        {'field': 'tx.wipe.rate', 'section': 'transition_style',
         'set': (lambda c, v: set_wipe_rate(c, rate_str=str(int(v)), me=0)),
         'xml': wp + '@rate', 'a': 51, 'b': 9, 'tol': 0},
        {'field': 'tx.wipe.pattern', 'section': 'transition_style',
         'set': (lambda c, v: set_wipe_pattern(c, pattern=WIPE_PATTERN_TO_INT[v], me=0)),
         'xml': wp + '@pattern', 'a': 'DiamondIris', 'b': 'CircleIris'},
        {'field': 'tx.wipe.symmetry', 'section': 'transition_style',
         'set': (lambda c, v: set_wipe_symmetry(c, symmetry=v, me=0)),
         'xml': wp + '@symmetry', 'a': 63.5, 'b': 21.0,
         'quirk': 'round-trips in isolation (set/save/restore all correct) but '
                  'will not reliably re-scramble to B inside the full transition '
                  'batch — a subtle ATEM multi-field state-coupling, not a '
                  'save/restore bug. Excluded so signal stays clean.'},
        {'field': 'tx.wipe.softness', 'section': 'transition_style',
         'set': (lambda c, v: set_wipe_softness(c, softness=v, me=0)),
         'xml': wp + '@borderSoftness', 'a': 12.5, 'b': 47.0},
        {'field': 'tx.wipe.width', 'section': 'transition_style',
         'set': (lambda c, v: set_wipe_width(c, width=v, me=0)),
         'xml': wp + '@borderWidth', 'a': 8.5, 'b': 33.0},
        {'field': 'tx.wipe.xPosition', 'section': 'transition_style',
         'set': (lambda c, v: set_wipe_position_x(c, position_x=v, me=0)),
         'xml': wp + '@xPosition', 'a': 0.371, 'b': 0.628, 'tol': 0.001},
        {'field': 'tx.wipe.yPosition', 'section': 'transition_style',
         'set': (lambda c, v: set_wipe_position_y(c, position_y=v, me=0)),
         'xml': wp + '@yPosition', 'a': 0.442, 'b': 0.118, 'tol': 0.001},
        {'field': 'tx.wipe.reverse', 'section': 'transition_style',
         'set': (lambda c, v: set_wipe_reverse(c, reverse=v, me=0)),
         'xml': wp + '@reverseDirection', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
        {'field': 'tx.wipe.flipFlop', 'section': 'transition_style',
         'set': (lambda c, v: set_wipe_flip_flop(c, flip_flop=v, me=0)),
         'xml': wp + '@flipFlip', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
        {'field': 'tx.wipe.borderInput', 'section': 'transition_style',
         'set': (lambda c, v: set_wipe_fill_source(c, source=int(v), me=0)),
         'xml': wp + '@borderInput', 'a': 2001, 'b': 2002, 'tol': 0},
    ]
    # Stinger.
    sp = _TS + '/StingerParameters'
    rows += [
        {'field': 'tx.stinger.source', 'section': 'transition_style',
         # this ATEM has 2 media players; MediaPlayer3+ are rejected.
         'set': (lambda c, v: set_stinger_source(c, source=int(v), me=0)),
         'xml': sp + '@source', 'a': 1, 'b': 2,
         'a_xml': 'MediaPlayer1', 'b_xml': 'MediaPlayer2',
         'note': 'IQ-5: u8 1..4 vs reader default 3010 — asserting it round-trips in-range'},
        # Stinger clip/gain/preMultiplied are the SAME physical transition-key
        # state as DVE-tx clip/gain/preMultiplied on this hardware (verified:
        # setting one moves both). Use the DVE values so both XML elements
        # reflect the shared state and the assertion is meaningful.
        {'field': 'tx.stinger.clip', 'section': 'transition_style',
         'set': (lambda c, v: set_stinger_clip(c, clip=v, me=0)),
         'xml': sp + '@clip', 'a': 58.5, 'b': 24.0,
         'note': 'shared transition-key clip with DVE-tx on hardware'},
        {'field': 'tx.stinger.gain', 'section': 'transition_style',
         'set': (lambda c, v: set_stinger_gain(c, gain=v, me=0)),
         'xml': sp + '@gain', 'a': 66.5, 'b': 31.0,
         'note': 'shared transition-key gain with DVE-tx on hardware'},
        {'field': 'tx.stinger.invert', 'section': 'transition_style',
         'set': (lambda c, v: set_stinger_invert_key(c, invert_key=v, me=0)),
         'xml': sp + '@invert', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
        {'field': 'tx.stinger.preMultiplied', 'section': 'transition_style',
         'set': (lambda c, v: set_stinger_pre_multiplied(c, pre_multiplied=v, me=0)),
         'xml': sp + '@preMultipliedKey', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False',
         'note': 'shared transition-key preMultiply with DVE-tx on hardware'},
        {'field': 'tx.stinger.clipDuration', 'section': 'transition_style',
         'set': (lambda c, v: set_stinger_clip_duration(c, clip_duration=str(int(v)), me=0)),
         'xml': sp + '@clipDuration', 'a': 88, 'b': 120, 'tol': 0},
        {'field': 'tx.stinger.triggerPoint', 'section': 'transition_style',
         'set': (lambda c, v: set_stinger_trigger_point(c, trigger_point=str(int(v)), me=0)),
         'xml': sp + '@triggerPoint', 'a': 22, 'b': 51, 'tol': 0},
        {'field': 'tx.stinger.mixRate', 'section': 'transition_style',
         'set': (lambda c, v: set_stinger_mix_rate(c, rate_str=str(int(v)), me=0)),
         'xml': sp + '@mixRate', 'a': 9, 'b': 17, 'tol': 0},
        {'field': 'tx.stinger.preroll', 'section': 'transition_style',
         'set': (lambda c, v: set_stinger_pre_roll(c, pre_roll=str(int(v)), me=0)),
         'xml': sp + '@preroll', 'a': 12, 'b': 40, 'tol': 0},
    ]
    # DVE transition.
    dp = _TS + '/DVEParameters'
    rows += [
        {'field': 'tx.dve.rate', 'section': 'transition_style',
         'set': (lambda c, v: set_dve_rate(c, rate_str=str(int(v)), me=0)),
         'xml': dp + '@rate', 'a': 33, 'b': 7, 'tol': 0},
        {'field': 'tx.dve.effect', 'section': 'transition_style',
         'set': (lambda c, v: set_dve_style(c, style=DVE_EFFECT_TO_INT[v], me=0)),
         'xml': dp + '@effect', 'a': 'PushRight', 'b': 'SqueezeBottom'},
        {'field': 'tx.dve.fillSource', 'section': 'transition_style',
         'set': (lambda c, v: set_dve_fill_source(c, source=int(v), me=0)),
         'xml': dp + '@fillSource', 'a': 2001, 'b': 2002, 'tol': 0},
        {'field': 'tx.dve.keySource', 'section': 'transition_style',
         # key/cut source: color gens (2001/2002) are rejected as a key
         # source; use physical inputs.
         'set': (lambda c, v: set_dve_key_source(c, source=int(v), me=0)),
         'xml': dp + '@keySource', 'a': 1, 'b': 2, 'tol': 0},
        {'field': 'tx.dve.enableKey', 'section': 'transition_style',
         'set': (lambda c, v: set_dve_enable_key(c, enable_key=v, me=0)),
         'xml': dp + '@enableKey', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
        {'field': 'tx.dve.clip', 'section': 'transition_style',
         'set': (lambda c, v: set_dve_clip(c, clip=v, me=0)),
         'xml': dp + '@clip', 'a': 58.5, 'b': 24.0},
        {'field': 'tx.dve.gain', 'section': 'transition_style',
         'set': (lambda c, v: set_dve_gain(c, gain=v, me=0)),
         'xml': dp + '@gain', 'a': 66.5, 'b': 31.0},
        {'field': 'tx.dve.invertKey', 'section': 'transition_style',
         'set': (lambda c, v: set_dve_invert_key(c, invert_key=v, me=0)),
         'xml': dp + '@invertKey', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
        {'field': 'tx.dve.preMultiplied', 'section': 'transition_style',
         'set': (lambda c, v: set_dve_pre_multiplied(c, pre_multiplied=v, me=0)),
         'xml': dp + '@preMultipliedKey', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
        {'field': 'tx.dve.reverse', 'section': 'transition_style',
         'set': (lambda c, v: set_dve_reverse(c, reverse=v, me=0)),
         'xml': dp + '@reverseDirection', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
        {'field': 'tx.dve.flipFlop', 'section': 'transition_style',
         'set': (lambda c, v: set_dve_flip_flop(c, flip_flop=v, me=0)),
         'xml': dp + '@flipFlop', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
    ]
    # Next-transition selection mask (own section).
    rows += [
        # key1/3/4 add to the next-transition fine; key2 is rejected by this
        # ATEM (DVE/keyer-resource constraint — mask packing verified correct,
        # so not a save/restore bug). Use background-only vs +key1.
        {'field': 'tx.next_transition', 'section': 'next_transition',
         'set': (lambda c, v: set_next_transition_layers(c, me=0, **v)),
         'xml': 'MixEffectBlocks/MixEffectBlock/NextTransition@selection',
         'a': {'background': True},
         'b': {'background': True, 'key1': True},
         'a_xml': 'Background', 'b_xml': 'Background,Key1',
         'quirk': 'adding a keyer to the next-transition round-trips in isolation '
                  '(mask packing verified, save/restore correct) but will not '
                  'reliably take inside the full transition batch — same ATEM '
                  'state-coupling as tx.wipe.symmetry. Not a save/restore bug.'},
    ]
    return rows


def _dsk_rows():
    base = "DownstreamKeys/DownstreamKey[@index='0']"
    return [
        {'field': 'dsk0.fillSource', 'section': 'downstream_keys',
         'set': (lambda c, v: set_dsk_fill_source(c, source=int(v), dsk_idx=0)),
         'xml': base + '@fillSource', 'a': 2001, 'b': 2002, 'tol': 0},
        {'field': 'dsk0.keySource', 'section': 'downstream_keys',
         # key source: color gens rejected; use physical inputs.
         'set': (lambda c, v: set_dsk_key_source(c, source=int(v), dsk_idx=0)),
         'xml': base + '@keySource', 'a': 1, 'b': 2, 'tol': 0},
        {'field': 'dsk0.rate', 'section': 'downstream_keys',
         'set': (lambda c, v: set_dsk_rate(c, rate_str=str(int(v)), dsk_idx=0)),
         'xml': base + '@rate', 'a': 44, 'b': 12, 'tol': 0},
        {'field': 'dsk0.maskEnabled', 'section': 'downstream_keys',
         'set': (lambda c, v: set_dsk_mask_enabled(c, enabled=v, dsk_idx=0)),
         'xml': base + '@maskEnabled', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
        {'field': 'dsk0.maskTop', 'section': 'downstream_keys',
         'set': (lambda c, v: set_dsk_mask_top(c, top=v, dsk_idx=0)),
         'xml': base + '@maskTop', 'a': 3.250, 'b': 1.125, 'tol': 0.002},
        {'field': 'dsk0.maskBottom', 'section': 'downstream_keys',
         'set': (lambda c, v: set_dsk_mask_bottom(c, bottom=v, dsk_idx=0)),
         'xml': base + '@maskBottom', 'a': 2.500, 'b': 4.750, 'tol': 0.002},
        {'field': 'dsk0.maskLeft', 'section': 'downstream_keys',
         'set': (lambda c, v: set_dsk_mask_left(c, left=v, dsk_idx=0)),
         'xml': base + '@maskLeft', 'a': 5.125, 'b': 0.875, 'tol': 0.002},
        {'field': 'dsk0.maskRight', 'section': 'downstream_keys',
         'set': (lambda c, v: set_dsk_mask_right(c, right=v, dsk_idx=0)),
         'xml': base + '@maskRight', 'a': 1.750, 'b': 6.250, 'tol': 0.002},
        {'field': 'dsk0.clip', 'section': 'downstream_keys',
         'set': (lambda c, v: configure_dsk_gain(c, clip=v, dsk_idx=0)),
         'xml': base + '@clip', 'a': 63.5, 'b': 22.0},
        {'field': 'dsk0.gain', 'section': 'downstream_keys',
         'set': (lambda c, v: configure_dsk_gain(c, gain=v, dsk_idx=0)),
         'xml': base + '@gain', 'a': 71.5, 'b': 18.0},
        {'field': 'dsk0.preMultiplied', 'section': 'downstream_keys',
         'set': (lambda c, v: configure_dsk_gain(c, pre_multiplied=v, dsk_idx=0)),
         'xml': base + '@preMultipliedKey', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
        {'field': 'dsk0.invert', 'section': 'downstream_keys',
         'set': (lambda c, v: configure_dsk_gain(c, invert=v, dsk_idx=0)),
         'xml': base + '@invert', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
        {'field': 'dsk0.tie', 'section': 'downstream_keys',
         'set': (lambda c, v: set_dsk_tie(c, tie=v, dsk_idx=0)),
         'xml': base + '@tie', 'a': True, 'b': False,
         'a_xml': 'True', 'b_xml': 'False'},
        {'field': 'dsk0.onAir', 'section': 'downstream_keys',
         'set': (lambda c, v: set_dsk_on_air(c, enabled=v, dsk_idx=0)),
         'xml': base + '@onAir', 'a': False, 'b': True,
         'a_xml': 'False', 'b_xml': 'True',
         'note': 'on-air: puts DSK on program; test ATEM only'},
    ]


# --- Increment 4: Fairlight (the master-EQ-gain motivation) -----------------
# All dB/ms/ratio fields round-trip identity (set op takes dB/ms/ratio, save
# reader returns the same; wire is x100). EQ-band/EQ gains are i32 on the wire
# — A uses a NEGATIVE value so a
# sign-extension/clamp regression (the master-EQ-gain bug) is exposed.

_FA = 'FairlightAudioMixer'
_STRIP1 = _FA + "/AudioInputs/AudioInput[@id='1']/AudioSource"


def _comp_rows(prefix, xmlbase, comp_set, tnote=None):
    """Compressor rows (enabled/threshold/ratio/attack/hold/release)."""
    return [
        _bool_row(f'{prefix}.enabled', xmlbase + '@enabled',
                  lambda c, v: comp_set(c, enabled=v), True, False),
        _R(f'{prefix}.threshold', xmlbase + '@threshold',
           lambda c, v: comp_set(c, threshold_db=v), -22.50, -8.00, note=tnote),
        _R(f'{prefix}.ratio', xmlbase + '@ratio',
           lambda c, v: comp_set(c, ratio=v), 3.50, 6.00),
        _R(f'{prefix}.attack', xmlbase + '@attack',
           lambda c, v: comp_set(c, attack_ms=v), 12.40, 4.70),
        _R(f'{prefix}.hold', xmlbase + '@hold',
           lambda c, v: comp_set(c, hold_ms=v), 15, 40, tol=0.5),
        _R(f'{prefix}.release', xmlbase + '@release',
           lambda c, v: comp_set(c, release_ms=v), 180, 320, tol=0.5),
    ]


def _lim_rows(prefix, xmlbase, lim_set, tnote=None):
    """Limiter rows (no ratio)."""
    return [
        _bool_row(f'{prefix}.enabled', xmlbase + '@enabled',
                  lambda c, v: lim_set(c, enabled=v), True, False),
        _R(f'{prefix}.threshold', xmlbase + '@threshold',
           lambda c, v: lim_set(c, threshold_db=v), -9.50, -3.25, note=tnote),
        _R(f'{prefix}.attack', xmlbase + '@attack',
           lambda c, v: lim_set(c, attack_ms=v), 2.40, 0.90),
        _R(f'{prefix}.hold', xmlbase + '@hold',
           lambda c, v: lim_set(c, hold_ms=v), 12, 35, tol=0.5),
        _R(f'{prefix}.release', xmlbase + '@release',
           lambda c, v: lim_set(c, release_ms=v), 140, 260, tol=0.5),
    ]


def _eq_band_rows(prefix, xmlbase, band_set):
    """EQ band rows. gain is i32 — A negative to expose sign-ext regressions."""
    return [
        _bool_row(f'{prefix}.enabled', xmlbase + '@enabled',
                  lambda c, v: band_set(c, enabled=v), True, False),
        _R(f'{prefix}.shape', xmlbase + '@shape',
           lambda c, v: band_set(c, shape=v), 'BandPass', 'Notch'),
        _R(f'{prefix}.range', xmlbase + '@frequencyRange',
           lambda c, v: band_set(c, frequency_range=v), 'MidLow', 'MidHigh'),
        _R(f'{prefix}.frequency', xmlbase + '@frequency',
           lambda c, v: band_set(c, frequency_hz=int(v)), 173, 820, tol=0),
        _R(f'{prefix}.gain', xmlbase + '@gain',
           lambda c, v: band_set(c, gain_db=v), -6.25, 3.50),
        _R(f'{prefix}.qFactor', xmlbase + '@qFactor',
           lambda c, v: band_set(c, q=v), 2.50, 0.71),
    ]


def _fairlight_rows():
    rows = []

    # ---- Master out ----
    rows += [
        _R('master.fader', _FA + '@masterOutFaderGain',
           lambda c, v: set_fairlight_master(c, volume_db=v), -7.85, -3.20),
        _bool_row('master.afv', _FA + '@followFadeToBlack',
                  lambda c, v: set_fairlight_master(c, afv=v), True, False),
        _bool_row('master.eq.enabled', _FA + '/MasterOutEqualizer@enabled',
                  lambda c, v: set_fairlight_master(c, eq_enable=v), True, False),
        # *** the regression guard: master EQ gain is a sign-extended i32;
        #     a NEGATIVE A would read as clamped +20 if the bug returned ***
        _R('master.eq.gain', _FA + '/MasterOutEqualizer@gain',
           lambda c, v: set_fairlight_master(c, eq_gain_db=v), -7.25, 4.50,
           note='master EQ gain i32 sign-ext regression guard (negative A)'),
        _R('master.dyn.makeup', _FA + '/MasterOutDynamicsProcessor@makeupGain',
           lambda c, v: set_fairlight_master(c, dynamics_makeup_db=v), 3.75, 8.20),
    ]
    # master comp/limiter THRESHOLD was the harness's first catch: a negative dB
    # clamped to 0 because CMCP/CMLP packed threshold s16@6 instead of the
    # sign-extended i32@4. FIXED 2026-06-09
    # (CMCP '>B B 2x i H 2x i i i', CMLP '>B B 2x i i i i'); now round-trips.
    # Kept here as a permanent regression guard (negative A exposes any relapse).
    _ITHR = 'i32@4 fix 2026-06-09 (was s16@6 clamp-to-0); negative A guards relapse'
    rows += _comp_rows('master.comp',
                       _FA + '/MasterOutDynamicsProcessor/Compressor',
                       set_fairlight_master_compressor, tnote=_ITHR)
    rows += _lim_rows('master.lim',
                      _FA + '/MasterOutDynamicsProcessor/Limiter',
                      set_fairlight_master_limiter, tnote=_ITHR)
    rows += _eq_band_rows(
        'master.eq.band2',
        _FA + "/MasterOutEqualizer/EqualizerBand[@index='2']",
        lambda c, **kw: set_fairlight_master_eq_band(c, band=2, **kw))

    # ---- Per-strip (source 1, stereo channel -1) ----
    def strip(c, **kw):
        set_fairlight_strip(c, source=1, channel=-1, **kw)
    rows += [
        _R('strip1.inputGain', _STRIP1 + '@inputGain',
           lambda c, v: strip(c, input_gain_db=v), -4.50, 5.50),  # max +6.0
        _R('strip1.pan', _STRIP1 + '@pan',
           lambda c, v: strip(c, pan=v), -37.0, 62.0, tol=0.2),
        _R('strip1.faderGain', _STRIP1 + '@faderGain',
           lambda c, v: strip(c, fader_gain_db=v), -12.30, -2.50),
        _R('strip1.mixOption', _STRIP1 + '@mixOption',
           lambda c, v: strip(c, mix_option=v), 'On', 'AudioFollowVideo'),
        _bool_row('strip1.eq.enabled', _STRIP1 + '/Equalizer@enabled',
                  lambda c, v: strip(c, eq_enable=v), True, False),
        _R('strip1.eq.gain', _STRIP1 + '/Equalizer@gain',
           lambda c, v: strip(c, eq_gain_db=v), -5.75, 4.25,
           note='per-strip EQ gain i32 (negative A)'),
        _R('strip1.dyn.makeup', _STRIP1 + '/DynamicsProcessor@makeupGain',
           lambda c, v: strip(c, dynamics_makeup_db=v), 2.50, 9.00),
    ]
    rows += _comp_rows('strip1.comp',
                       _STRIP1 + '/DynamicsProcessor/Compressor',
                       lambda c, **kw: set_fairlight_compressor(c, source=1, channel=-1, **kw))
    rows += _lim_rows('strip1.lim',
                      _STRIP1 + '/DynamicsProcessor/Limiter',
                      lambda c, **kw: set_fairlight_limiter(c, source=1, channel=-1, **kw))
    # Expander: keep mode='expander' for both A/B (gate mode couples ratio);
    # scramble the numeric fields.
    exp = _STRIP1 + '/DynamicsProcessor/Expander'

    def exp_set(c, **kw):
        set_fairlight_expander(c, source=1, channel=-1, mode='expander', **kw)
    rows += [
        _bool_row('strip1.exp.enabled', exp + '@enabled',
                  lambda c, v: exp_set(c, enabled=v), True, False),
        _R('strip1.exp.threshold', exp + '@threshold',
           lambda c, v: exp_set(c, threshold_db=v), -33.50, -12.00),
        _R('strip1.exp.range', exp + '@range',
           lambda c, v: exp_set(c, range_db=v), 24.0, 9.0, tol=0.2),
        _R('strip1.exp.ratio', exp + '@ratio',
           lambda c, v: exp_set(c, ratio=v), 1.40, 2.60),
        _R('strip1.exp.attack', exp + '@attack',
           lambda c, v: exp_set(c, attack_ms=v), 3.40, 1.10),
        _R('strip1.exp.hold', exp + '@hold',
           lambda c, v: exp_set(c, hold_ms=v), 18, 44, tol=0.5),
        _R('strip1.exp.release', exp + '@release',
           lambda c, v: exp_set(c, release_ms=v), 160, 300, tol=0.5),
    ]
    rows += _eq_band_rows(
        'strip1.eq.band2', _STRIP1 + "/Equalizer/EqualizerBand[@index='2']",
        lambda c, **kw: set_fairlight_eq_band(c, source=1, channel=-1, band=2, **kw))

    return rows


VALUE_TABLE = _build_value_table()


# =============================================================================
# Pure helpers (no hardware) — exercised by --dry-run
# =============================================================================

def xml_get(root: ET.Element, path_attr: str):
    """Resolve "element/path@attribute" against ``root``. Returns the
    attribute string, or None if the element or attribute is absent."""
    # rpartition (not partition): predicates like [@index='0'] contain '@';
    # the attribute is always after the FINAL '@'.
    elem_path, _, attr = path_attr.rpartition('@')
    elem_path = elem_path.rstrip('/')
    node = root if elem_path in ('', '.') else root.find(elem_path)
    if node is None:
        return None
    return node.get(attr)


def oracle(row: dict, which: str):
    """The independent expectation for how value A/B appears in the saved XML.
    Author-encoded — never derived from the save code."""
    base = row[which]
    explicit = row.get(f'{which}_xml')
    if explicit is not None:
        return explicit
    if 'save_scale' in row:
        return float(base) * row['save_scale']
    return base


def _as_float(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def compare(expected, got, tol: float) -> bool:
    """Numeric-aware equality: floats within ``tol``, else case-insensitive
    string compare (handles enums, True/False, -inf)."""
    if got is None:
        return False
    ef, gf = _as_float(expected), _as_float(got)
    if ef is not None and gf is not None:
        return abs(ef - gf) <= (tol or 0.0)
    return str(expected).strip().lower() == str(got).strip().lower()


def rows_for(features):
    """Flatten the value table to the selected feature list (or all)."""
    names = features if features else list(VALUE_TABLE)
    out = []
    for name in names:
        if name not in VALUE_TABLE:
            raise SystemExit(f"unknown --features entry: {name!r} "
                             f"(known: {', '.join(VALUE_TABLE)})")
        out.extend(VALUE_TABLE[name])
    return out


def asserted_rows(rows):
    """Rows that participate in value assertions (quirk rows are excluded)."""
    return [r for r in rows if not r.get('quirk')]


# =============================================================================
# Option builders
# =============================================================================

def _all_bools_false(opts) -> None:
    """Flip every top-level bool flag off and fully deselect every
    per-M/E block (the harness only ever exercises M/E 0 on .85)."""
    for f in vars(opts):
        cur = getattr(opts, f)
        if isinstance(cur, bool):
            setattr(opts, f, False)
    opts.mes = [MixEffectOptions.none() for _ in opts.mes]


def _save_options_all_false() -> SaveOptions:
    opts = SaveOptions()
    _all_bools_false(opts)
    return opts


def _apply_options_all_false() -> ApplyOptions:
    opts = ApplyOptions()
    _all_bools_false(opts)
    return opts


def _usk_index(section_key):
    if section_key.startswith('usk_'):
        return int(section_key.split('_')[1]) - 1
    return None


def save_options_only(section_key: str) -> SaveOptions:
    opts = _save_options_all_false()
    i = _usk_index(section_key)
    section = SECTIONS.get(section_key, {})
    if i is not None:
        opts.mes[0].usk[i] = True
    elif section.get('per_me'):
        setattr(opts.mes[0], section['save'], True)
    else:
        setattr(opts, section['save'], True)
    return opts


def apply_options_only(section_key: str) -> ApplyOptions:
    opts = _apply_options_all_false()
    i = _usk_index(section_key)
    section = SECTIONS.get(section_key, {})
    if i is not None:
        opts.mes[0].usk[i] = True
    elif section.get('per_me'):
        setattr(opts.mes[0], section['apply'], True)
    else:
        setattr(opts, section['apply'], True)
    return opts


# =============================================================================
# Connection helpers + converge-with-retry core
#
# A busy link drops reliable packets under burst (worse over a VPN). A
# dropped set/restore command would falsely
# read as "didn't round-trip". The converge loop separates the two: retry the
# push until the SAVED XML matches the target, up to MAX_TRIES. A field that
# converges after a retry was a transient drop (fine). A field that NEVER
# converges is a real finding — a save-read bug, a send-encode bug (clamp/
# sign-ext like master-EQ-gain), or a restore gap. The oracle stays the
# independent author-encoded expectation throughout, so this is not circular.
# =============================================================================

MAX_TRIES = 5

# Per-send pacing. Firing N set-commands back-to-back drains them through the
# worker at its 10ms BURST_SEND_SPACING — ~35 commands hit the ATEM inside
# ~350ms and it drops some (verified: paced single sends land 30/30, bursts
# lose ~scattered fields). Pacing each send keeps the ATEM un-bursted.
SEND_SPACING = 0.05


def _fast_save_options() -> SaveOptions:
    """Save every section the value table reads, but skip macros (slow
    file-transfer download that also contends for the lossy link) and media
    images (binary). The round-trip harness never asserts those."""
    return SaveOptions(macros=False, media_pool_images=False)


def save_profile(conn, save_opts=None) -> Profile:
    return Profile.from_atem(conn, save_opts or _fast_save_options())


def _push_rows(conn, rows, which):
    """Call each row's set-op for value A/B, paced by SEND_SPACING so a batch
    doesn't burst-overflow the ATEM. Returns (field, exc) failures."""
    errs = []
    for r in rows:
        try:
            r['set'](conn, r[which])
        except Exception as exc:  # noqa: BLE001
            errs.append((r['field'], exc))
        time.sleep(SEND_SPACING)
    return errs


def _unfit(root, check_rows, target):
    """check_rows whose saved XML doesn't match target(row). target(row)
    returns the expected XML string for that row."""
    bad = []
    for r in check_rows:
        if not compare(target(r), xml_get(root, r['xml']), r.get('tol', 0.05)):
            bad.append(r)
    return bad


def converge_set(conn, send_rows, check_rows, which, settle, tag):
    """Push value ``which`` for every send_row, then retry only the rows that
    didn't land (per the saved XML) until all check_rows match or MAX_TRIES.
    Returns (profile, unfit_rows, set_errors). unfit = never-converged."""
    set_errors = _push_rows(conn, send_rows, which)
    time.sleep(settle)
    prof = save_profile(conn)
    unfit = _unfit(prof.root, check_rows, lambda r: oracle(r, which))
    other = 'a' if which == 'b' else 'b'
    for attempt in range(2, MAX_TRIES + 1):
        if not unfit:
            break
        print(f'      {tag}: retry {attempt} for {len(unfit)} unsettled '
              f'field(s): {", ".join(r["field"] for r in unfit)}')
        # Nudge: set OPPOSITE then target (both paced). If attempt 1's echo for
        # this field dropped, the ATEM already holds the target value, so a
        # plain re-send is a no-op and emits no fresh echo — mixerstate stays
        # stale forever. Forcing a real change (opposite -> target) guarantees
        # a new echo. The unfit set is small here, so the paced nudge can't
        # burst the ATEM.
        _push_rows(conn, unfit, other)
        time.sleep(0.3)
        _push_rows(conn, unfit, which)
        time.sleep(settle)
        prof = save_profile(conn)
        unfit = _unfit(prof.root, check_rows, lambda r: oracle(r, which))
    return prof, unfit, set_errors


def converge_restore(conn, profile_target, rows, target_root, settle, tag):
    """Restore ``profile_target`` section-by-section, then retry the sections
    whose fields didn't land vs ``target_root`` (the saved-A XML) until all
    match or MAX_TRIES. Returns (profile_after, unfit_rows)."""
    chk = asserted_rows(rows)
    target = lambda r: xml_get(target_root, r['xml'])
    sections = _sections_in(rows, None)
    prof = None
    unfit = chk
    for attempt in range(1, MAX_TRIES + 1):
        secs = sections if attempt == 1 else sorted({r['section'] for r in unfit})
        for s in secs:
            res = profile_target.apply(conn, apply_options_only(s))
            for e in res.errors:
                print(f'        ! [{tag}:{s}] {e}')
            time.sleep(min(settle, 1.0))
        time.sleep(settle)
        prof = save_profile(conn)
        unfit = _unfit(prof.root, chk, target)
        if not unfit:
            break
        if attempt < MAX_TRIES:
            print(f'      {tag}: retry {attempt + 1} for {len(unfit)} field(s): '
                  f'{", ".join(r["field"] for r in unfit)}')
    return prof, unfit


# =============================================================================
# Reporting
# =============================================================================

class Findings:
    def __init__(self):
        self.items = []   # (guarantee, field, expected, got, xml_path, note)
        self.set_errors = []  # (phase, field, exc)

    def add(self, guarantee, field, expected, got, xml_path, note=None):
        self.items.append((guarantee, field, str(expected), str(got),
                           xml_path, note))

    def add_set_errors(self, phase, errs):
        for field, exc in errs:
            self.set_errors.append((phase, field, f'{type(exc).__name__}: {exc}'))

    def report(self) -> int:
        print('\n' + '=' * 72)
        print('ROUND-TRIP SMOKE RESULTS')
        print('=' * 72)
        if self.set_errors:
            print(f'\nSET ERRORS ({len(self.set_errors)}) — a set-op raised; '
                  'that field could not be exercised:')
            for phase, field, msg in self.set_errors:
                print(f'  ! [{phase}] {field}: {msg}')
        if not self.items:
            print('\nPASS — every asserted field round-tripped.')
            return 0 if not self.set_errors else 1
        by_g = {}
        for g, field, exp, got, path, note in self.items:
            by_g.setdefault(g, []).append((field, exp, got, path, note))
        for g in sorted(by_g):
            rows = by_g[g]
            print(f'\n{g}  ({len(rows)} mismatch{"es" if len(rows) != 1 else ""}):')
            for field, exp, got, path, note in rows:
                print(f'  FAIL {field}')
                print(f'        expected: {exp!r}   got: {got!r}')
                print(f'        at: {path}')
                if note:
                    print(f'        note: {note}')
        print(f'\nFAIL — {len(self.items)} field(s) did not round-trip.')
        return 1


# =============================================================================
# Mode: roundtrip (guarantees 1 & 2)
# =============================================================================

def mode_roundtrip(conn, rows, findings, settle):
    chk = asserted_rows(rows)

    print('[2/7] setting value-set A (converge) ...')
    prof_a, unfit_a, errs = converge_set(conn, rows, chk, 'a', settle, 'set-A')
    findings.add_set_errors('set-A', errs)
    print(f'[3/7] guarantee 1 (save reads A): {len(unfit_a)} unsettled')
    for r in unfit_a:
        findings.add('GUARANTEE 1 (save reads value A)',
                     r['field'], oracle(r, 'a'), xml_get(prof_a.root, r['xml']),
                     r['xml'], r.get('note'))

    print('[4/7] scrambling to value-set B (converge) ...')
    prof_b, unfit_b, errs = converge_set(conn, rows, chk, 'b', settle, 'set-B')
    findings.add_set_errors('set-B', errs)
    print(f'[5/7] guarantee 1 (save reads B): {len(unfit_b)} unsettled')
    for r in unfit_b:
        findings.add('GUARANTEE 1 (save reads value B)',
                     r['field'], oracle(r, 'b'), xml_get(prof_b.root, r['xml']),
                     r['xml'], r.get('note'))

    # Restore A section-by-section with retry. Per-section applies stay small
    # (no ~200-command burst) and the retry recovers dropped packets, so what
    # remains unfit is a real restore gap, not transport flakiness.
    print('[6/7] restoring profile A (apply + converge) ...')
    prof_a2, unfit2 = converge_restore(conn, prof_a, rows, prof_a.root,
                                       settle, 'restore-A')
    print(f'[7/7] guarantee 2 (restore writes A): {len(unfit2)} unsettled')
    for r in unfit2:
        findings.add('GUARANTEE 2 (restore writes value A)',
                     r['field'], xml_get(prof_a.root, r['xml']),
                     xml_get(prof_a2.root, r['xml']), r['xml'], r.get('note'))

    return {'A': prof_a, 'B': prof_b, 'A2': prof_a2}


# =============================================================================
# Mode: save-sections (guarantee 3a)
# =============================================================================

def mode_save_sections(conn, rows, findings, section_filter):
    sections = _sections_in(rows, section_filter)
    print(f'[save-sections] checking {len(sections)} section(s): '
          f'{", ".join(sections)}')
    # All section xml-paths we know how to check presence for.
    known = {s: SECTIONS[s]['xml'] for s in sections if s in SECTIONS}
    for s in sections:
        if s not in SECTIONS and _usk_index(s) is None:
            continue
        prof = save_profile(conn, save_options_only(s))
        root = prof.root
        own_xml = _section_presence_path(s)
        if root.find(own_xml) is None:
            findings.add('GUARANTEE 3a (selective save: section present)',
                         s, 'element present', 'MISSING', own_xml)
        # Every OTHER known section's element must be absent.
        own = _section_presence_path(s)
        for other, opath in known.items():
            if other == s:
                continue
            # Skip structurally-nested pairs (e.g. inputs lives under Settings,
            # so saving 'settings' necessarily emits Settings/Inputs and vice
            # versa). M/E cells share the MixEffectBlocks wrapper but our paths
            # target distinct child elements, so those are not nested.
            if _xml_nested(own, opath):
                continue
            if root.find(opath) is not None:
                findings.add('GUARANTEE 3a (selective save: no other section)',
                             f'{s} leaked {other}', 'absent', 'present', opath)


def _xml_nested(a, b) -> bool:
    """True if element path a and b are the same or one contains the other."""
    return a == b or a.startswith(b + '/') or b.startswith(a + '/')


def _section_presence_path(section_key):
    i = _usk_index(section_key)
    if i is not None:
        return f"MixEffectBlocks/MixEffectBlock/Keys/Key[@index='{i}']"
    return SECTIONS[section_key]['xml']


# =============================================================================
# Mode: no-leak restore (guarantee 3b — broadcast safety)
# =============================================================================

def mode_no_leak(conn, rows, findings, settle, section_filter):
    chk = asserted_rows(rows)
    sections = _sections_in(rows, section_filter)
    print(f'[no-leak] reference = value-set A; testing {len(sections)} '
          f'section(s): {", ".join(sections)}')

    print('[no-leak] establishing A reference (converge) ...')
    prof_a, unfit_a, errs = converge_set(conn, rows, chk, 'a', settle,
                                         'no-leak set-A')
    findings.add_set_errors('no-leak set-A', errs)
    for r in unfit_a:
        findings.add('GUARANTEE 3b (could not establish A reference)',
                     r['field'], oracle(r, 'a'), xml_get(prof_a.root, r['xml']),
                     r['xml'])
    root_a = prof_a.root

    for s in sections:
        print(f"[no-leak] section {s!r}: scramble->B, restore only {s}, verify ...")
        _, _, errsb = converge_set(conn, rows, chk, 'b', settle,
                                   f'no-leak set-B({s})')
        findings.add_set_errors(f'no-leak set-B ({s})', errsb)
        s_rows = [r for r in rows if r['section'] == s]
        prof_after, unfit_s = converge_restore(conn, prof_a, s_rows, root_a,
                                               settle, f'no-leak restore({s})')
        root_after = prof_after.root
        # S's own fields must have reverted to A.
        for r in unfit_s:
            findings.add(f'GUARANTEE 3b ({s}: restored field did not revert)',
                         r['field'], xml_get(root_a, r['xml']),
                         xml_get(root_after, r['xml']), r['xml'])
        # Every OTHER field must still be B. A true LEAK = a non-S field that
        # moved to its A value (restoring S touched it). A value that is
        # neither A nor B is transport drift, reported as 'unstable' not a leak.
        for r in chk:
            if r['section'] == s:
                continue
            got = xml_get(root_after, r['xml'])
            if compare(oracle(r, 'b'), got, r.get('tol', 0.05)):
                continue  # still B — good
            a_val = xml_get(root_a, r['xml'])
            if compare(a_val, got, r.get('tol', 0.05)):
                findings.add(f'GUARANTEE 3b ({s}: LEAK into {r["section"]})',
                             r['field'], f'B={oracle(r, "b")}', f'A={got}',
                             r['xml'])
            else:
                findings.add(f'GUARANTEE 3b ({s}: unstable {r["section"]})',
                             r['field'], f'B={oracle(r, "b")}', got, r['xml'])


def _sections_in(rows, section_filter):
    present = []
    for r in rows:
        if r['section'] not in present:
            present.append(r['section'])
    if section_filter:
        present = [s for s in present if s in section_filter]
    return present


# =============================================================================
# Dry run — validate XPaths + planned values against real hardware XML
# =============================================================================

def dry_run(conn, rows):
    print('[dry-run] saving current ATEM state (mutates nothing) ...')
    root = save_profile(conn).root
    print(f'\n{"field":<28} {"resolves":<9} {"A":>10} {"B":>10}  oracle(A)/oracle(B)')
    print('-' * 92)
    unresolved = 0
    for r in rows:
        got = xml_get(root, r['xml'])
        ok = 'OK' if got is not None else 'MISSING'
        if got is None:
            unresolved += 1
        ann = (f"  [quirk: {r['quirk']}]" if r.get('quirk')
               else f"  [note: {r['note']}]" if r.get('note') else '')
        print(f"{r['field']:<28} {ok:<9} {str(r['a']):>10} {str(r['b']):>10}  "
              f"{oracle(r, 'a')} / {oracle(r, 'b')}{ann}")
        if got is not None and ok == 'OK':
            print(f"    path: {r['xml']}   (live now: {got!r})")
        else:
            print(f"    path: {r['xml']}   <-- DOES NOT RESOLVE")
    print('-' * 92)
    print(f"{len(rows)} field(s); {unresolved} XPath(s) did not resolve.")
    return 1 if unresolved else 0


# =============================================================================
# Snapshot / restore-original
# =============================================================================

def snapshot_original(conn, artifacts_dir):
    print('[1/7] snapshotting current ATEM state ...')
    prof = save_profile(conn)
    path = os.path.join(artifacts_dir, 'original.xml')
    prof.to_file(path)
    print(f'      original state written to {path}')
    return prof


def restore_original(conn, original, rows, settle):
    """Undo the harness's changes by restoring the touched sections from the
    start-of-run snapshot — section-by-section with retry (converge) so a
    dropped packet doesn't leave the test ATEM scrambled."""
    print('[restore] restoring original ATEM state (touched sections) ...')
    _, unfit = converge_restore(conn, original, rows, original.root, settle,
                                'restore-original')
    if unfit:
        print(f'      WARNING: {len(unfit)} field(s) did not restore to '
              f'original: {", ".join(r["field"] for r in unfit)}')


# =============================================================================
# Main
# =============================================================================

def _confirm(ip):
    print('\n' + '!' * 72)
    print(f'  DESTRUCTIVE round-trip smoke against ATEM {ip}')
    print('  This changes EVERYTHING on the switcher, including on-air buses')
    print('  (aux routing, USK on-air, DSK tie). TEST ATEM ONLY.')
    print('  Original state is snapshotted and restored at the end.')
    print('!' * 72)
    ans = input(f"\nType 'yes' to proceed against {ip}: ").strip().lower()
    if ans != 'yes':
        print('Aborted.')
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('ip')
    ap.add_argument('--mode', default='roundtrip',
                    choices=['roundtrip', 'save-sections', 'no-leak', 'all'])
    ap.add_argument('--features', default='')
    ap.add_argument('--sections', default='')
    ap.add_argument('--include-program-preview', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--keep-going', action='store_true')
    ap.add_argument('--no-restore', action='store_true')
    ap.add_argument('--settle', type=float, default=2.5)
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()

    features = [f.strip() for f in args.features.split(',') if f.strip()]
    section_filter = [s.strip() for s in args.sections.split(',') if s.strip()]
    rows = rows_for(features)
    # program/preview cut to air; opt in explicitly. (No such rows exist yet —
    # this is the wiring for when they're added as quirk-gated rows.)
    if not args.include_program_preview:
        rows = [r for r in rows if r['section'] not in ('program', 'preview')]

    print(f'# roundtrip_smoke — target {args.ip}')
    print(f'# features: {", ".join(features) if features else "ALL"} '
          f'({len(rows)} fields, {len(asserted_rows(rows))} asserted)')

    if args.dry_run:
        with acquire_connection(args.ip) as conn:
            wait_state_settled(conn, timeout=6.0)
            rc = dry_run(conn, rows)
        sys.exit(rc)

    if not args.force:
        _confirm(args.ip)

    artifacts_dir = tempfile.mkdtemp(
        prefix=f'roundtrip_smoke_{datetime.datetime.now():%Y%m%d_%H%M%S}_')
    print(f'# XML artifacts -> {artifacts_dir}')

    findings = Findings()
    modes = (['roundtrip', 'save-sections', 'no-leak']
             if args.mode == 'all' else [args.mode])

    with acquire_connection(args.ip) as conn:
        wait_state_settled(conn, timeout=6.0)
        original = snapshot_original(conn, artifacts_dir)
        try:
            if 'roundtrip' in modes:
                profs = mode_roundtrip(conn, rows, findings, args.settle)
                for name, prof in profs.items():
                    prof.to_file(os.path.join(artifacts_dir, f'{name}.xml'))
            if 'save-sections' in modes:
                mode_save_sections(conn, rows, findings, section_filter)
            if 'no-leak' in modes:
                mode_no_leak(conn, rows, findings, args.settle, section_filter)
        finally:
            if not args.no_restore:
                restore_original(conn, original, rows, args.settle)

    rc = findings.report()
    print(f'\n# artifacts kept at {artifacts_dir}')
    sys.exit(rc)


if __name__ == '__main__':
    # A signal unwinds instead of killing, so every session gets its goodbye.
    import _exit_signals
    _exit_signals.install()
    main()
