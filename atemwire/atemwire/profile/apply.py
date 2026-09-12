# SPDX-License-Identifier: LGPL-3.0-only
"""
Apply side — walk the parsed XML, dispatch to per-feature operations.

Each ``_apply_<section>`` reads the relevant chunk of the profile XML
and emits the corresponding wire commands. ``_safe`` wraps each
section in error handling that records into ``ApplyResult`` instead
of bubbling — one bad section never aborts the whole apply.

Macros are restored by replaying each ``<Op>`` as a wire command
inside an ``MSRc``/``MAct`` envelope (see ``MACRO_FORMAT.md``); the
ATEM captures whatever's on the wire while recording.

Operation imports are grouped by feature module under
``atemwire.messages``. The ``_OPS`` dict at the bottom of the import
block is the dispatch namespace used by the string-keyed tables
(``_USK_MASK_EDGES`` etc.); it's built from an explicit name list
rather than ``globals()`` filtering so a missing entry fails loudly
at module load instead of at runtime.
"""

import logging
from typing import Optional

from atemwire._state import _me_count

from atemwire.messages.color_generator import (
    set_color_generator_hue, set_color_generator_luma,
    set_color_generator_saturation,
)
from atemwire.messages.downstream_keyer import (
    configure_dsk_gain, set_dsk_fill_source, set_dsk_key_source,
    set_dsk_mask_bottom, set_dsk_mask_enabled, set_dsk_mask_left,
    set_dsk_mask_right, set_dsk_mask_top, set_dsk_on_air, set_dsk_rate,
    set_dsk_tie,
)
from atemwire.messages.fade_to_black import set_ftb_disabled, set_ftb_rate
from atemwire.messages.hyperdeck import set_hyperdeck_settings
from atemwire.messages.fairlight import (
    set_fairlight_compressor, set_fairlight_eq_band, set_fairlight_expander,
    set_fairlight_limiter, set_fairlight_master,
    set_fairlight_master_compressor, set_fairlight_master_eq_band,
    set_fairlight_master_limiter, set_fairlight_strip,
)
from atemwire.messages.input_video import (
    VIDEO_MODE_NAMES, set_input_label, set_video_mode,
)
from atemwire.messages.media import (
    set_media_player_clip, set_media_player_still,
)
from atemwire.messages.switching import (
    set_aux_output, set_preview, set_program,
)
from atemwire.messages.system_info import video_mode as _state_video_mode
from atemwire.messages.transition import (
    set_dip_rate, set_dip_source, set_dve_clip, set_dve_enable_key,
    set_dve_fill_source, set_dve_flip_flop, set_dve_gain,
    set_dve_invert_key, set_dve_key_source, set_dve_pre_multiplied,
    set_dve_rate, set_dve_reverse, set_dve_style, set_mix_rate,
    set_next_transition_layers, set_stinger_clip,
    set_stinger_clip_duration, set_stinger_gain, set_stinger_invert_key,
    set_stinger_mix_rate, set_stinger_pre_multiplied,
    set_stinger_pre_roll, set_stinger_source,
    set_stinger_trigger_point, set_transition_style,
    set_wipe_fill_source, set_wipe_flip_flop, set_wipe_pattern,
    set_wipe_position_x, set_wipe_position_y, set_wipe_rate,
    set_wipe_reverse, set_wipe_softness, set_wipe_symmetry,
    set_wipe_width,
)
from atemwire.messages.upstream_keyer import (
    configure_usk_luma, set_keyer_fly_keyframe,
    set_usk_chroma_background, set_usk_chroma_blue,
    set_usk_chroma_brightness, set_usk_chroma_contrast,
    set_usk_chroma_flare_suppression, set_usk_chroma_foreground,
    set_usk_chroma_green, set_usk_chroma_key_edge,
    set_usk_chroma_red, set_usk_chroma_sample_position,
    set_usk_chroma_sample_size, set_usk_chroma_sampled_color,
    set_usk_chroma_saturation, set_usk_chroma_spill,
    set_usk_dve_border_bevel_position, set_usk_dve_border_bevel_softness,
    set_usk_dve_border_enabled, set_usk_dve_border_hue,
    set_usk_dve_border_inner_softness, set_usk_dve_border_inner_width,
    set_usk_dve_border_luma, set_usk_dve_border_opacity,
    set_usk_dve_border_outer_softness, set_usk_dve_border_outer_width,
    set_usk_dve_border_saturation, set_usk_dve_bottom, set_usk_dve_left,
    set_usk_dve_light_altitude, set_usk_dve_light_direction,
    set_usk_dve_masked, set_usk_dve_position_x, set_usk_dve_position_y,
    set_usk_dve_rate, set_usk_dve_right, set_usk_dve_rotation,
    set_usk_dve_size_x, set_usk_dve_size_y,
    set_usk_dve_shadow, set_usk_dve_top, set_usk_fill_source,
    set_usk_fly_enabled, set_usk_key_source,
    set_usk_mask_bottom, set_usk_mask_enabled, set_usk_mask_left,
    set_usk_mask_right, set_usk_mask_top, set_usk_on_air,
    set_usk_pattern_invert, set_usk_pattern_position_x,
    set_usk_pattern_position_y, set_usk_pattern_size,
    set_usk_pattern_softness, set_usk_pattern_style,
    set_usk_pattern_symmetry, set_usk_type,
)
from atemwire.profile._enums import (
    DVE_EFFECT_TO_INT,
    TRANS_STYLE_TO_INT,
    USK_TYPE_TO_INT,
    WIPE_PATTERN_TO_INT,
)
from atemwire.profile._xml import (
    _bool,
    _float,
    _int,
    _str_to_next_transition,
)
from atemwire.profile.options import ApplyResult


# Dispatch namespace for the string-keyed tables (USK mask edges, USK
# pattern floats, USK DVE bits, USK chroma floats, DSK mask edges).
# Each entry must map a string name appearing in one of those tables
# to the imported function above. Explicit list — a missing entry
# raises KeyError at runtime if a table references it, which is loud
# enough to catch in CI.
_OPS = {
    # USK mask edges
    'set_usk_mask_top':    set_usk_mask_top,
    'set_usk_mask_bottom': set_usk_mask_bottom,
    'set_usk_mask_left':   set_usk_mask_left,
    'set_usk_mask_right':  set_usk_mask_right,
    # USK pattern floats
    'set_usk_pattern_size':       set_usk_pattern_size,
    'set_usk_pattern_symmetry':   set_usk_pattern_symmetry,
    'set_usk_pattern_softness':   set_usk_pattern_softness,
    'set_usk_pattern_position_x': set_usk_pattern_position_x,
    'set_usk_pattern_position_y': set_usk_pattern_position_y,
    # USK DVE floats
    'set_usk_dve_light_direction':       set_usk_dve_light_direction,
    'set_usk_dve_light_altitude':        set_usk_dve_light_altitude,
    'set_usk_dve_border_hue':            set_usk_dve_border_hue,
    'set_usk_dve_border_saturation':     set_usk_dve_border_saturation,
    'set_usk_dve_border_luma':           set_usk_dve_border_luma,
    'set_usk_dve_border_outer_width':    set_usk_dve_border_outer_width,
    'set_usk_dve_border_inner_width':    set_usk_dve_border_inner_width,
    'set_usk_dve_border_outer_softness': set_usk_dve_border_outer_softness,
    'set_usk_dve_border_inner_softness': set_usk_dve_border_inner_softness,
    'set_usk_dve_border_opacity':        set_usk_dve_border_opacity,
    'set_usk_dve_border_bevel_position': set_usk_dve_border_bevel_position,
    'set_usk_dve_border_bevel_softness': set_usk_dve_border_bevel_softness,
    # USK DVE bools
    'set_usk_dve_shadow':         set_usk_dve_shadow,
    'set_usk_dve_border_enabled': set_usk_dve_border_enabled,
    # USK DVE mask edges
    'set_usk_dve_top':    set_usk_dve_top,
    'set_usk_dve_bottom': set_usk_dve_bottom,
    'set_usk_dve_left':   set_usk_dve_left,
    'set_usk_dve_right':  set_usk_dve_right,
    # USK chroma floats
    'set_usk_chroma_foreground':         set_usk_chroma_foreground,
    'set_usk_chroma_background':         set_usk_chroma_background,
    'set_usk_chroma_key_edge':           set_usk_chroma_key_edge,
    'set_usk_chroma_spill':              set_usk_chroma_spill,
    'set_usk_chroma_flare_suppression':  set_usk_chroma_flare_suppression,
    'set_usk_chroma_brightness':         set_usk_chroma_brightness,
    'set_usk_chroma_contrast':           set_usk_chroma_contrast,
    'set_usk_chroma_red':                set_usk_chroma_red,
    'set_usk_chroma_green':              set_usk_chroma_green,
    'set_usk_chroma_blue':               set_usk_chroma_blue,
    # DSK mask edges
    'set_dsk_mask_top':    set_dsk_mask_top,
    'set_dsk_mask_bottom': set_dsk_mask_bottom,
    'set_dsk_mask_left':   set_dsk_mask_left,
    'set_dsk_mask_right':  set_dsk_mask_right,
    # DSK tie (single field — dispatched via _OPS in _apply_downstream_keys)
    'set_dsk_tie':         set_dsk_tie,
}

logger = logging.getLogger(__name__)


# =============================================================================
# apply helpers — walk XML, dispatch to operations
# =============================================================================

def _safe(section: str, result: ApplyResult, fn, *args, **kwargs) -> None:
    """Run fn, capture exceptions into ApplyResult, log."""
    try:
        fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        result.note_error(section, e)


def _iter_me_blocks(conn, root):
    """Yield ``(me, block)`` for every ``<MixEffectBlock>`` whose index
    exists on the connected switcher. Blocks whose index is at or
    beyond the live ``me_count`` are skipped silently — a 4-M/E
    profile applied to a 1-M/E switcher applies only index 0
    (cross-model load, not an error). The per-M/E option gating
    (``opts.mes[me]``) is the caller's job."""
    me_count = _me_count(getattr(conn, 'mixerstate', None) or {})
    for block in root.findall('MixEffectBlocks/MixEffectBlock'):
        me = _int(block.get('index'), 0)
        if me < 0 or me >= me_count:
            logger.debug(
                "skipping <MixEffectBlock index=%r> — switcher has %d M/E(s)",
                block.get('index'), me_count)
            continue
        yield me, block


# Per-section field tables. Each row maps an XML attribute name to the
# wire op + kwarg + default. ``_FLOAT`` rows pass the attribute through
# ``_float(value, default)``; ``_BOOL`` rows through ``_bool(value)``
# (default False). The function bodies below iterate these tables so
# adding a new field is a one-line edit at the table.

_USK_MASK_EDGES = (
    # (xml_attr, op_fn, kw_name) — value is _float(default 0.0).
    ('maskTop',    'set_usk_mask_top',    'top'),
    ('maskBottom', 'set_usk_mask_bottom', 'bottom'),
    ('maskLeft',   'set_usk_mask_left',   'left'),
    ('maskRight',  'set_usk_mask_right',  'right'),
)

_USK_PATTERN_FLOATS = (
    # (xml_attr, op_fn, kw_name, default)
    ('size',      'set_usk_pattern_size',       'size',       50.0),
    ('symmetry',  'set_usk_pattern_symmetry',   'symmetry',   50.0),
    ('softness',  'set_usk_pattern_softness',   'softness',   0.0),
    ('xPosition', 'set_usk_pattern_position_x', 'position_x', 0.5),
    ('yPosition', 'set_usk_pattern_position_y', 'position_y', 0.5),
)

_USK_DVE_FLOATS = (
    # All default 0.0 — leave the wire field alone when the XML omits.
    ('lightSourceDirection',  'set_usk_dve_light_direction',     'direction'),
    ('lightSourceAltitude',   'set_usk_dve_light_altitude',      'altitude'),
    ('borderBevelHue',        'set_usk_dve_border_hue',          'hue'),
    ('borderBevelSaturation', 'set_usk_dve_border_saturation',   'saturation'),
    ('borderBevelLuma',       'set_usk_dve_border_luma',         'luma'),
    ('borderWidthOut',        'set_usk_dve_border_outer_width',  'outer_width'),
    ('borderWidthIn',         'set_usk_dve_border_inner_width',  'inner_width'),
    ('borderSoftnessOut',     'set_usk_dve_border_outer_softness',  'outer_softness'),
    ('borderSoftnessIn',      'set_usk_dve_border_inner_softness',  'inner_softness'),
    ('borderBevelOpacity',    'set_usk_dve_border_opacity',      'opacity'),
    ('borderBevelPosition',   'set_usk_dve_border_bevel_position',  'bevel_position'),
    ('borderBevelSoftness',   'set_usk_dve_border_bevel_softness',  'bevel_softness'),
)

_USK_DVE_BOOLS = (
    # (xml_attr, op_fn, kw_name)
    ('shadowEnabled', 'set_usk_dve_shadow',         'shadow'),
    ('borderEnabled', 'set_usk_dve_border_enabled', 'enabled'),
)

_USK_DVE_MASK_EDGES = (
    # DVE mask edges. Same shape as the USK mask above, routed through
    # the DVE-specific set_usk_dve_* ops.
    ('maskTop',    'set_usk_dve_top',    'top'),
    ('maskBottom', 'set_usk_dve_bottom', 'bottom'),
    ('maskLeft',   'set_usk_dve_left',   'left'),
    ('maskRight',  'set_usk_dve_right',  'right'),
)

_USK_CHROMA_FLOATS = (
    # Raw float fields — XML value passes straight to the op (signed
    # color-correction; range is NOT 0..100). Percent fields live in
    # _USK_CHROMA_PERCENTS below.
    ('brightness',      'set_usk_chroma_brightness',         'brightness'),
    ('contrast',        'set_usk_chroma_contrast',           'contrast'),
    ('red',             'set_usk_chroma_red',                'red'),
    ('green',           'set_usk_chroma_green',              'green'),
    ('blue',            'set_usk_chroma_blue',               'blue'),
)

# Chroma fields whose XML is 0..100 percent but the op takes 0..1 unit —
# same x100-save / /100-restore as keyEdge. (saturation is handled
# separately: its range is 0..2, not 0..100.)
_USK_CHROMA_PERCENTS = (
    ('foregroundLevel', 'set_usk_chroma_foreground',        'foreground'),
    ('backgroundLevel', 'set_usk_chroma_background',        'background'),
    ('keyEdge',         'set_usk_chroma_key_edge',          'key_edge'),
    ('spillSuppress',   'set_usk_chroma_spill',             'spill'),
    ('flareSuppress',   'set_usk_chroma_flare_suppression', 'flare_suppression'),
)

# FlyParameters geometry → USK DVE position/size/rotation ops (direct
# function refs, since these aren't in _OPS). ``rate`` is handled
# separately (it's a frame-string, not a float).
_USK_FLY_GEOMETRY = (
    ('xPosition', set_usk_dve_position_x, 'position_x'),
    ('yPosition', set_usk_dve_position_y, 'position_y'),
    ('xSize',     set_usk_dve_size_x,     'size_x'),
    ('ySize',     set_usk_dve_size_y,     'size_y'),
    ('rotation',  set_usk_dve_rotation,   'rotation'),
)

# <KeyFrameA/B> attribute → USK DVE setter. The keyframe restore drives the
# LIVE DVE geometry to these values then SFKF-snapshots it (the ATEM has no
# direct keyframe-geometry write). Attribute names differ from
# <DVEParameters> (e.g. borderOpacity vs borderBevelOpacity, borderHue vs
# borderBevelHue, borderLightSourceDirection vs lightSourceDirection), so
# this is its own table. All values pass through ``_float``; the setters
# apply their own wire scaling (same as the live DVE/Fly restore).
_FLY_KEYFRAME_GEOMETRY = (
    ('xPosition',                  set_usk_dve_position_x,            'position_x'),
    ('yPosition',                  set_usk_dve_position_y,            'position_y'),
    ('xSize',                      set_usk_dve_size_x,                'size_x'),
    ('ySize',                      set_usk_dve_size_y,                'size_y'),
    ('rotation',                   set_usk_dve_rotation,              'rotation'),
    ('borderWidthOut',             set_usk_dve_border_outer_width,    'outer_width'),
    ('borderWidthIn',              set_usk_dve_border_inner_width,    'inner_width'),
    ('borderSoftnessOut',          set_usk_dve_border_outer_softness, 'outer_softness'),
    ('borderSoftnessIn',           set_usk_dve_border_inner_softness, 'inner_softness'),
    ('borderBevelSoftness',        set_usk_dve_border_bevel_softness, 'bevel_softness'),
    ('borderBevelPosition',        set_usk_dve_border_bevel_position, 'bevel_position'),
    ('borderOpacity',              set_usk_dve_border_opacity,        'opacity'),
    ('borderHue',                  set_usk_dve_border_hue,            'hue'),
    ('borderSaturation',           set_usk_dve_border_saturation,     'saturation'),
    ('borderLuma',                 set_usk_dve_border_luma,           'luma'),
    ('borderLightSourceDirection', set_usk_dve_light_direction,       'direction'),
    ('borderLightSourceAltitude',  set_usk_dve_light_altitude,        'altitude'),
    ('maskTop',                    set_usk_dve_top,                   'top'),
    ('maskBottom',                 set_usk_dve_bottom,                'bottom'),
    ('maskLeft',                   set_usk_dve_left,                  'left'),
    ('maskRight',                  set_usk_dve_right,                 'right'),
)

_DSK_MASK_EDGES = (
    ('maskTop',    'set_dsk_mask_top',    'top'),
    ('maskBottom', 'set_dsk_mask_bottom', 'bottom'),
    ('maskLeft',   'set_dsk_mask_left',   'left'),
    ('maskRight',  'set_dsk_mask_right',  'right'),
)


def _apply_video_mode(conn, root, result):
    """Apply the <VideoMode videoMode="..."/> element, if it differs from
    the live state. Mode changes are destructive: outputs drop briefly
    while the ATEM resyncs and clients may be force-disconnected. We log
    a warning before sending and sleep ~3s afterwards to let the dump
    re-arrive before the rest of apply runs.
    """
    import time as _time

    e = root.find('VideoMode')
    if e is None:
        return
    target_name = e.get('videoMode', '')
    if not target_name:
        result.note_skipped('video_mode', 'empty videoMode in profile')
        return

    target_int = VIDEO_MODE_NAMES.get(target_name)
    if target_int is None:
        result.note_skipped('video_mode',
                            f'unrecognized mode {target_name!r}')
        return

    current = _state_video_mode(conn.mixerstate)
    current_label = (current or {}).get('format', '') if current else ''
    # state.video_mode emits "1080p60 16:9" — the XML doesn't carry the
    # aspect suffix, so split before comparing.
    current_label = current_label.split(' ')[0] if current_label else ''

    if current_label == target_name:
        result.note_skipped('video_mode',
                            f'already at {target_name}')
        return

    logger.warning(
        "Profile.apply changing video mode %s → %s. Outputs will drop "
        "briefly while the ATEM resyncs.", current_label or '?', target_name,
    )
    try:
        set_video_mode(conn, mode=target_name)
    except Exception as exc:  # noqa: BLE001
        result.note_error('video_mode', exc)
        return
    # Wait for the resync. The ATEM stops responding for ~1-2s while it
    # reconfigures. Sleep here so subsequent sections don't race the
    # resync and end up with their commands dropped.
    _time.sleep(3.0)
    result.note_applied(f'video_mode (-> {target_name})')


def _apply_inputs(conn, root, result):
    """Apply the <Settings><Inputs><Input .../></Inputs> renames.

    Each input gets a single CInL packet with both labels. Per-input
    failure is captured but doesn't abort the rest of the section.
    """
    settings = root.find('Settings')
    if settings is None:
        return
    inputs_e = settings.find('Inputs')
    if inputs_e is None:
        return
    n = 0
    for input_e in inputs_e.findall('Input'):
        src = _int(input_e.get('id'))
        if src <= 0:
            continue
        long_name = input_e.get('longName')
        short_name = input_e.get('shortName')
        if long_name is None and short_name is None:
            continue
        try:
            set_input_label(
                conn, source=src,
                long_name=long_name, short_name=short_name,
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            result.note_error(f'inputs[{src}]', exc)
    if n:
        result.note_applied(f'inputs ({n})')


# ---------------------------------------------------------------------------
# Fairlight audio mixer apply
#
# Walks the <FairlightAudioMixer> tree and emits granular write commands.
# Three categories:
#   1. Master out (CFMP)        — eq enable, eq gain, dynamics gain, volume,
#                                 audio-follow-video.
#   2. Per-strip (CFSP) per AudioSource — input gain, EQ enable + master
#                                 gain, dynamics make-up gain, pan, fader
#                                 gain (special-case "-inf"), mix option.
#   3. Per-EQ-band (CEBP) for strips, CMBP for the master — six bands each,
#                                 with enabled / shape / freq range /
#                                 frequency / gain / Q. The master bands use
#                                 the master-specific CMBP (band index, no
#                                 source/channel), NOT CEBP source=0.
#
# Per-strip Compressor / Limiter / Expander ARE applied, via CICP / CILP /
# CIXP (see _apply_dynamics_block). Master-out comp/limiter are applied via
# the dedicated master Send commands CMCP / CMLP (no source/channel — see
# below); master EQ enable/gain via CFMP and master EQ bands via CMBP. Save
# reads the MOCP / AMLP / FAMP / AMBP echoes, so the whole master bus now
# round-trips. The only remaining Fairlight gap is the master Expander (the
# master bus has none).
#
# Volume burst: a full Fairlight restore on a 10-input switcher emits
# ~1 + 6 (master EQ bands) + 10 × (1 + 6) = ~77 commands. The chunking
# in ATEMConnection._drain_cmd_queue handles the UDP-packet split.
# ---------------------------------------------------------------------------

def _apply_fairlight(conn, root, result):
    fa = root.find('FairlightAudioMixer')
    if fa is None:
        return

    # Master out (CFMP). ASC writes each master field in its OWN single-mask
    # CFMP packet, so we mirror that and emit one field per packet (EQ enable
    # first). NOTE: this per-field split is NOT what made the EQ gain restore
    # — the real bug was the gain wire encoding (it is a sign-extended i32,
    # not an s16; see FairlightMasterPropertiesCommand). The split is kept
    # because it faithfully matches ASC and is harmless.
    master_eq = fa.find('MasterOutEqualizer')
    master_dyn = fa.find('MasterOutDynamicsProcessor')
    master_fields = [
        ('eq_enable',
         _bool(master_eq.get('enabled')) if master_eq is not None else None),
        ('eq_gain_db',
         _float(master_eq.get('gain')) if master_eq is not None else None),
        ('dynamics_makeup_db',
         _float(master_dyn.get('makeupGain')) if master_dyn is not None else None),
        ('volume_db', _fader_db(fa.get('masterOutFaderGain'))),
        ('afv', _bool(fa.get('followFadeToBlack'))),
    ]
    for kw, val in master_fields:
        if val is None:
            continue
        _safe('fairlight.master', result, set_fairlight_master, conn, **{kw: val})

    # Master EQ bands — CMBP (a band_index selects the band; no source /
    # channel). The prior CEBP source=0 path wrote BLACK's EQ, never the
    # master bus — the same Black-not-master bug the dynamics had. CMBP RE'd
    # from a Wireshark capture of Software Control (2026-06-04).
    if master_eq is not None:
        for band_e in master_eq.findall('EqualizerBand'):
            _apply_master_eq_band(conn, band_e=band_e, result=result)

    # Master dynamics — Compressor (CMCP) + Limiter (CMLP). The master bus
    # has its own compact Send commands (no source/channel — "master" is
    # implicit in the 4cc), RE'd from a Software Control capture. This is
    # NOT the per-strip CICP/CILP path (source=0 there is Black, not the
    # master bus — that earlier wiring was wrong and is gone). The master
    # make-up gain is handled above via set_fairlight_master (CFMP/FAMP),
    # not here. Save reads the matching MOCP/AMLP echoes live, so this now
    # round-trips end to end.
    if master_dyn is not None:
        comp = master_dyn.find('Compressor')
        lim = master_dyn.find('Limiter')
        if comp is not None:
            _safe('fairlight.master.compressor', result,
                  set_fairlight_master_compressor, conn,
                  enabled=_bool(comp.get('enabled')),
                  threshold_db=_float(comp.get('threshold')),
                  ratio=_float(comp.get('ratio')),
                  attack_ms=_float(comp.get('attack')),
                  hold_ms=_float(comp.get('hold')),
                  release_ms=_float(comp.get('release')))
        if lim is not None:
            _safe('fairlight.master.limiter', result,
                  set_fairlight_master_limiter, conn,
                  enabled=_bool(lim.get('enabled')),
                  threshold_db=_float(lim.get('threshold')),
                  attack_ms=_float(lim.get('attack')),
                  hold_ms=_float(lim.get('hold')),
                  release_ms=_float(lim.get('release')))
        if comp is not None or lim is not None:
            result.note_applied('fairlight.master.dynamics')

    # Per-strip
    inputs_e = fa.find('AudioInputs')
    if inputs_e is not None:
        n_strips = 0
        n_bands = 0
        for ai in inputs_e.findall('AudioInput'):
            src = _int(ai.get('id'))
            if src <= 0:
                continue
            for src_e in ai.findall('AudioSource'):
                # Constellation-HD profiles use channel=-1 (stereo
                # combined) or -256/-65280 (mono natively / stereo
                # natively). Both map to channel=-1 in the wire format.
                channel = -1
                try:
                    set_fairlight_strip(
                        conn,
                        source=src, channel=channel,
                        input_gain_db=_float(src_e.get('inputGain')),
                        pan=_float(src_e.get('pan')),
                        fader_gain_db=_fader_db(src_e.get('faderGain')),
                        mix_option=src_e.get('mixOption'),
                        delay_frames=(_int(src_e.get('delayFrames'))
                                      if src_e.get('delayFrames') is not None
                                      else None),
                    )
                    n_strips += 1
                except Exception as exc:  # noqa: BLE001
                    result.note_error(f'fairlight.strip[{src}]', exc)

                # EQ block — set master EQ enable/gain on the strip too,
                # then per-band.
                eq_e = src_e.find('Equalizer')
                if eq_e is not None:
                    try:
                        set_fairlight_strip(
                            conn, source=src, channel=channel,
                            eq_enable=_bool(eq_e.get('enabled')),
                            eq_gain_db=_float(eq_e.get('gain')),
                        )
                    except Exception as exc:  # noqa: BLE001
                        result.note_error(f'fairlight.strip[{src}].eq', exc)
                    for band_e in eq_e.findall('EqualizerBand'):
                        _apply_eq_band(conn, source=src, channel=channel,
                                       band_e=band_e, result=result,
                                       label=f'fairlight.strip[{src}].eq')
                        n_bands += 1

                # Dynamics: make-up gain on the strip, then the
                # Compressor / Limiter / Expander blocks.
                dyn_e = src_e.find('DynamicsProcessor')
                if dyn_e is not None:
                    try:
                        set_fairlight_strip(
                            conn, source=src, channel=channel,
                            dynamics_makeup_db=_float(dyn_e.get('makeupGain')),
                        )
                    except Exception as exc:  # noqa: BLE001
                        result.note_error(
                            f'fairlight.strip[{src}].dynamics', exc)
                    _apply_dynamics_block(
                        conn, source=src, channel=channel, dyn_e=dyn_e,
                        result=result, label=f'fairlight.strip[{src}]')
        if n_strips:
            result.note_applied(
                f'fairlight ({n_strips} strips, {n_bands} EQ bands)')


def _fader_db(s) -> Optional[float]:
    """Parse a faderGain string to dB. Recognizes the literal '-inf'
    sentinel (silenced) which the operations layer maps to wire -10000.
    Returns None on missing/empty input so partial CFSP packets don't
    overwrite live state."""
    if s is None or s == '':
        return None
    if isinstance(s, str) and s.strip().lower() in ('-inf', '-infinity'):
        return float('-inf')
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _apply_master_eq_band(conn, *, band_e, result):
    """Send one CMBP packet (master-bus EQ band) with all observed fields.
    No source/channel — the master command carries only a band index."""
    band = _int(band_e.get('index'))
    try:
        set_fairlight_master_eq_band(
            conn, band=band,
            enabled=_bool(band_e.get('enabled')),
            shape=band_e.get('shape'),
            frequency_range=band_e.get('frequencyRange'),
            frequency_hz=_int(band_e.get('frequency')) if band_e.get(
                'frequency') is not None else None,
            gain_db=_float(band_e.get('gain')),
            q=_float(band_e.get('qFactor')),
        )
    except Exception as exc:  # noqa: BLE001
        result.note_error(f'fairlight.master.eq.band[{band}]', exc)


def _apply_eq_band(conn, *, source, channel, band_e, result, label):
    """Send one CEBP packet with all observed band fields."""
    band = _int(band_e.get('index'))
    try:
        set_fairlight_eq_band(
            conn,
            source=source, channel=channel, band=band,
            enabled=_bool(band_e.get('enabled')),
            shape=band_e.get('shape'),
            frequency_range=band_e.get('frequencyRange'),
            frequency_hz=_int(band_e.get('frequency')) if band_e.get(
                'frequency') is not None else None,
            gain_db=_float(band_e.get('gain')),
            q=_float(band_e.get('qFactor')),
        )
    except Exception as exc:  # noqa: BLE001
        result.note_error(f'{label}.band[{band}]', exc)


def _apply_dynamics_block(conn, *, source, channel, dyn_e, result, label):
    """Apply the Compressor / Limiter / Expander children of a
    ``DynamicsProcessor`` element (CICP / CILP / CIXP). Each block is
    optional; the master has Compressor + Limiter, strips add Expander."""
    if dyn_e is None:
        return
    comp = dyn_e.find('Compressor')
    if comp is not None:
        _safe(f'{label}.compressor', result, set_fairlight_compressor,
              conn, source=source, channel=channel,
              enabled=_bool(comp.get('enabled')),
              threshold_db=_float(comp.get('threshold')),
              ratio=_float(comp.get('ratio')),
              attack_ms=_float(comp.get('attack')),
              hold_ms=_float(comp.get('hold')),
              release_ms=_float(comp.get('release')))
    lim = dyn_e.find('Limiter')
    if lim is not None:
        _safe(f'{label}.limiter', result, set_fairlight_limiter,
              conn, source=source, channel=channel,
              enabled=_bool(lim.get('enabled')),
              threshold_db=_float(lim.get('threshold')),
              attack_ms=_float(lim.get('attack')),
              hold_ms=_float(lim.get('hold')),
              release_ms=_float(lim.get('release')))
    exp = dyn_e.find('Expander')
    if exp is not None:
        _safe(f'{label}.expander', result, set_fairlight_expander,
              conn, source=source, channel=channel,
              enabled=_bool(exp.get('enabled')),
              mode=('gate' if _bool(exp.get('gateMode')) else 'expander'),
              threshold_db=_float(exp.get('threshold')),
              range_db=_float(exp.get('range')),
              ratio=_float(exp.get('ratio')),
              attack_ms=_float(exp.get('attack')),
              hold_ms=_float(exp.get('hold')),
              release_ms=_float(exp.get('release')))


def _apply_settings_flags(conn, root, result):
    settings = root.find('Settings')
    if settings is None:
        return
    ftb_enabled = _bool(settings.get('ftbEnabled'), True)
    _safe('settings.ftbEnabled', result,
          set_ftb_disabled, conn, disabled=not ftb_enabled)
    result.note_applied('settings.ftbEnabled')


def _apply_color_generators(conn, root, result):
    parent = root.find('ColorGenerators')
    if parent is None:
        return
    for cg in parent.findall('ColorGenerator'):
        idx = _int(cg.get('index'))
        _safe(f'color_gen[{idx}].hue', result,
              set_color_generator_hue, conn,
              generator=idx, hue=_float(cg.get('hue')))
        _safe(f'color_gen[{idx}].saturation', result,
              set_color_generator_saturation, conn,
              generator=idx, saturation=_float(cg.get('saturation')))
        _safe(f'color_gen[{idx}].luma', result,
              set_color_generator_luma, conn,
              generator=idx, luma=_float(cg.get('luma')))
    result.note_applied('color_generators')


def _apply_auxiliaries(conn, root, result):
    parent = root.find('Auxiliaries')
    if parent is None:
        return
    for aux in parent.findall('Auxiliary'):
        # XML id is 8001 + aux_index.
        aux_id = _int(aux.get('id'))
        idx = aux_id - 8001
        if idx < 0:
            continue
        source = _int(aux.get('input'))
        _safe(f'aux[{idx}]', result, set_aux_output,
              conn, aux=idx, source=source)
    result.note_applied('aux_outputs')


def _apply_hyperdecks(conn, root, result):
    """Restore each ``<HyperDeck>`` binding (network address + switcher input)
    via CXMS. Configured slots are bound; slots saved as 0.0.0.0 are cleared,
    so the switcher ends up matching the profile exactly. auto-roll /
    frame-delay aren't on the wire yet (not written)."""
    parent = root.find('HyperDecks')
    if parent is None:
        return
    n = 0
    for e in parent.findall('HyperDeck'):
        slot = _int(e.get('id'), -1)
        if slot < 0:
            continue
        ip = (e.get('networkAddress') or '0.0.0.0').strip()
        inp = _int(e.get('input'))
        _safe(f'hyperdeck[{slot}]', result, set_hyperdeck_settings,
              conn, slot=slot, network_address=ip, switcher_input=inp)
        if ip and ip != '0.0.0.0':
            n += 1
    result.note_applied(f'hyperdecks ({n})')


def _apply_transition(conn, root, result, opts):
    """Apply transition state for every ``<MixEffectBlock>`` the
    connected switcher has, each gated by its ``opts.mes[me]`` block's
    two independent flags:

    - ``mes[me].transition_style``: the ``<TransitionStyle>``
      element and all five per-style sub-elements (Mix/Dip/Wipe/
      Stinger/DVE Parameters).
    - ``mes[me].next_transition``: the ``<NextTransition>``
      element (the BG/Key1-4 selection mask).

    Each runs independently so the dialog can restore one without
    the other (matching what Software Control's Save dialog cells
    suggest)."""
    did_style = did_next = False
    for me, block in _iter_me_blocks(conn, root):
        me_opts = opts.me_options(me)
        _apply_transition_block(conn, me, block, me_opts, result)
        did_style |= me_opts.transition_style
        did_next |= me_opts.next_transition
    if did_style and did_next:
        result.note_applied('transition')
    elif did_style:
        result.note_applied('transition_style')
    elif did_next:
        result.note_applied('next_transition')


def _apply_transition_block(conn, me, block, me_opts, result):
    """One M/E's transition restore — see ``_apply_transition``."""
    ts = block.find('TransitionStyle')
    if me_opts.transition_style and ts is not None:
        style_name = ts.get('style', 'Mix')
        style_int = TRANS_STYLE_TO_INT.get(style_name, 0)
        _safe(f'me[{me}].transition.style', result,
              set_transition_style, conn, style=style_int, me=me)

        # Per-style settings.
        mix = ts.find('MixParameters')
        if mix is not None:
            _safe(f'me[{me}].mix_rate', result,
                  set_mix_rate, conn,
                  rate_str=str(_int(mix.get('rate'), 25)), me=me)

        dip = ts.find('DipParameters')
        if dip is not None:
            _safe(f'me[{me}].dip_rate', result,
                  set_dip_rate, conn,
                  rate_str=str(_int(dip.get('rate'), 25)), me=me)
            _safe(f'me[{me}].dip_source', result,
                  set_dip_source, conn,
                  source=_int(dip.get('input')), me=me)

        wipe = ts.find('WipeParameters')
        if wipe is not None:
            _safe(f'me[{me}].wipe_rate', result,
                  set_wipe_rate, conn,
                  rate_str=str(_int(wipe.get('rate'), 25)), me=me)
            wp_name = wipe.get('pattern', 'LeftToRightBar')
            _safe(f'me[{me}].wipe_pattern', result,
                  set_wipe_pattern, conn,
                  pattern=WIPE_PATTERN_TO_INT.get(wp_name, 0), me=me)
            _safe(f'me[{me}].wipe_symmetry', result,
                  set_wipe_symmetry, conn,
                  symmetry=_float(wipe.get('symmetry'), 50.0), me=me)
            _safe(f'me[{me}].wipe_softness', result,
                  set_wipe_softness, conn,
                  softness=_float(wipe.get('borderSoftness'), 0.0), me=me)
            _safe(f'me[{me}].wipe_width', result,
                  set_wipe_width, conn,
                  width=_float(wipe.get('borderWidth'), 0.0), me=me)
            _safe(f'me[{me}].wipe_position_x', result,
                  set_wipe_position_x, conn,
                  position_x=_float(wipe.get('xPosition'), 0.5), me=me)
            _safe(f'me[{me}].wipe_position_y', result,
                  set_wipe_position_y, conn,
                  position_y=_float(wipe.get('yPosition'), 0.5), me=me)
            _safe(f'me[{me}].wipe_reverse', result,
                  set_wipe_reverse, conn,
                  reverse=_bool(wipe.get('reverseDirection')), me=me)
            _safe(f'me[{me}].wipe_flip_flop', result,
                  set_wipe_flip_flop, conn,
                  flip_flop=_bool(wipe.get('flipFlip')), me=me)
            _safe(f'me[{me}].wipe_fill_source', result,
                  set_wipe_fill_source, conn,
                  source=_int(wipe.get('borderInput')), me=me)

        sting = ts.find('StingerParameters')
        if sting is not None:
            # source attr is the 1-indexed media player number, written
            # symbolically as "MediaPlayerN". The wire field
            # (StingerSettingsCommand.mediaplayer) is also 1-indexed, so
            # we pass the index through unchanged via set_stinger_source.
            src_str = sting.get('source', '')
            mp_idx = None
            if src_str.startswith('MediaPlayer'):
                try:
                    mp_idx = int(src_str[len('MediaPlayer'):])
                except ValueError:
                    pass
            else:
                try:
                    mp_idx = int(src_str)
                except ValueError:
                    pass
            if mp_idx is not None:
                _safe(f'me[{me}].stinger.source', result,
                      set_stinger_source, conn, source=mp_idx, me=me)
            _safe(f'me[{me}].stinger.clip', result,
                  set_stinger_clip, conn,
                  clip=_float(sting.get('clip'), 50.0), me=me)
            _safe(f'me[{me}].stinger.gain', result,
                  set_stinger_gain, conn,
                  gain=_float(sting.get('gain'), 70.0), me=me)
            _safe(f'me[{me}].stinger.invert_key', result,
                  set_stinger_invert_key, conn,
                  invert_key=_bool(sting.get('invert')), me=me)
            _safe(f'me[{me}].stinger.pre_multiplied', result,
                  set_stinger_pre_multiplied, conn,
                  pre_multiplied=_bool(sting.get('preMultipliedKey')), me=me)
            _safe(f'me[{me}].stinger.clip_duration', result,
                  set_stinger_clip_duration, conn,
                  clip_duration=str(_int(sting.get('clipDuration'), 73)),
                  me=me)
            _safe(f'me[{me}].stinger.trigger_point', result,
                  set_stinger_trigger_point, conn,
                  trigger_point=str(_int(sting.get('triggerPoint'), 34)),
                  me=me)
            _safe(f'me[{me}].stinger.mix_rate', result,
                  set_stinger_mix_rate, conn,
                  rate_str=str(_int(sting.get('mixRate'), 5)), me=me)
            _safe(f'me[{me}].stinger.pre_roll', result,
                  set_stinger_pre_roll, conn,
                  pre_roll=str(_int(sting.get('preroll'), 0)), me=me)

        dvet = ts.find('DVEParameters')
        if dvet is not None:
            _safe(f'me[{me}].dve_t.rate', result,
                  set_dve_rate, conn,
                  rate_str=str(_int(dvet.get('rate'), 25)), me=me)
            _safe(f'me[{me}].dve_t.fill_source', result,
                  set_dve_fill_source, conn,
                  source=_int(dvet.get('fillSource')), me=me)
            _safe(f'me[{me}].dve_t.key_source', result,
                  set_dve_key_source, conn,
                  source=_int(dvet.get('keySource')), me=me)
            _safe(f'me[{me}].dve_t.enable_key', result,
                  set_dve_enable_key, conn,
                  enable_key=_bool(dvet.get('enableKey')), me=me)
            _safe(f'me[{me}].dve_t.clip', result,
                  set_dve_clip, conn,
                  clip=_float(dvet.get('clip'), 50.0), me=me)
            _safe(f'me[{me}].dve_t.gain', result,
                  set_dve_gain, conn,
                  gain=_float(dvet.get('gain'), 70.0), me=me)
            _safe(f'me[{me}].dve_t.invert_key', result,
                  set_dve_invert_key, conn,
                  invert_key=_bool(dvet.get('invertKey')), me=me)
            _safe(f'me[{me}].dve_t.pre_multiplied', result,
                  set_dve_pre_multiplied, conn,
                  pre_multiplied=_bool(dvet.get('preMultipliedKey')), me=me)
            # effect → wire int via the reverse enum map. Unknown names are
            # left unapplied (no bare int(name) coercion — that path turned
            # every symbolic effect like "PushBottom" into a ValueError and
            # silently dropped it).
            eff_int = DVE_EFFECT_TO_INT.get(dvet.get('effect', ''))
            if eff_int is not None:
                _safe(f'me[{me}].dve_t.style', result,
                      set_dve_style, conn, style=eff_int, me=me)
            _safe(f'me[{me}].dve_t.reverse', result,
                  set_dve_reverse, conn,
                  reverse=_bool(dvet.get('reverseDirection')), me=me)
            _safe(f'me[{me}].dve_t.flip_flop', result,
                  set_dve_flip_flop, conn,
                  flip_flop=_bool(dvet.get('flipFlop')), me=me)

    # Next-transition selection — set the mask absolutely in ONE packet.
    # The earlier implementation toggled each differing bit separately
    # which raced: each ``toggle_transition_*`` op re-reads mixerstate
    # and packs the full mask, but mixerstate doesn't reflect prior
    # toggles until the ATEM round-trips a TrSS — back-to-back toggles
    # erase each other. Use the absolute-set op to avoid the race.
    if me_opts.next_transition:
        nt = block.find('NextTransition')
        if nt is not None:
            target = _str_to_next_transition(nt.get('selection', 'Background'))
            _safe(f'me[{me}].next_transition', result,
                  set_next_transition_layers, conn, me=me, **target)


def _apply_fly_keyframe(conn, me, k, kf_e, which, result, ctx):
    """Restore one fly keyframe (A/B) via the SFKF snapshot path: drive the
    live USK DVE geometry to the keyframe's values, then snapshot it into
    keyframe A or B (the ATEM exposes no direct keyframe-geometry write).

    ON-AIR VISIBLE — the live key moves through the keyframe geometry. The
    caller runs this BEFORE the live <DVEParameters> + <FlyParameters>
    blocks, which then restore the intended live geometry over the top."""
    for xml_a, op, kw in _FLY_KEYFRAME_GEOMETRY:
        v = kf_e.get(xml_a)
        if v is not None:
            _safe(f'{ctx}.kf{which}.{xml_a}', result, op,
                  conn, keyer=k, me=me, **{kw: _float(v)})
    _safe(f'{ctx}.kf{which}.snapshot', result, set_keyer_fly_keyframe,
          conn, keyer=k, keyframe=which.lower(), me=me)


def _apply_upstream_keyers(conn, root, result, opts):
    """Apply USK state for every ``<MixEffectBlock>`` the connected
    switcher has. ``opts.mes[me].usk[i]`` gates the Key element with
    ``index="i"`` in that M/E's block — keys whose flag is False are
    skipped entirely (no type, sources, mask, luma, chroma, pattern,
    DVE, fly setters fire). Indices outside the ``usk`` list are also
    skipped."""
    kept_by_me = {}
    for me, block in _iter_me_blocks(conn, root):
        me_opts = opts.me_options(me)
        keys_e = block.find('Keys')
        if keys_e is None:
            continue
        if _apply_usk_block(conn, me, keys_e, me_opts, opts, result):
            kept_by_me[me] = [str(i + 1) for i, on in enumerate(me_opts.usk)
                              if on]
    if kept_by_me:
        # Report which keyer indices ran so the result panel shows
        # "usk (1, 3, 4)" rather than a vague "upstream_keyers" —
        # per-M/E prefixed once more than one M/E was touched.
        if set(kept_by_me) == {0}:
            result.note_applied(
                f'upstream_keyers ({", ".join(kept_by_me[0])})')
        else:
            parts = [f'M/E{me + 1}: {", ".join(kept)}'
                     for me, kept in sorted(kept_by_me.items())]
            result.note_applied(f'upstream_keyers ({"; ".join(parts)})')


def _apply_usk_block(conn, me, keys_e, me_opts, opts, result) -> int:
    """Apply one M/E's ``<Keys>`` children; returns the number of keys
    applied. ``opts`` (the full ApplyOptions) is threaded through for
    the global ``restore_fly_keyframes`` gate."""
    applied = 0
    for key_e in keys_e.findall('Key'):
        k = _int(key_e.get('index'))
        # Per-keyer gate. If outside the opts list (rare) or False,
        # skip the entire key.
        if k < 0 or k >= len(me_opts.usk) or not me_opts.usk[k]:
            continue
        applied += 1
        ctx = f'me[{me}].usk[{k}]'

        # Type FIRST so subsequent params target the right config.
        type_int = USK_TYPE_TO_INT.get(key_e.get('type', 'Luma'), 0)
        _safe(f'{ctx}.type', result, set_usk_type,
              conn, keyer=k, key_type=type_int, me=me)
        _safe(f'{ctx}.fill', result, set_usk_fill_source,
              conn, keyer=k, source=_int(key_e.get('inputFill')), me=me)
        _safe(f'{ctx}.cut', result, set_usk_key_source,
              conn, keyer=k, source=_int(key_e.get('inputCut')), me=me)
        _safe(f'{ctx}.on_air', result, set_usk_on_air,
              conn, keyer=k, enabled=_bool(key_e.get('onAir')), me=me)

        # Mask
        _safe(f'{ctx}.mask_enabled', result, set_usk_mask_enabled,
              conn, keyer=k, enabled=_bool(key_e.get('masked')), me=me)
        for xml_a, op_name, kw in _USK_MASK_EDGES:
            _safe(f'{ctx}.{xml_a}', result, _OPS[op_name],
                  conn, keyer=k, me=me, **{kw: _float(key_e.get(xml_a))})

        # Luma — bundles all four fields into one CKLm packet.
        luma = key_e.find('LumaParameters')
        if luma is not None:
            _safe(f'{ctx}.luma', result, configure_usk_luma,
                  conn, keyer=k, me=me,
                  clip=_float(luma.get('clip')),
                  gain=_float(luma.get('gain')),
                  pre_multiplied=_bool(luma.get('preMultiplied')),
                  invert=_bool(luma.get('inverse')))

        # Pattern
        pat = key_e.find('PatternParameters')
        if pat is not None:
            pname = pat.get('style', 'LeftToRightBar')
            _safe(f'{ctx}.pattern.style', result, set_usk_pattern_style,
                  conn, keyer=k, me=me,
                  pattern=WIPE_PATTERN_TO_INT.get(pname, 0))
            _safe(f'{ctx}.pattern.invert', result, set_usk_pattern_invert,
                  conn, keyer=k, me=me, invert=_bool(pat.get('inverse')))
            for xml_a, op_name, kw, default in _USK_PATTERN_FLOATS:
                _safe(f'{ctx}.pattern.{kw}', result, _OPS[op_name],
                      conn, keyer=k, me=me,
                      **{kw: _float(pat.get(xml_a), default)})

        # Fly keyframes (A/B) — restore via SFKF snapshot, gated behind
        # opts.restore_fly_keyframes (default off; on-air visible). Done
        # BEFORE the DVE + FlyParameters blocks so those restore the live
        # geometry over the top. Only fires when the keyframe element exists.
        if opts.restore_fly_keyframes:
            fly_kf = key_e.find('FlyParameters')
            if fly_kf is not None:
                for which in ('A', 'B'):
                    kf_e = fly_kf.find('KeyFrame' + which)
                    if kf_e is not None:
                        _apply_fly_keyframe(conn, me, k, kf_e, which,
                                            result, ctx)

        # DVE
        dve = key_e.find('DVEParameters')
        if dve is not None:
            _safe(f'{ctx}.dve.masked', result, set_usk_dve_masked,
                  conn, keyer=k, me=me,
                  masked=_bool(dve.get('maskEnabled')))
            for xml_a, op_name, kw in _USK_DVE_MASK_EDGES:
                _safe(f'{ctx}.dve.{xml_a}', result, _OPS[op_name],
                      conn, keyer=k, me=me, **{kw: _float(dve.get(xml_a))})
            for xml_a, op_name, kw in _USK_DVE_BOOLS:
                _safe(f'{ctx}.dve.{kw}', result, _OPS[op_name],
                      conn, keyer=k, me=me, **{kw: _bool(dve.get(xml_a))})
            for xml_a, op_name, kw in _USK_DVE_FLOATS:
                _safe(f'{ctx}.dve.{kw}', result, _OPS[op_name],
                      conn, keyer=k, me=me, **{kw: _float(dve.get(xml_a))})

        # Chroma — only if we set type=Chroma above (else the values
        # are latent state but atemwire stores chroma bare so it doesn't
        # really matter; sending always is harmless). Skip any XML
        # attr that isn't present so we don't clobber with a 0.0.
        chroma = key_e.find('AdvancedChromaParameters')
        if chroma is not None:
            for xml_a, op_name, kw in _USK_CHROMA_FLOATS:
                v = chroma.get(xml_a)
                if v is None:
                    continue
                _safe(f'{ctx}.chroma.{xml_a}', result, _OPS[op_name],
                      conn, keyer=k, me=me, **{kw: _float(v)})
            for xml_a, op_name, kw in _USK_CHROMA_PERCENTS:
                v = chroma.get(xml_a)
                if v is None:
                    continue
                _safe(f'{ctx}.chroma.{xml_a}', result, _OPS[op_name],
                      conn, keyer=k, me=me, **{kw: _float(v) / 100.0})
            sat = chroma.get('saturation')
            if sat is not None:
                _safe(f'{ctx}.chroma.saturation', result,
                      set_usk_chroma_saturation,
                      conn, keyer=k, me=me, saturation=_float(sat) / 100.0)
            # Colourpicker cursor: size + position via the live ops
            # (inverse of save's ``(v-0.5)*32`` / ``*18`` and ``*100``),
            # then the sampled colour LAST so a position-triggered
            # re-sample doesn't clobber the restored colour.
            cs = chroma.get('cursorSize')
            if cs is not None:
                _safe(f'{ctx}.chroma.cursor_size', result,
                      set_usk_chroma_sample_size,
                      conn, keyer=k, me=me, size=_float(cs) / 100.0)
            cx, cy = chroma.get('cursorXPosition'), chroma.get('cursorYPosition')
            if cx is not None and cy is not None:
                _safe(f'{ctx}.chroma.cursor_position', result,
                      set_usk_chroma_sample_position, conn, keyer=k, me=me,
                      x=_float(cx) / 32.0 + 0.5, y=_float(cy) / 18.0 + 0.5)
            sy, scb, scr = (chroma.get('sampledY'), chroma.get('sampledCb'),
                            chroma.get('sampledCr'))
            if sy is not None and scb is not None and scr is not None:
                _safe(f'{ctx}.chroma.sampled_color', result,
                      set_usk_chroma_sampled_color, conn, keyer=k, me=me,
                      y=_float(sy), cb=_float(scb), cr=_float(scr))

        # FlyParameters — enable + geometry (position/size/rotation/rate via
        # the USK DVE ops). This restores the live DVE state; keyframe A/B
        # were already snapshotted above (when opts.restore_fly_keyframes is
        # on) and are overwritten here with the intended live geometry.
        fly = key_e.find('FlyParameters')
        if fly is not None:
            _safe(f'{ctx}.fly_enabled', result, set_usk_fly_enabled,
                  conn, keyer=k, me=me, enabled=_bool(fly.get('enabled')))
            for xml_a, op, kw in _USK_FLY_GEOMETRY:
                v = fly.get(xml_a)
                if v is not None:
                    _safe(f'{ctx}.fly.{xml_a}', result, op,
                          conn, keyer=k, me=me, **{kw: _float(v)})
            rate = fly.get('rate')
            if rate is not None:
                _safe(f'{ctx}.fly.rate', result, set_usk_dve_rate,
                      conn, keyer=k, me=me, rate_str=str(_int(rate, 25)))
    return applied


def _apply_downstream_keys(conn, root, result):
    parent = root.find('DownstreamKeys')
    if parent is None:
        return
    for d in parent.findall('DownstreamKey'):
        idx = _int(d.get('index'))
        ctx = f'dsk[{idx}]'
        _safe(f'{ctx}.fill', result, set_dsk_fill_source,
              conn, source=_int(d.get('fillSource')), dsk_idx=idx)
        _safe(f'{ctx}.key', result, set_dsk_key_source,
              conn, source=_int(d.get('keySource')), dsk_idx=idx)
        _safe(f'{ctx}.rate', result, set_dsk_rate,
              conn, rate_str=str(_int(d.get('rate'), 25)), dsk_idx=idx)
        _safe(f'{ctx}.mask_enabled', result, set_dsk_mask_enabled,
              conn, enabled=_bool(d.get('maskEnabled')), dsk_idx=idx)
        for xml_a, op_name, kw in _DSK_MASK_EDGES:
            _safe(f'{ctx}.{xml_a}', result, _OPS[op_name],
                  conn, dsk_idx=idx, **{kw: _float(d.get(xml_a))})
        # gain_block bundles four fields into one CDsG packet.
        _safe(f'{ctx}.gain_block', result, configure_dsk_gain,
              conn, dsk_idx=idx,
              clip=_float(d.get('clip')),
              gain=_float(d.get('gain')),
              pre_multiplied=_bool(d.get('preMultipliedKey')),
              invert=_bool(d.get('invert')))
        _safe(f'{ctx}.on_air', result, set_dsk_on_air,
              conn, enabled=_bool(d.get('onAir')), dsk_idx=idx)
        _safe(f'{ctx}.tie', result, _OPS['set_dsk_tie'],
              conn, dsk_idx=idx, tie=_bool(d.get('tie')))
    result.note_applied('downstream_keys')


def _apply_fade_to_black(conn, root, result, opts):
    applied = False
    for me, block in _iter_me_blocks(conn, root):
        if not opts.me_options(me).fade_to_black:
            continue
        ftb = block.find('FadeToBlack')
        if ftb is None:
            continue
        _safe(f'me[{me}].ftb_rate', result,
              set_ftb_rate, conn,
              rate_str=str(_int(ftb.get('rate'), 25)), me=me)
        applied = True
    if applied:
        result.note_applied('fade_to_black')


def _apply_media_players(conn, root, result):
    parent = root.find('MediaPlayers')
    if parent is None:
        return
    for mp in parent.findall('MediaPlayer'):
        idx = _int(mp.get('index'))
        st = mp.get('sourceType', 'Still')
        slot = _int(mp.get('sourceIndex'))
        if st == 'Clip':
            _safe(f'mp[{idx}].clip', result,
                  set_media_player_clip, conn, player=idx, slot=slot)
        else:
            _safe(f'mp[{idx}].still', result,
                  set_media_player_still, conn, player=idx, slot=slot)
    result.note_applied('media_players')


def _apply_program_preview(conn, root, result, opts):
    """Apply Program / Preview separately for every ``<MixEffectBlock>``
    the connected switcher has. ``opts.mes[me].program`` and
    ``opts.mes[me].preview`` independently gate each side. Preview
    fires first so a paired program+preview restore lands on the
    intended bus configuration."""
    did_prog = did_prev = False
    for me, block in _iter_me_blocks(conn, root):
        me_opts = opts.me_options(me)
        prev = block.find('Preview')
        if me_opts.preview and prev is not None:
            _safe(f'me[{me}].preview', result,
                  set_preview, conn,
                  source=_int(prev.get('input')), me=me)
            did_prev = True
        prog = block.find('Program')
        if me_opts.program and prog is not None:
            _safe(f'me[{me}].program', result,
                  set_program, conn,
                  source=_int(prog.get('input')), me=me)
            did_prog = True
    if did_prog and did_prev:
        result.note_applied('program_preview')
    elif did_prog:
        result.note_applied('program')
    elif did_prev:
        result.note_applied('preview')


# =============================================================================
# Macro restore
# -----------------------------------------------------------------------------
# Macros are restored by uploading their LE-format bytecode directly to the
# macro store via the file-transfer protocol (FTSD/FTCD/FTDa/FTFD/FTDC,
# store=0xFFFF, mode=0x0300). The encode side lives in
# atemwire/macrotransfer.py (encode_macro_bytecode + upload_macro_bytecode);
# this module just dispatches per-slot. See atemwire/docs/MACRO_FORMAT.md for the
# wire-level format.
#
# Earlier revisions used MSRc → replay each <Op> as a control command →
# MAct(stop) to record server-side. That worked but drove the live ATEM's
# program/USK/DSK state through every op of every macro during apply.
# Bytecode upload doesn't touch live state at all.
# =============================================================================


def _apply_macros(conn, root, result):
    """Restore each ``<Macro>`` in ``<MacroPool>`` by uploading its
    encoded bytecode directly to the macro slot via the file-transfer
    protocol (FTSD/FTCD/FTDa/FTFD/FTDC, store=0xFFFF, mode=0x0300).

    Per-op partial upload: each `<Op>` child is encoded individually
    via ``encode_single_op``. Ops the encoder doesn't recognise are
    dropped from that macro's bytecode and reported in the summary
    (Fairlight / VideoMode / etc. have no wire-format mapping yet);
    the remaining ops still upload, so the macro lands in its slot
    with most of its functionality intact. Operators chose this over
    a strict "fail-the-whole-macro" policy because the dropped ops are
    typically low-value side-effects (audio routing, video mode reset)
    that operators can re-add manually if needed; refusing the whole
    macro left the slot empty and the operator stranded.

    Outcome categories:
      * **restored** — macro landed in its slot, every op encoded.
      * **with dropped ops** — macro landed but some ops were dropped.
        Sub-case "metadata only": every op was dropped, fell back to
        MSRc → MAct(stop) so name + description still land.
      * **failed** — upload itself errored (FTDE, timeout). The slot
        is left in whatever state the previous content was; the user
        should investigate.

    No live ATEM state changes during the bytecode upload — the
    bytecode lands directly in the slot, so program/preview/USK/DSK
    don't twitch through the recorded 
    Empty `<Macro>` elements (no `<Op>` children at all) fall back to
    the same MSRc → MAct(stop) path, but aren't reported as drops
    because there were no ops to drop in the first place.

    Macros are still restored AFTER inputs/labels because the encoder
    accepts `<Op>` attributes carrying post-rename symbolic names
    (``Camera5`` etc.); the encoder resolves them at apply time.
    """
    from atemwire.macrotransfer import (
        encode_single_op,
        upload_macro_bytecode,
    )

    pool = root.find('MacroPool')
    if pool is None:
        return
    macros = pool.findall('Macro')
    if not macros:
        return  # empty macro pool: emit no result line

    n_total = len(macros)
    n_restored = 0  # count of macros that landed in slot (clean OR with drops)
    drops = []  # [(slot, name, dropped_op_ids, metadata_only)]
    failures = []  # [(slot, name, reason)]
    logger.info("Profile.apply restoring %d macro(s) via bytecode upload",
                n_total)

    protocol = getattr(conn, 'protocol', None)
    if protocol is None or not protocol.connected:
        logger.warning("Profile.apply macros: no connected protocol — "
                       "skipping all macros")
        result.note_skipped('macros', 'no connected protocol')
        return

    for m_e in macros:
        slot = _int(m_e.get('index'), -1)
        if slot < 0:
            continue
        name = m_e.get('name', '') or ''
        description = m_e.get('description', '') or ''
        op_elements = list(m_e.findall('Op'))

        # Per-op encode. Ops that fail are dropped and noted; the rest
        # are concatenated into the macro's bytecode.
        encoded_chunks = []
        dropped_ids = []
        for op_e in op_elements:
            op_dict = dict(op_e.attrib)
            op_dict['id'] = op_e.get('id', '')
            try:
                encoded_chunks.append(encode_single_op(op_dict))
            except ValueError as e:
                dropped_ids.append(op_dict['id'] or '<no-id>')
                logger.debug("macro slot %d: dropping op %r (%s)",
                             slot, op_dict['id'], e)

        # Three upload paths:
        #   (a) encoded_chunks non-empty → upload partial/full bytecode
        #   (b) encoded_chunks empty AND op_elements non-empty (everything
        #       dropped) → upload empty bytecode (metadata-only landing),
        #       flagged as drops
        #   (c) op_elements empty AND dropped empty (truly empty macro)
        #       → upload empty bytecode (metadata-only landing), NOT
        #       flagged as drops
        #
        # Both (b) and (c) used to fall through to MSRc → sleep →
        # MAct(STOP_RECORD). That works, but ATEM Software Control's
        # red record-border state is keyed off the MSRc / MAct cycle —
        # MSRc flips ``MRcS.is_recording`` to True for the 0.15s
        # window, and any connected SC client paints the red border.
        # BMD's "Restore from State" never uses MSRc/MAct for empty
        # macros; verified against a Wireshark capture of it which shows
        # FTSD(slot=N, len=0, mode=0x0300) → FTFD for the empty slot,
        # same wire path as a normal macro upload. Switching to empty
        # bytecode upload here matches that behaviour and stops the
        # spurious red border.
        metadata_only = not encoded_chunks
        try:
            bytecode = b''.join(encoded_chunks)  # b'' when metadata_only
            upload_macro_bytecode(protocol, slot, name, description,
                                  bytecode, timeout=10.0)
        except Exception as e:  # noqa: BLE001
            failures.append((slot, name, f'upload error: {e}'))
            logger.warning("macro slot %d (%r) upload failed: %s",
                           slot, name, e)
            continue

        n_restored += 1
        if dropped_ids:
            drops.append((slot, name, dropped_ids, metadata_only))
            logger.info(
                "macro slot %d (%r) %s with %d dropped op(s): %s",
                slot, name,
                'uploaded metadata only' if metadata_only else 'uploaded',
                len(dropped_ids),
                ', '.join(sorted(set(dropped_ids))))

    summary = _format_macro_summary(n_restored, n_total, drops, failures)
    logger.info("Profile.apply macros: %s", summary)
    if n_restored:
        result.note_applied(f'macros ({summary})')
    else:
        result.note_skipped('macros', summary)
    # Upload failures (FTDE / timeout — the macro did NOT land in its slot) are
    # genuine errors, distinct from dropped-op partial successes. Record them in
    # result.errors so callers that gate on it (e.g. a macro-only apply
    # job) correctly mark the job failed instead of silently
    # passing. Drops stay a partial success (the macro still landed).
    for slot, name, reason in failures:
        result.note_error('macros', RuntimeError(f"slot {slot} ({name!r}): {reason}"))


def _format_macro_summary(n_restored: int, n_total: int,
                          drops: list, failures: list) -> str:
    """Build the apply-result summary line for the macro section.

    Format:
      All success:  ``2/2 restored, 0 failed``
      With drops:   ``2/2 restored, 1 with dropped ops: slot 0 ('A')
                    dropped 1 op (FairlightX)``
      Failures:     ``1/2 restored, 1 failed: slot 5 ('B') — upload
                    error: <details>``
      Both:         drops section + failures section, comma-separated
      All-dropped:  drop entry has ``— metadata only`` suffix to
                    flag the slot has name+description but no 
    Note for operators: dropped ops mean the macro may behave
    differently than intended — re-record manually if the dropped op
    is functionally important.
    """
    if not drops and not failures:
        return f'{n_restored}/{n_total} restored, 0 failed'

    head = f'{n_restored}/{n_total} restored'
    sections = []

    if drops:
        drop_details = '; '.join(
            _format_drop_entry(slot, name, dropped_ids, metadata_only)
            for slot, name, dropped_ids, metadata_only in drops
        )
        sections.append(f'{len(drops)} with dropped ops: {drop_details}')

    if failures:
        fail_details = '; '.join(
            f"slot {slot} ({name!r}) — {reason}"
            for slot, name, reason in failures
        )
        sections.append(f'{len(failures)} failed: {fail_details}')

    return f'{head}, ' + ', '.join(sections)


def _format_drop_entry(slot: int, name: str,
                       dropped_ids: list, metadata_only: bool) -> str:
    n = len(dropped_ids)
    word = 'op' if n == 1 else 'ops'
    op_list = ', '.join(sorted(set(dropped_ids)))
    suffix = ' — metadata only' if metadata_only else ''
    return (f"slot {slot} ({name!r}) dropped {n} {word} "
            f"({op_list}){suffix}")


