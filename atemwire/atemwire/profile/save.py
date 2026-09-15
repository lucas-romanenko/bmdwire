# SPDX-License-Identifier: LGPL-3.0-only
"""
Save side — read mixerstate, build XML elements.

Each ``_build_<section>`` reads the relevant chunk of the ATEM's
mixerstate and emits matching ``<...>`` children under the root
``<Profile>`` element. ``download_media_pool_images`` is the
companion that captures the slot PNGs alongside the metadata XML.
"""

import logging
import xml.etree.ElementTree as ET
from typing import List

from atemwire.state import _dsk_count, me_count, me_keyer_count, decode_name
from atemwire.messages.color_generator import color_generator
from atemwire.messages.downstream_keyer import dsk_state
from atemwire.messages.fade_to_black import (
    ftb_active, ftb_disabled, ftb_rate,
)
from atemwire.messages.hyperdeck import hyperdeck_settings
from atemwire.messages.fairlight import (
    fairlight_compressor, fairlight_expander, fairlight_limiter,
    fairlight_master_compressor, fairlight_master_limiter,
)
from atemwire.messages.macros import macro_entry
from atemwire.messages.media import (
    mediaplayer_selected, mediaplayer_slot_info,
)
from atemwire.messages.switching import (
    aux_source, preview_source, program_source,
)
from atemwire.messages.system_info import sdi_3g_level, video_mode
from atemwire.messages.transition import (
    dip_rate, dip_source, dve_clip, dve_enable_key, dve_fill_source,
    dve_flip_flop, dve_gain, dve_invert_key, dve_key_source,
    dve_pre_multiplied, dve_rate, dve_reverse, dve_style, mix_rate,
    stinger_clip, stinger_clip_duration, stinger_gain,
    stinger_invert_key, stinger_pre_multiplied, stinger_pre_roll,
    stinger_rate, stinger_source, stinger_trigger_point,
    transition_position, transition_selection, transition_style,
    wipe_fill_source, wipe_flip_flop, wipe_pattern, wipe_position_x,
    wipe_position_y, wipe_rate, wipe_reverse, wipe_softness,
    wipe_symmetry, wipe_width,
)
from atemwire.messages.upstream_keyer import (
    usk_chroma, usk_dve, usk_fill_source, usk_fly, usk_fly_keyframe,
    usk_key_source, usk_luma, usk_mask, usk_on_air, usk_pattern, usk_type,
)
from atemwire.profile._common import (
    _resolve_mixerstate,
    _resolve_protocol_or_none,
    _set_attrs,
)
from atemwire.profile._enums import (
    DVE_EFFECT_NAMES,
    EQ_FREQ_RANGE_NAMES,
    EQ_SHAPE_NAMES,
    MIX_OPTION_NAMES,
    TRANS_STYLE_NAMES,
    USK_TYPE_NAMES,
    WIPE_PATTERN_NAMES,
)
from atemwire.profile._xml import (
    _ext_port_type_name,
    _fmt,
    _next_transition_to_str,
)
from atemwire.profile.options import SaveOptions  # noqa: F401  (referenced in docstrings/type hints)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MixEffectBlocks
# ---------------------------------------------------------------------------

def _build_mix_effect_blocks(root: ET.Element, mx: dict, opts) -> None:
    """Build the ``<MixEffectBlocks><MixEffectBlock>`` tree, one block
    per M/E the connected switcher has (``me_count``), each gated by
    its ``opts.mes[me]`` (``MixEffectOptions``) block:

    - ``mes[me].program`` → ``<Program>``
    - ``mes[me].preview`` → ``<Preview>``
    - ``mes[me].next_transition`` → ``<NextTransition>``
    - ``mes[me].transition_style`` → ``<TransitionStyle>`` + 5 per-style children
    - ``mes[me].usk[i]`` → ``<Key index="i">`` inside the ``<Keys>`` wrapper
    - ``mes[me].fade_to_black`` → ``<FadeToBlack>``

    A fully-deselected M/E emits no ``<MixEffectBlock>`` at all; when
    every M/E is deselected the ``<MixEffectBlocks>`` wrapper itself
    is omitted (mirroring the per-section gates elsewhere).
    """
    selected = [me for me in range(me_count(mx))
                if opts.me_options(me).any_selected()]
    if not selected:
        return
    blocks = ET.SubElement(root, 'MixEffectBlocks')
    for me in selected:
        _build_mix_effect_block(blocks, mx, me, opts.me_options(me))


def _build_mix_effect_block(blocks: ET.Element, mx: dict, me: int,
                            me_opts) -> None:
    """Emit one ``<MixEffectBlock index="me">`` with each child element
    gated by the corresponding ``MixEffectOptions`` flag. The ``<Keys>``
    wrapper element is only emitted if at least one USK flag is True;
    the Key loop is sized to the M/E's real keyer count."""
    block = ET.SubElement(blocks, 'MixEffectBlock')
    block.set('index', str(me))

    if me_opts.program:
        prog = ET.SubElement(block, 'Program')
        _set_attrs(prog, input=program_source(mx, me))
    if me_opts.preview:
        prev = ET.SubElement(block, 'Preview')
        _set_attrs(prev, input=preview_source(mx, me))

    # Next transition selection — TrSS is keyed per M/E (Stage 4A), read
    # via the me index.
    if me_opts.next_transition:
        sel = transition_selection(mx, me)
        next_trans = ET.SubElement(block, 'NextTransition')
        _set_attrs(next_trans,
                   selection=_next_transition_to_str(sel),
                   nextSelection=_next_transition_to_str(sel))

    # Transition style block — covers <TransitionStyle> + the five
    # per-style parameter children.
    if me_opts.transition_style:
        trans_style = ET.SubElement(block, 'TransitionStyle')
        style_int = transition_style(mx, me)
        style_name = TRANS_STYLE_NAMES.get(style_int, 'Mix')
        pos = transition_position(mx, me)  # 0..1 from atemwire
        _set_attrs(trans_style,
                   style=style_name, nextStyle=style_name,
                   previewTransition=False,
                   transitionPosition=int(round(pos * 10000)))

        # Per-style params — emit all five regardless of which is active.
        mix_p = ET.SubElement(trans_style, 'MixParameters')
        _set_attrs(mix_p, rate=mix_rate(mx, me))

        dip_p = ET.SubElement(trans_style, 'DipParameters')
        _set_attrs(dip_p, rate=dip_rate(mx, me),
                   input=dip_source(mx, me))

        wipe_p = ET.SubElement(trans_style, 'WipeParameters')
        wp = WIPE_PATTERN_NAMES[wipe_pattern(mx, me) % len(WIPE_PATTERN_NAMES)]
        _set_attrs(wipe_p,
                   rate=wipe_rate(mx, me),
                   pattern=wp,
                   symmetry=wipe_symmetry(mx, me),
                   xPosition=wipe_position_x(mx, me),
                   yPosition=wipe_position_y(mx, me),
                   reverseDirection=wipe_reverse(mx, me),
                   flipFlip=wipe_flip_flop(mx, me),
                   borderInput=wipe_fill_source(mx, me),
                   borderWidth=wipe_width(mx, me),
                   borderSoftness=wipe_softness(mx, me))

        # Stinger. Note: stinger_source returns the wire mediaplayer
        # index (1-indexed, NOT a global source ID). Software Control's
        # XML uses the symbolic "MediaPlayerN" for it.
        stinger_p = ET.SubElement(trans_style, 'StingerParameters')
        mp_idx = stinger_source(mx, me)
        stinger_source_name = (f'MediaPlayer{mp_idx}'
                               if 1 <= mp_idx <= 4 else str(mp_idx))
        _set_attrs(stinger_p,
                   source=stinger_source_name,
                   preMultipliedKey=stinger_pre_multiplied(mx, me),
                   clip=stinger_clip(mx, me),
                   gain=stinger_gain(mx, me),
                   invert=stinger_invert_key(mx, me),
                   clipDuration=stinger_clip_duration(mx, me),
                   triggerPoint=stinger_trigger_point(mx, me),
                   mixRate=stinger_rate(mx, me),
                   preroll=stinger_pre_roll(mx, me))

        # DVE transition. effect is the 16-entry hardware enum (Squeeze /
        # Push, wire 16..31). Emit the symbolic name — never a bare int,
        # which ASC can't parse. A value outside the table (other model /
        # unset field) falls back to the lowest valid token.
        dve_p = ET.SubElement(trans_style, 'DVEParameters')
        dve_eff = dve_style(mx, me)
        dve_eff_name = DVE_EFFECT_NAMES.get(dve_eff, 'SqueezeTopLeft')
        _set_attrs(dve_p,
                   rate=dve_rate(mx, me),
                   logoRate=dve_rate(mx, me),
                   reverseDirection=dve_reverse(mx, me),
                   flipFlop=dve_flip_flop(mx, me),
                   effect=dve_eff_name,
                   fillSource=dve_fill_source(mx, me),
                   keySource=dve_key_source(mx, me),
                   enableKey=dve_enable_key(mx, me),
                   preMultipliedKey=dve_pre_multiplied(mx, me),
                   clip=dve_clip(mx, me),
                   gain=dve_gain(mx, me),
                   invertKey=dve_invert_key(mx, me))

    # Keys — emit the wrapper only when at least one keyer is selected;
    # each individual <Key index="k"> gated by me_opts.usk[k]. Loop is
    # sized to this M/E's real keyer count (4 on every 1-M/E model;
    # multi-M/E Constellations carry fewer keyers on the upper M/Es).
    if any(me_opts.usk):
        keys = ET.SubElement(block, 'Keys')
        for k in range(me_keyer_count(mx, me)):
            if k < len(me_opts.usk) and me_opts.usk[k]:
                _build_key(keys, mx, me, k)

    # Fade-to-black
    if me_opts.fade_to_black:
        ftb = ET.SubElement(block, 'FadeToBlack')
        _set_attrs(ftb,
                   rate=ftb_rate(mx, me),
                   isFullyBlack=ftb_active(mx, me))


def _build_key(parent: ET.Element, mx: dict, me: int, k: int) -> None:
    type_int = usk_type(mx, me, k)
    mask = usk_mask(mx, me, k)
    luma = usk_luma(mx, me, k)
    chroma = usk_chroma(mx, me, k)
    pattern = usk_pattern(mx, me, k)
    dve = usk_dve(mx, me, k)
    fly = usk_fly(mx, me, k)

    key = ET.SubElement(parent, 'Key')
    _set_attrs(key, index=k,
               type=USK_TYPE_NAMES.get(type_int, 'Luma'),
               inputCut=usk_key_source(mx, me, k),
               inputFill=usk_fill_source(mx, me, k),
               onAir=usk_on_air(mx, me, k),
               masked=mask['mask_enabled'],
               maskTop=mask['mask_top'],
               maskBottom=mask['mask_bottom'],
               maskLeft=mask['mask_left'],
               maskRight=mask['mask_right'])

    luma_e = ET.SubElement(key, 'LumaParameters')
    _set_attrs(luma_e,
               preMultiplied=luma['luma_pre_multiplied'],
               clip=luma['luma_clip'],
               gain=luma['luma_gain'],
               inverse=luma['luma_invert'])

    chroma_e = ET.SubElement(key, 'AdvancedChromaParameters')
    cur = chroma['chroma_sample_position']
    sc = chroma['chroma_sampled_color']
    _set_attrs(chroma_e,
               foregroundLevel=chroma['chroma_foreground'] * 100.0,
               backgroundLevel=chroma['chroma_background'] * 100.0,
               keyEdge=chroma['chroma_key_edge'] * 100.0,          # was raw
               spillSuppress=chroma['chroma_spill'] * 100.0,
               flareSuppress=chroma['chroma_flare_suppression'] * 100.0,
               brightness=chroma['chroma_brightness'],
               contrast=chroma['chroma_contrast'],
               saturation=chroma['chroma_saturation'] * 100.0,
               red=chroma['chroma_red'],
               green=chroma['chroma_green'],
               blue=chroma['chroma_blue'],
               cursorXPosition=(cur['x'] - 0.5) * 32.0,
               cursorYPosition=(cur['y'] - 0.5) * 18.0,
               cursorSize=chroma['chroma_sample_size'] * 100.0,    # was raw
               sampledY=sc['y_raw'] / 10000.0,                     # was sc['y']
               sampledCb=sc['cb_raw'] / 10000.0,                   # was sc['cb']
               sampledCr=sc['cr_raw'] / 10000.0)                   # was sc['cr']

    pat_e = ET.SubElement(key, 'PatternParameters')
    _set_attrs(pat_e,
               style=WIPE_PATTERN_NAMES[pattern['pattern_style'] % len(WIPE_PATTERN_NAMES)],
               inverse=pattern['pattern_invert'],
               size=pattern['pattern_size'],
               symmetry=pattern['pattern_symmetry'],
               softness=pattern['pattern_softness'],
               xPosition=pattern['pattern_position_x'],
               yPosition=pattern['pattern_position_y'])

    dve_e = ET.SubElement(key, 'DVEParameters')
    _set_attrs(dve_e,
               maskEnabled=dve['dve_masked'],
               maskTop=dve['dve_top'],
               maskBottom=dve['dve_bottom'],
               maskLeft=dve['dve_left'],
               maskRight=dve['dve_right'],
               shadowEnabled=dve['dve_shadow'],
               lightSourceDirection=dve['dve_light_direction'],
               lightSourceAltitude=dve['dve_light_altitude'],
               borderEnabled=dve['dve_border_enabled'],
               borderStyle='None',
               borderBevelHue=dve['dve_border_hue'],
               borderBevelSaturation=dve['dve_border_saturation'],
               borderBevelLuma=dve['dve_border_luma'],
               borderWidthOut=dve['dve_border_outer_width'],
               borderWidthIn=dve['dve_border_inner_width'],
               borderSoftnessOut=dve['dve_border_outer_softness'],
               borderSoftnessIn=dve['dve_border_inner_softness'],
               borderBevelOpacity=dve['dve_border_opacity'],
               borderBevelPosition=dve['dve_border_bevel_position'],
               borderBevelSoftness=dve['dve_border_bevel_softness'])

    fly_e = ET.SubElement(key, 'FlyParameters')
    _set_attrs(fly_e,
               enabled=fly['fly_enabled'],
               xPosition=dve['dve_position_x'],
               yPosition=dve['dve_position_y'],
               xSize=dve['dve_size_x'],
               ySize=dve['dve_size_y'],
               rotation=dve['dve_rotation'],
               rate=dve['dve_rate'])
    # KeyFrameA / KeyFrameB — emitted from the real parsed KKFP geometry.
    # ATEM Software Control writes both; each is gated on that keyframe
    # being stored (usk_fly_keyframe returns None when it isn't). Attribute
    # names mirror apply._FLY_KEYFRAME_GEOMETRY so the round-trip is exact.
    for which in ('A', 'B'):
        kf_g = usk_fly_keyframe(mx, me, k, which)
        if kf_g is None:
            continue
        kf = ET.SubElement(fly_e, 'KeyFrame' + which)
        _set_attrs(kf,
                   xSize=kf_g['size_x'], ySize=kf_g['size_y'],
                   xPosition=kf_g['pos_x'], yPosition=kf_g['pos_y'],
                   rotation=kf_g['rotation'],
                   borderWidthOut=kf_g['border_outer_width'],
                   borderWidthIn=kf_g['border_inner_width'],
                   borderSoftnessOut=kf_g['border_outer_softness'],
                   borderSoftnessIn=kf_g['border_inner_softness'],
                   borderBevelSoftness=kf_g['border_bevel_softness'],
                   borderBevelPosition=kf_g['border_bevel_position'],
                   borderOpacity=kf_g['border_opacity'],
                   borderHue=kf_g['border_hue'],
                   borderSaturation=kf_g['border_saturation'],
                   borderLuma=kf_g['border_luma'],
                   borderLightSourceDirection=kf_g['light_direction'],
                   borderLightSourceAltitude=kf_g['light_altitude'],
                   maskTop=kf_g['mask_top'], maskBottom=kf_g['mask_bottom'],
                   maskLeft=kf_g['mask_left'], maskRight=kf_g['mask_right'])


# ---------------------------------------------------------------------------
# DownstreamKeys
# ---------------------------------------------------------------------------

def _build_downstream_keys(root: ET.Element, mx: dict) -> None:
    parent = ET.SubElement(root, 'DownstreamKeys')
    # One block per real DSK (_top → dkey-state entries → 1). The old
    # range(2) cap silently dropped DSK 3/4 on 4-DSK models.
    for idx in range(_dsk_count(mx)):
        d = dsk_state(mx, idx)
        e = ET.SubElement(parent, 'DownstreamKey')
        _set_attrs(e, index=idx,
                   fillSource=d['fill_source'],
                   keySource=d['key_source'],
                   rate=d['rate'],
                   maskEnabled=d['mask_enabled'],
                   maskTop=d['mask_top'],
                   maskBottom=d['mask_bottom'],
                   maskLeft=d['mask_left'],
                   maskRight=d['mask_right'],
                   preMultipliedKey=d['pre_multiplied'],
                   clip=d['clip'],
                   gain=d['gain'],
                   invert=d['invert_key'],
                   onAir=d['on_air'],
                   tie=d['tie'])


# ---------------------------------------------------------------------------
# ColorGenerators
# ---------------------------------------------------------------------------

def _build_color_generators(root: ET.Element, mx: dict) -> None:
    parent = ET.SubElement(root, 'ColorGenerators')
    for idx in range(2):
        cg = color_generator(mx, idx)
        e = ET.SubElement(parent, 'ColorGenerator')
        _set_attrs(e, index=idx,
                   hue=cg['hue'],
                   saturation=cg['saturation'],
                   luma=cg['luma'])


# ---------------------------------------------------------------------------
# Auxiliaries
# ---------------------------------------------------------------------------

def _build_auxiliaries(root: ET.Element, mx: dict) -> None:
    parent = ET.SubElement(root, 'Auxiliaries')
    aux_dict = mx.get('aux-output-source', {})
    for idx in sorted(aux_dict.keys()):
        e = ET.SubElement(parent, 'Auxiliary')
        _set_attrs(e, id=8001 + idx, input=aux_source(mx, idx))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _build_settings(root: ET.Element, mx: dict) -> None:
    settings = ET.SubElement(root, 'Settings')
    _set_attrs(settings,
               abDirect=False,
               cameraAux=-1,
               SDI3GOutputLevel=sdi_3g_level(mx),
               ftbEnabled=not ftb_disabled(mx))

    # MultiViewVideoModes — static lookup table emitted as Software
    # Control does. Not stored on the switcher.
    mvm = ET.SubElement(settings, 'MultiViewVideoModes')
    for core, mv in _DEFAULT_MULTIVIEW_VIDEO_MODES:
        e = ET.SubElement(mvm, 'MultiViewVideoMode')
        _set_attrs(e, coreVideoMode=core, multiViewVideoMode=mv)

    # Talkback / EngineeringTalkback — atemwire doesn't expose state;
    # emit defaults that match the reference (everything unmuted).
    for which in ('Talkback', 'EngineeringTalkback'):
        tb = ET.SubElement(settings, which)
        tb.set('sdiMuted', 'False')
        tb_inputs = ET.SubElement(tb, 'TalkbackInputs')
        for i in range(1, 11):
            ti = ET.SubElement(tb_inputs, 'TalkbackInput')
            _set_attrs(ti, audioInputId=i, sdiMuted=False)

    # Inputs — list external sources (port_type==0).
    inputs_e = ET.SubElement(settings, 'Inputs')
    for src_idx in sorted(mx.get('input-properties', {}).keys()):
        ip = mx['input-properties'][src_idx]
        if getattr(ip, 'port_type', None) != 0:
            continue
        ie = ET.SubElement(inputs_e, 'Input')
        _set_attrs(ie,
                   id=src_idx,
                   shortName=getattr(ip, 'short_name', '') or '',
                   longName=getattr(ip, 'name', '') or '',
                   externalPortType=_ext_port_type_name(
                       getattr(ip, 'source_ports', 1) or 1))

    # MediaPool — clip pool capacity. atemwire doesn't expose; defaults.
    mp = ET.SubElement(settings, 'MediaPool')
    clips = ET.SubElement(mp, 'Clips')
    for ci in range(2):
        c = ET.SubElement(clips, 'Clip')
        _set_attrs(c, index=ci, maxFrameCount=100)

    # MixMinusOutputs — atemwire doesn't expose; defaults.
    mmo = ET.SubElement(settings, 'MixMinusOutputs')
    for i in range(6):
        e = ET.SubElement(mmo, 'MixMinusOutput')
        _set_attrs(e, index=i, audioMode='ProgramOut')

    # MultiViews
    mvs = ET.SubElement(settings, 'MultiViews')
    mv_props = mx.get('multiviewer-properties', {})
    mv_inputs = mx.get('multiviewer-input', {})
    mv_vu = mx.get('multiviewer-vu', {})
    mv_safe = mx.get('multiviewer-safe-area', {})
    for mv_idx in sorted(mv_props.keys()):
        mv_node = mv_props[mv_idx]
        mv = ET.SubElement(mvs, 'MultiView')
        _set_attrs(mv, index=mv_idx,
                   LayoutID=getattr(mv_node, 'layout', 12),
                   vuMeterOpacity=1)
        windows = ET.SubElement(mv, 'Windows')
        win_dict = mv_inputs.get(mv_idx, {})
        for win_idx in sorted(win_dict.keys()):
            wn = win_dict[win_idx]
            we = ET.SubElement(windows, 'Window')
            vu_enabled = getattr(
                mv_vu.get(mv_idx, {}).get(win_idx, None), 'enabled', False)
            safe_enabled = getattr(
                mv_safe.get(mv_idx, {}).get(win_idx, None), 'enabled', False)
            _set_attrs(we,
                       index=win_idx,
                       input=getattr(wn, 'source', 0),
                       vuMeterEnabled=bool(vu_enabled),
                       safeAreaEnabled=bool(safe_enabled))

    # ButtonMapping — local (Software Control) state, not on switcher.
    bm = ET.SubElement(settings, 'ButtonMapping')
    for i in range(10):
        b = ET.SubElement(bm, 'Button')
        _set_attrs(b, index=i, externalInputIndex=i,
                   mappedToCamera=True,
                   mappedCameraModelName='Blackmagic Generic')

    # UpstreamKeys sizeLink is Software-Control-local UI state, not exposed
    # in mixerstate. Emit the element without a fabricated value rather than
    # lying with a hardcoded True (the live ASC save shows it can be False).
    ET.SubElement(settings, 'UpstreamKeys')


# Static MV lookup observed in the reference (Constellation HD). Order
# preserved verbatim. This is hardware capability — not user state.
_DEFAULT_MULTIVIEW_VIDEO_MODES = [
    ('720p50', '720p50'),
    ('720p5994', '720p5994'),
    ('720p60', '720p60'),
    ('1080i50', '1080i50'),
    ('1080i5994', '1080i5994'),
    ('1080i60', '1080i60'),
    ('1080p2398', '1080p2398'),
    ('1080p24', '1080p24'),
    ('1080p25', '1080p25'),
    ('1080p2997', '1080i5994'),
    ('1080p30', '1080p30'),
    ('1080p50', '1080i50'),
    ('1080p5994', '1080i5994'),
    ('1080p60', '1080p60'),
]


# ---------------------------------------------------------------------------
# VideoMode
# ---------------------------------------------------------------------------

def _build_video_mode(root: ET.Element, mx: dict) -> None:
    e = ET.SubElement(root, 'VideoMode')
    vm = video_mode(mx)
    label = (vm or {}).get('format', '') if isinstance(vm, dict) else ''
    # XML form has no spaces or aspect — drop any " 16:9" suffix.
    label = label.split(' ')[0] if label else ''
    e.set('videoMode', label or '1080p60')


# ---------------------------------------------------------------------------
# HyperDecks
# ---------------------------------------------------------------------------

def _build_hyperdecks(root: ET.Element, mx: dict) -> None:
    # Emit each slot's real binding (IP + switcher input) read live from RXMS.
    # autoRoll / autoRollFrameDelay aren't mapped on the wire yet, so they stay
    # at the reference defaults (preserved, not yet round-tripped).
    parent = ET.SubElement(root, 'HyperDecks')
    for i in range(10):
        hd = hyperdeck_settings(mx, i)
        e = ET.SubElement(parent, 'HyperDeck')
        _set_attrs(e, id=i, networkAddress=hd['network_address'],
                   input=hd['input'], autoRoll=False, autoRollFrameDelay=0)


# ---------------------------------------------------------------------------
# FairlightAudioMixer
# ---------------------------------------------------------------------------

def _build_fairlight(root: ET.Element, mx: dict) -> None:
    fa = ET.SubElement(root, 'FairlightAudioMixer')
    master = mx.get('fairlight-master-properties')
    master_volume = (getattr(master, 'volume', 0) / 100.0) if master else 0.0
    afv = bool(getattr(master, 'afv', False)) if master else False
    eq_enable = bool(getattr(master, 'eq_enable', True)) if master else True
    eq_gain = (getattr(master, 'eq_gain', 0) / 100.0) if master else 0.0
    dyn_makeup = (getattr(master, 'dynamics_gain', 0) / 100.0) if master else 0.0
    _set_attrs(fa,
               masterOutFaderGain=master_volume,
               followFadeToBlack=afv,
               audioFollowVideoCrossfadeTransition=False)

    eq = ET.SubElement(fa, 'MasterOutEqualizer')
    _set_attrs(eq, enabled=eq_enable, gain=eq_gain)
    _build_eq_bands(eq, mx, source_id=None)

    dyn = ET.SubElement(fa, 'MasterOutDynamicsProcessor')
    _set_attrs(dyn, makeupGain=dyn_makeup)
    # Master Compressor/Limiter read live from MOCP/AMLP (parsed since
    # 2026-06). _build_compressor/_build_limiter fall back to defaults when
    # the packet hasn't arrived. (Master make-up gain comes from FAMP above,
    # not MOCP.) The master has no Expander.
    _build_compressor(dyn, fairlight_master_compressor(mx))
    _build_limiter(dyn, fairlight_master_limiter(mx))

    # Per-input audio strips. The ATEM has different "input" types
    # tracked under multiple mixerstate keys; use 'fairlight-audio-input'
    # for the input metadata + 'fairlight-strip-properties' for per-strip.
    inputs_e = ET.SubElement(fa, 'AudioInputs')
    audio_inputs = mx.get('fairlight-audio-input', {})
    strip_props = mx.get('fairlight-strip-properties', {})
    # Live per-strip dynamics, keyed by strip_id ('<source>.<subchannel>').
    comp_by_strip = fairlight_compressor(mx)
    lim_by_strip = fairlight_limiter(mx)
    exp_by_strip = fairlight_expander(mx)

    # FASP / AEBP are keyed by ``strip_id`` (a "<source>.<subchannel>"
    # string built by the field decoder, e.g. '1.0' for the stereo
    # combined channel of source 1, '1.1' for split-mono right). We index
    # both flatly under that key.
    strips_by_source = {}
    for sid_str, sp in strip_props.items():
        if not isinstance(sid_str, str) or '.' not in sid_str:
            continue
        try:
            base = int(sid_str.split('.')[0])
        except ValueError:
            continue
        strips_by_source.setdefault(base, []).append((sid_str, sp))

    # Order in the reference: 1..10 (cameras), 2001/2002 (color),
    # 1301 (XLR mic), 1401 (TRS jack). We sort numerically for
    # determinism — Software Control's order may differ.
    for src_id in sorted(audio_inputs.keys()):
        ai = audio_inputs[src_id]
        is_mono = (getattr(ai, 'split', 2) == 1)
        ai_e = ET.SubElement(inputs_e, 'AudioInput')
        _set_attrs(ai_e, configuration='Mono' if is_mono else 'Stereo',
                   id=src_id)

        # Optional Analog level — only on inputs with analog hardware.
        level = getattr(ai, 'level', 0)
        if level in (1, 2):
            an = ET.SubElement(ai_e, 'Analog')
            an.set('level', 'Mic' if level == 1 else 'ProLine')

        strips = strips_by_source.get(src_id, [])
        # Default: emit one synthetic strip with sensible defaults if FASP
        # hasn't arrived yet (matches Software Control's blank-default
        # output). Use the right id-int form for stereo vs mono.
        if not strips:
            strips = [(f'{src_id}.0', None)]

        for strip_id_str, sp in strips:
            src_e = ET.SubElement(ai_e, 'AudioSource')
            # ATEM Software Control writes a signed-i64 stereo/mono
            # sentinel for the AudioSource id rather than a per-strip
            # value; this mirrors that behaviour (-65280 stereo, -256
            # mono). The macro encoder in macrotransfer treats either
            # convention as the universal sentinel.
            sid = -256 if is_mono else -65280
            volume = getattr(sp, 'volume', -10000) if sp else -10000
            gain = getattr(sp, 'gain', 0) if sp else 0
            pan = getattr(sp, 'pan', 0) if sp else 0
            mix = getattr(sp, 'state', 0x01) if sp else 0x01
            delay = getattr(sp, 'delay', None) if sp else None
            _set_attrs(src_e,
                       id=sid,
                       inputGain=gain / 100.0,
                       pan=pan / 100.0,
                       faderGain=(float('-inf') if volume <= -10000
                                  else volume / 100.0),
                       mixOption=MIX_OPTION_NAMES.get(mix, 'Off'))
            if delay is not None and src_id >= 1300:
                src_e.set('delayFrames', _fmt(delay))

            eq_e = ET.SubElement(src_e, 'Equalizer')
            eq_enabled = bool(getattr(sp, 'eq_enable', True)) if sp else True
            eq_g = (getattr(sp, 'eq_gain', 0) / 100.0) if sp else 0.0
            _set_attrs(eq_e, enabled=eq_enabled, gain=eq_g)
            _build_eq_bands(eq_e, mx, source_id=strip_id_str)

            dyn_e = ET.SubElement(src_e, 'DynamicsProcessor')
            dyn_g = (getattr(sp, 'dynamics_gain', 0) / 100.0) if sp else 0.0
            _set_attrs(dyn_e, makeupGain=dyn_g)
            _build_expander(dyn_e, exp_by_strip.get(strip_id_str))
            _build_compressor(dyn_e, comp_by_strip.get(strip_id_str))
            _build_limiter(dyn_e, lim_by_strip.get(strip_id_str))

    # Headphone outputs.
    hp_out = ET.SubElement(fa, 'AudioHeadphoneOutputs')
    hp_node = mx.get('fairlight-headphones')
    hp_volume = (getattr(hp_node, 'volume', -1500) / 100.0) if hp_node else -15.0
    hp_e = ET.SubElement(hp_out, 'AudioHeadphoneOutput')
    _set_attrs(hp_e,
               index=0,
               gain=hp_volume,
               masterOutGain=-6,
               masterOutMute=True,
               talkbackGain=-10,
               talkbackMute=True,
               sidetoneGain=-10)


def _build_eq_bands(parent: ET.Element, mx: dict, source_id) -> None:
    """Emit 6 EqualizerBand entries. ``source_id`` is the strip-id string
    (``'<source>.<subchannel>'``) for per-input EQ, or ``None`` for the
    master EQ. Falls back to default values matching the reference XML
    when no live data is available."""
    if source_id is not None:
        bp = mx.get('atem-eq-band-properties', {})
        bands_dict = bp.get(source_id, {}) if isinstance(bp, dict) else {}
    else:
        bands_dict = mx.get('atem-master-eq-band-properties', {}) or {}

    defaults = [
        (0, False, 'HighPass',  'Low',     46,    0, 0.71),
        (1, True,  'LowShelf',  'Low',     49,    0, 1),
        (2, True,  'BandPass',  'MidLow',  171,   0, 2),
        (3, True,  'BandPass',  'MidHigh', 798,   0, 2),
        (4, True,  'HighShelf', 'High',    7260,  0, 1),
        (5, False, 'LowPass',   'High',    12900, 0, 0.71),
    ]
    for idx, en, shape, freq_range, freq, gain, q in defaults:
        # Override with live data when available.
        b = bands_dict.get(idx)
        if b is not None:
            en = bool(getattr(b, 'band_enabled', en))
            shape_int = getattr(b, 'band_filter', None)
            if shape_int is not None and shape_int in EQ_SHAPE_NAMES:
                shape = EQ_SHAPE_NAMES[shape_int]
            # Read the band's real frequency range, not the static
            # by-index default — a user who moves a band into another
            # range now round-trips.
            range_int = getattr(b, 'band_freq_range', None)
            if range_int is not None and int(range_int) in EQ_FREQ_RANGE_NAMES:
                freq_range = EQ_FREQ_RANGE_NAMES[int(range_int)]
            freq = getattr(b, 'band_frequency', freq)
            gain = (getattr(b, 'band_gain', 0) / 100.0)
            q = (getattr(b, 'band_q', int(round(q * 100))) / 100.0)
        e = ET.SubElement(parent, 'EqualizerBand')
        _set_attrs(e,
                   index=idx, enabled=en, shape=shape,
                   frequencyRange=freq_range,
                   frequency=int(freq) if isinstance(freq, (int, float))
                   and float(freq).is_integer() else freq,
                   gain=gain, qFactor=q)


def _build_default_expander(parent: ET.Element) -> None:
    e = ET.SubElement(parent, 'Expander')
    _set_attrs(e, enabled=False, gateMode=False,
               threshold=-45, range=18, ratio=1.1,
               attack=1.4, hold=0, release=93)


def _build_default_compressor(parent: ET.Element) -> None:
    e = ET.SubElement(parent, 'Compressor')
    _set_attrs(e, enabled=False, threshold=-35, ratio=2,
               attack=1.4, hold=0, release=93)


def _build_default_limiter(parent: ET.Element) -> None:
    e = ET.SubElement(parent, 'Limiter')
    _set_attrs(e, enabled=False, threshold=-12,
               attack=0.71, hold=0, release=93)


# Live per-strip dynamics emitters. ``block`` is the reader dict
# (fairlight_compressor/limiter/expander[strip_id]) or None — when the
# strip's dynamics packet hasn't arrived we fall back to the defaults so
# the XML shape is unchanged.

def _build_compressor(parent: ET.Element, block) -> None:
    if block is None:
        _build_default_compressor(parent)
        return
    e = ET.SubElement(parent, 'Compressor')
    _set_attrs(e, enabled=block['enabled'], threshold=block['threshold_db'],
               ratio=block.get('ratio', 2), attack=block['attack_ms'],
               hold=block['hold_ms'], release=block['release_ms'])


def _build_limiter(parent: ET.Element, block) -> None:
    if block is None:
        _build_default_limiter(parent)
        return
    e = ET.SubElement(parent, 'Limiter')
    _set_attrs(e, enabled=block['enabled'], threshold=block['threshold_db'],
               attack=block['attack_ms'], hold=block['hold_ms'],
               release=block['release_ms'])


def _build_expander(parent: ET.Element, block) -> None:
    if block is None:
        _build_default_expander(parent)
        return
    e = ET.SubElement(parent, 'Expander')
    _set_attrs(e, enabled=block['enabled'],
               gateMode=(block.get('mode') == 'gate'),
               threshold=block['threshold_db'], range=block.get('range_db', 0),
               ratio=block.get('ratio', 1), attack=block['attack_ms'],
               hold=block['hold_ms'], release=block['release_ms'])


# ---------------------------------------------------------------------------
# MediaPlayers + MediaPool stills
# ---------------------------------------------------------------------------

def _build_media_players(root: ET.Element, mx: dict) -> None:
    parent = ET.SubElement(root, 'MediaPlayers')
    sel = mx.get('mediaplayer-selected', {})
    if not sel:
        # No data — emit two defaults to match the reference shape.
        for i in range(2):
            e = ET.SubElement(parent, 'MediaPlayer')
            _set_attrs(e, index=i, sourceType='Still', sourceIndex=0)
        return
    for i in sorted(sel.keys()):
        s = mediaplayer_selected(mx, i)
        st_int = s.get('source_type', 1)
        e = ET.SubElement(parent, 'MediaPlayer')
        _set_attrs(e, index=i,
                   sourceType='Still' if st_int == 1 else 'Clip',
                   sourceIndex=s.get('slot', 0))


def _build_media_pool_stills(root: ET.Element, mx: dict) -> None:
    parent = ET.SubElement(root, 'MediaPool')
    stills = ET.SubElement(parent, 'Stills')
    files = mx.get('mediaplayer-file-info', {})
    for idx in sorted(files.keys()):
        info = mediaplayer_slot_info(mx, idx)
        if not info['is_used']:
            continue
        e = ET.SubElement(stills, 'Still')
        name = info['name']
        _set_attrs(e, index=idx, name=name,
                   path=f'ATEM Media Pool/{name}.png' if name else '')


# ---------------------------------------------------------------------------
# CameraControl
# ---------------------------------------------------------------------------

def _build_camera_control(root: ET.Element, mx: dict) -> None:
    """Emit defaults for cameras 1..10 matching observed reference output.
    atemwire's CCdP parser doesn't expose typed accessors for individual
    parameters, so we emit a "freshly-defaulted" set rather than reading
    live state."""
    parent = ET.SubElement(root, 'CameraControl')
    # Discover camera count from the input list (port_type==0).
    cameras = []
    for src_idx in sorted(mx.get('input-properties', {}).keys()):
        ip = mx['input-properties'][src_idx]
        if getattr(ip, 'port_type', None) == 0:
            cameras.append(src_idx)
    if not cameras:
        cameras = list(range(1, 11))

    def _param(d, cat, name, **kw):
        e = ET.SubElement(parent, 'Parameter')
        _set_attrs(e, device=d, category=cat, parameter=name, **kw)

    for d in cameras:
        _param(d, 'Lens', 'ApertureFstop', value=5)
        _param(d, 'Video', 'ManualWhiteBalance', temperature=5600, tint=0)
        _param(d, 'Video', 'Exposure', value=20000)
        _param(d, 'Video', 'DetailLevel', value=1)
        _param(d, 'Video', 'SensorGainInDecibels', value=0)
        _param(d, 'Video', 'SensorNDFilter', value=0)
        _param(d, 'ColorCorrection', 'LiftAdjust', red=0, green=0, blue=0, luma=0)
        _param(d, 'ColorCorrection', 'GammaAdjust', red=0, green=0, blue=0, luma=0)
        _param(d, 'ColorCorrection', 'GainAdjust', red=1, green=1, blue=1, luma=1)
        _param(d, 'ColorCorrection', 'OffsetAdjust', red=0, green=0, blue=0, luma=0)
        _param(d, 'ColorCorrection', 'ContrastAdjust', pivot=0.5, adjust=1)
        _param(d, 'ColorCorrection', 'LumaMix', value=1)
        _param(d, 'ColorCorrection', 'ColorAdjust', hue=0, saturation=1)
    for d in cameras:
        e = ET.SubElement(parent, 'Property')
        _set_attrs(e, device=d, property='ApertureCoarse', value=1)
        e = ET.SubElement(parent, 'Property')
        _set_attrs(e, device=d, property='Locked', value=False)


# ---------------------------------------------------------------------------
# MacroPool / MacroControl
# ---------------------------------------------------------------------------

def _build_macro_pool(root: ET.Element, mx: dict, atem=None) -> None:
    """Emit each used macro slot's metadata + ``<Op>`` children.

    Slot metadata (name/description) comes from MPrp packets in
    ``mixerstate``. Op contents are downloaded over the file-transfer
    protocol (FTSU → FTDa → FTUA → FTDC), parsed from the LE-encoded
    macro bytecode, and emitted as ``<Op id="..." attrs="..."/>``.

    See ``atemwire.macrotransfer`` and ``atemwire/docs/MACRO_FORMAT.md`` for the
    download flow and bytecode format.

    ``atem`` is the connected ATEM/ATEMConnection/protocol (passed through from
    ``Profile.from_atem``). When ``None`` — e.g. when from_atem is
    invoked with just a mixerstate dict for testing — only the slot
    metadata is emitted; ``<Op>`` children are skipped. Per-slot
    download failures are logged and don't prevent other slots from
    being emitted.
    """
    from atemwire.macrotransfer import (  # local import to avoid cycle
        decode_macro_bytecode,
        download_macro_bytecode,
    )

    parent = ET.SubElement(root, 'MacroPool')
    macros = mx.get('macro-properties', {})
    for idx in sorted(macros.keys()):
        m = macro_entry(mx, idx)
        if not m['is_used']:
            continue
        e = ET.SubElement(parent, 'Macro')
        node = macros[idx]
        desc = decode_name(getattr(node, 'description', b''), '')
        _set_attrs(e, index=idx, name=m['name'], description=desc)

        if atem is None:
            continue

        # Download + decode the slot's bytecode (native transfer; skip-on-
        # error per slot). Worker-pumped connections use download_macro;
        # bare protocols (dev tools driving Profile.from_atem directly)
        # use the self-pumping helper — pumping on a pooled connection
        # would race its worker thread.
        conn = getattr(atem, 'raw', None) or atem
        try:
            if hasattr(conn, 'download_macro'):
                raw = conn.download_macro(idx)
            else:
                raw = download_macro_bytecode(
                    _resolve_protocol_or_none(atem), idx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("macro slot %d: download failed (%s) — emitting "
                           "metadata only", idx, exc)
            continue
        if not raw:
            continue
        try:
            ops = decode_macro_bytecode(raw, mx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("macro slot %d: decode failed (%s) — emitting "
                           "metadata only", idx, exc)
            continue
        for op in ops:
            op_e = ET.SubElement(e, 'Op')
            op_e.set('id', op['id'])
            for k, v in op.items():
                if k == 'id':
                    continue
                op_e.set(k, v)




def download_media_pool_images(
    atem,
    progress_callback=None,
    cancel_check=None,
    timeout_per_slot: float = 30.0,
    cache_get=None,
    cache_put=None,
) -> List[dict]:
    """Download every used media pool slot as a PNG.

    Returns a list of ``{slot: int, name: str, png_bytes: bytes}``.
    Slots that fail to download are skipped (logged); the caller gets a
    partial list with the successful ones.

    ``progress_callback(slot, completed, total)`` fires after each slot
    finishes (whether downloaded fresh or served from cache).

    ``cancel_check()`` returning True aborts before the next slot.

    ``cache_get(slot, hash_hex) -> bytes | None`` is consulted before
    each download; on hit, the cached PNG is appended and the protocol
    download is skipped entirely. ``cache_put(slot, hash_hex,
    png_bytes)`` is called after each successful fresh download.
    Callers that don't care about caching leave both as ``None``.
    The library doesn't own the cache — a caller can use a
    per-save ``dict[hash_hex, bytes]`` to dedupe slots that share
    the same MPfe hash within a single save.

    Downloads ride the connection's NORMAL packet loop
    (``ATEMConnection.download_still`` — native interleaved transfer,
    ASC-style step 3, 2026-07-02): control traffic and state events keep
    flowing during each slot (~2 s measured per 1080p still), so a save
    with images no longer freezes operators sharing the connection.
    Requires an ``ATEM`` facade or ``ATEMConnection`` (a bare
    ``AtemProtocol`` has no worker pumping the transfer).
    """
    from io import BytesIO

    from PIL import Image
    from atemwire.imaging import atem_to_rgb

    conn = getattr(atem, 'raw', None) or atem  # ATEM facade → ATEMConnection
    if not hasattr(conn, 'download_still'):
        raise RuntimeError(
            "download_media_pool_images needs an ATEM/ATEMConnection "
            "(native download_still); got "
            f"{type(atem).__name__}")
    if not getattr(conn, 'is_connected', False):
        raise RuntimeError("download_media_pool_images: connection not ready")

    mx = _resolve_mixerstate(atem)
    files = mx.get('mediaplayer-file-info', {})
    used = sorted(idx for idx, info in files.items()
                  if getattr(info, 'is_used', False))

    # Resolution from current video-mode (1920×1080 for any 1080p mode).
    vm = mx.get('video-mode')
    if vm is not None:
        try:
            width, height = vm.get_resolution()
        except Exception:
            width, height = 1920, 1080
    else:
        width, height = 1920, 1080

    out: List[dict] = []
    total = len(used)
    for n, slot in enumerate(used):
        if cancel_check is not None:
            try:
                if cancel_check():
                    logger.info("media-pool image capture cancelled "
                                "after slot %d/%d", n, total)
                    break
            except Exception:  # noqa: BLE001
                pass

        info = mediaplayer_slot_info(mx, slot)
        name = info.get('name') or f'Slot{slot}'
        hash_hex = info.get('hash') or ''

        # Cache fast-path: if the caller supplied cache_get and the hash
        # matches a stored entry, skip the ~2 s download entirely.
        png_bytes = None
        from_cache = False
        if cache_get is not None and hash_hex:
            try:
                cached = cache_get(slot, hash_hex)
            except Exception:  # noqa: BLE001
                cached = None
            if cached:
                png_bytes = cached
                from_cache = True

        if png_bytes is None:
            try:
                ycbcra = conn.download_still(slot, timeout=timeout_per_slot)
            except Exception as exc:  # noqa: BLE001
                logger.warning("media-pool slot %d (%r) download failed: %s",
                               slot, name, exc)
                continue
            try:
                rgb = atem_to_rgb(ycbcra, width, height)
                img = Image.frombytes('RGBA', (width, height), rgb)
                buf = BytesIO()
                img.save(buf, format='PNG')
                png_bytes = buf.getvalue()
            except Exception as exc:  # noqa: BLE001
                logger.warning("media-pool slot %d (%r) decode/PNG failed: %s",
                               slot, name, exc)
                continue
            if cache_put is not None and hash_hex:
                try:
                    cache_put(slot, hash_hex, png_bytes)
                except Exception:  # noqa: BLE001
                    pass

        out.append({'slot': int(slot), 'name': str(name),
                    'png_bytes': png_bytes, 'from_cache': from_cache})
        if progress_callback is not None:
            try:
                progress_callback(slot, len(out), total)
            except Exception:  # noqa: BLE001
                pass

    return out


def _build_macro_control(root: ET.Element, mx: dict) -> None:
    # Every observed Software Control export has loop="False"; atemwire's
    # MRPr exposes ``is_looping`` but Software Control doesn't write it
    # to this element. Emit the constant to match the reference.
    e = ET.SubElement(root, 'MacroControl')
    e.set('loop', 'False')


def _build_counters(root: ET.Element, mx: dict) -> None:
    """Display-clock / counter overlay state. atemwire doesn't expose typed
    accessors; emit one default counter that matches the reference."""
    parent = ET.SubElement(root, 'Counters')
    e = ET.SubElement(parent, 'Counter')
    _set_attrs(e, index=0, enabled=False, mode='Countdown', autoHide=False,
               positionX=0, positionY=-6.3, size=25, opacity=100,
               startFromHours=0, startFromMinutes=0,
               startFromSeconds=0, startFromFrames=0)


# =============================================================================
# apply helpers — walk XML, dispatch to operations
