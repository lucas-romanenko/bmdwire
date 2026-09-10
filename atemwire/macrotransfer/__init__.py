# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""Macro file-transfer + bytecode codec.

This is the read + write side that closes the macro round-trip gap
from the 2026-04-28 macro-restore session. Macros are downloaded
over the same file-transfer protocol the still-store uses
(FTSU/FTDa/FTUA/FTDC) and uploaded via upstream's transfer
state machine.

Notable wire-format quirks distinguish the macro store from the still
store:

1. **No locking** on download (Software Control's macro pull skips
   PLCK/LKOB and goes straight to FTSU).
2. **The bytecode is little-endian.** Every other ATEM wire format —
   the live control commands, the still RLE chunks — is big-endian.
   The macro store is the exception: each op's length, type code,
   and parameters are encoded as little-endian u16/u32.
3. **Mode bits in FTSD** (upload only): macro store requires 0x0300
   (uncompressed + pre-erase), while the still store accepts 0x0001
   (Write RLE).

Layout::

    atemwire/macrotransfer/
      __init__.py        — _KNOWN_OPS registry + decode/encode dispatch
      _io.py             — download_macro_bytecode + upload_macro_bytecode
      _helpers.py        — primitives + layout helpers + shared enum tables
      switching.py       — bus, transition, ftb, aux, color gen, video mode
      upstream_keyer.py  — USK, ACK, fly key
      downstream_keyer.py
      media.py
      fairlight.py       — strip, master, EQ, dynamics, headphones
      flow.py            — MacroSleep

Usage::

    from atemwire.macrotransfer import (
        download_macro_bytecode, decode_macro_bytecode,
        encode_macro_bytecode, upload_macro_bytecode,
    )

    raw = connection.download_macro(0)   # or download_macro_bytecode(protocol, 0) on a bare, self-pumped protocol
    ops = decode_macro_bytecode(raw, connection.mixerstate)
    # ops is a list of dicts with 'id' (Software Control op id)
    # plus per-op parameter attributes matching XML profile output.

    new_bytecode = encode_macro_bytecode(ops)
    upload_macro_bytecode(connection.protocol, slot=0,
                          name='My Macro', description='', bytecode=new_bytecode)
"""

import logging
import struct
from typing import List, Optional

from atemwire.macrotransfer import (
    downstream_keyer as _dsk,
    fairlight as _fl,
    flow as _flow,
    hyperdeck as _hd,
    media as _media,
    switching as _sw,
    upstream_keyer as _usk,
)
from atemwire.macrotransfer._io import (
    MACRO_STORE,
    download_macro_bytecode,
    upload_macro_bytecode,
)
from atemwire.macrotransfer._helpers import _u16le

logger = logging.getLogger(__name__)


def _op(mod, name):
    """Resolve a per-feature codec's ``(decoder, encoder)`` pair from
    its module by name. Each ``_KNOWN_OPS`` row spreads this tuple into
    the trailing two slots of ``(xml_id, decoder, encoder)``."""
    return getattr(mod, '_d_' + name), getattr(mod, '_e_' + name)


# Op-code → (xml_id, decoder, encoder). The xml_id matches what ATEM
# Software Control emits in its XML <Op> elements; the decoder /
# encoder functions implement the per-op wire ↔ XML conversion. The
# ``test_known_ops_table_entries_are_3_tuples`` test asserts every row
# has callable decoder + encoder so save/restore stays symmetric.
_KNOWN_OPS = {
    # Bus + transition
    0x0002: ('ProgramInput',          *_op(_sw, 'program_input')),
    0x0003: ('PreviewInput',          *_op(_sw, 'preview_input')),
    0x0004: ('Cut',                   *_op(_sw, 'cut')),
    0x0005: ('Auto',                  *_op(_sw, 'auto')),
    # Video mode (auto-prologue + explicit operator-driven mode change).
    # Live-discovered 2026-04-30: recording a CVdM produces op 0x000c.
    0x000C: ('VideoMode',             *_op(_sw, 'video_mode')),
    # Fade-to-black enable. Live-discovered 2026-04-30 (FEna capture);
    # XML schema confirmed 2026-04-30 from a real Software Control
    # macro export.
    0x0202: ('FadeToBlackEnabled',    *_op(_sw, 'ftb_enabled')),
    0x00A7: ('FadeToBlack',           *_op(_sw, 'fade_to_black')),
    0x0083: ('TransitionStyle',       *_op(_sw, 'transition_style')),
    0x0084: ('TransitionSource',      *_op(_sw, 'transition_source')),
    # Macro flow
    0x0007: ('MacroSleep',            *_op(_flow, 'macro_sleep')),
    # Aux routing — confirmed against a Software Control export of a
    # macro containing two AuxiliaryInput ops.
    0x001F: ('AuxiliaryInput',        *_op(_sw, 'auxiliary_input')),
    # Upstream keyer
    0x0025: ('KeyCutInput',           *_op(_usk, 'key_cut_input')),
    0x0026: ('KeyFillInput',          *_op(_usk, 'key_fill_input')),
    0x0027: ('KeyOnAir',              *_op(_usk, 'key_on_air')),
    0x0028: ('KeyType',               *_op(_usk, 'key_type')),
    0x0029: ('LumaKeyClip',           *_op(_usk, 'luma_key_clip')),
    0x002A: ('LumaKeyGain',           *_op(_usk, 'luma_key_gain')),
    0x002B: ('KeyFlyEnable',          *_op(_usk, 'key_fly_enable')),
    0x002C: ('LumaKeyInvert',         *_op(_usk, 'luma_key_invert')),
    0x002D: ('LumaKeyPreMultiply',    *_op(_usk, 'luma_key_pre_multiply')),
    0x002F: ('KeyMaskEnable',         *_op(_usk, 'key_mask_enable')),
    # DVE / fly key — sizes, positions, mask. Op codes empirically discovered
    # 2026-04-29 by recording single-field wire commands into slot 99 on a
    # live ATEM Constellation HD and diffing the downloaded bytecode.
    0x0035: ('DVEKeyMaskEnable',      *_op(_usk, 'dve_mask_enable')),
    0x0036: ('DVEKeyMaskTop',         *_op(_usk, 'dve_mask_top')),
    0x0037: ('DVEKeyMaskBottom',      *_op(_usk, 'dve_mask_bottom')),
    0x0038: ('DVEKeyMaskLeft',        *_op(_usk, 'dve_mask_left')),
    0x0039: ('DVEKeyMaskRight',       *_op(_usk, 'dve_mask_right')),
    0x0047: ('DVEAndFlyKeyXSize',     *_op(_usk, 'dve_xsize')),
    0x0048: ('DVEAndFlyKeyYSize',     *_op(_usk, 'dve_ysize')),
    0x004A: ('DVEAndFlyKeyXPosition', *_op(_usk, 'dve_xpos')),
    0x004B: ('DVEAndFlyKeyYPosition', *_op(_usk, 'dve_ypos')),
    # Downstream keyer
    0x0096: ('DownstreamKeyFillInput',    *_op(_dsk, 'dsk_fill_input')),
    0x0097: ('DownstreamKeyCutInput',     *_op(_dsk, 'dsk_cut_input')),
    0x0098: ('DownstreamKeyRate',         *_op(_dsk, 'dsk_rate')),
    0x0099: ('DownstreamKeyAuto',         *_op(_dsk, 'dsk_auto')),
    0x009A: ('DownstreamKeyOnAir',        *_op(_dsk, 'dsk_on_air')),
    0x009B: ('DownstreamKeyTie',          *_op(_dsk, 'dsk_tie')),
    0x009C: ('DownstreamKeyClip',         *_op(_dsk, 'dsk_clip')),
    0x009D: ('DownstreamKeyGain',         *_op(_dsk, 'dsk_gain')),
    0x009E: ('DownstreamKeyMaskEnable',   *_op(_dsk, 'dsk_mask_enable')),
    0x00A4: ('DownstreamKeyPreMultiply',  *_op(_dsk, 'dsk_pre_multiply')),
    # Media player
    0x00DA: ('MediaPlayerSourceStillIndex', *_op(_media, 'media_player_still_index')),
    0x00E1: ('MediaPlayerSourceStill',      *_op(_media, 'media_player_still_only')),
    # Fairlight audio mixer (verified live-recorded on a production switcher 2026-04-30:
    # CFSP→0x014b strip volume, CFSP→0x014a strip state, CFMP→0x016b
    # master volume).
    0x014A: ('FairlightAudioMixerInputSourceMixType',     *_op(_fl, 'fairlight_strip_mix_type')),
    0x014B: ('FairlightAudioMixerInputSourceFaderGain',   *_op(_fl, 'fairlight_strip_fader_gain')),
    0x016B: ('FairlightAudioMixerMasterOutFaderGain',     *_op(_fl, 'fairlight_master_fader_gain')),

    # Comprehensive close-out 2026-04-30 (Lots XML alignment):
    # Tier 1 — Transition family
    0x0087: ('TransitionMixRate',                *_op(_sw, 'transition_mix_rate')),
    0x0088: ('TransitionDipRate',                *_op(_sw, 'transition_dip_rate')),
    0x0089: ('TransitionDipInput',               *_op(_sw, 'transition_dip_input')),
    0x007C: ('TransitionWipeRate',               *_op(_sw, 'transition_wipe_rate')),
    0x007D: ('TransitionWipePattern',            *_op(_sw, 'transition_wipe_pattern')),
    0x007E: ('TransitionWipeBorderWidth',        *_op(_sw, 'transition_wipe_border_width')),
    0x007F: ('TransitionWipeBorderSoftness',     *_op(_sw, 'transition_wipe_border_softness')),
    0x0080: ('TransitionWipeBorderFillInput',    *_op(_sw, 'transition_wipe_border_fill_input')),
    0x0081: ('TransitionWipeAndDVEReverse',      *_op(_sw, 'transition_wipe_dve_reverse')),
    0x0082: ('TransitionWipeAndDVEFlipFlop',     *_op(_sw, 'transition_wipe_dve_flipflop')),
    0x0015: ('TransitionWipeXPosition',          *_op(_sw, 'transition_wipe_x_position')),
    0x0016: ('TransitionWipeYPosition',          *_op(_sw, 'transition_wipe_y_position')),
    0x003A: ('TransitionDVERate',                *_op(_sw, 'transition_dve_rate')),
    0x0034: ('TransitionDVEPattern',             *_op(_sw, 'transition_dve_pattern')),
    0x00D7: ('TransitionDVEFillInput',           *_op(_sw, 'transition_dve_fill_input')),
    0x00D8: ('TransitionDVECutInput',            *_op(_sw, 'transition_dve_cut_input')),
    0x00D9: ('TransitionDVECutInputEnable',      *_op(_sw, 'transition_dve_cut_input_enable')),
    0x008C: ('TransitionStingerSourceMediaPlayer', *_op(_sw, 'transition_stinger_source_mediaplayer')),
    0x008D: ('TransitionStingerClipDuration',    *_op(_sw, 'transition_stinger_clip_duration')),
    0x008E: ('TransitionStingerTriggerPoint',    *_op(_sw, 'transition_stinger_trigger_point')),
    0x008F: ('TransitionStingerMixRate',         *_op(_sw, 'transition_stinger_mix_rate')),
    0x0090: ('TransitionStingerPreRoll',         *_op(_sw, 'transition_stinger_pre_roll')),
    0x0092: ('TransitionStingerDVEClip',         *_op(_sw, 'transition_stinger_dve_clip')),
    0x0093: ('TransitionStingerDVEGain',         *_op(_sw, 'transition_stinger_dve_gain')),
    0x0094: ('TransitionStingerDVEInvert',       *_op(_sw, 'transition_stinger_dve_invert')),
    0x0095: ('TransitionStingerDVEPreMultiply',  *_op(_sw, 'transition_stinger_dve_premultiply')),
    # Tier 1 — Color generators
    0x001C: ('ColorGeneratorHue',                *_op(_sw, 'color_gen_hue')),
    0x001D: ('ColorGeneratorSaturation',         *_op(_sw, 'color_gen_saturation')),
    0x001E: ('ColorGeneratorLuminescence',       *_op(_sw, 'color_gen_luminescence')),
    # Tier 1 — FTB rate
    0x00A5: ('FadeToBlackRate',                  *_op(_sw, 'ftb_rate')),
    # Tier 1 — USK mask edges (KeyMaskEnable already mapped at 0x002F)
    0x0030: ('KeyMaskTop',                       *_op(_usk, 'usk_mask_top')),
    0x0031: ('KeyMaskBottom',                    *_op(_usk, 'usk_mask_bottom')),
    0x0032: ('KeyMaskLeft',                      *_op(_usk, 'usk_mask_left')),
    0x0033: ('KeyMaskRight',                     *_op(_usk, 'usk_mask_right')),
    # Tier 1 — DVE & fly key rate
    0x0046: ('DVEAndFlyKeyRate',                 *_op(_usk, 'dve_flykey_rate')),
    # Tier 1 — USK DVE border
    0x004E: ('DVEKeyBorderEnable',               *_op(_usk, 'dve_border_enable')),
    0x005A: ('DVEKeyBorderHue',                  *_op(_usk, 'dve_border_hue')),
    0x005B: ('DVEKeyBorderSaturation',           *_op(_usk, 'dve_border_saturation')),
    0x005C: ('DVEKeyBorderLuminescence',         *_op(_usk, 'dve_border_luminescence')),
    0x005E: ('DVEKeyBorderOuterWidth',           *_op(_usk, 'dve_border_outer_width')),
    0x005F: ('DVEKeyBorderInnerWidth',           *_op(_usk, 'dve_border_inner_width')),
    0x0060: ('DVEKeyBorderOuterSoftness',        *_op(_usk, 'dve_border_outer_softness')),
    0x0061: ('DVEKeyBorderInnerSoftness',        *_op(_usk, 'dve_border_inner_softness')),
    0x0062: ('DVEKeyBorderOpacity',              *_op(_usk, 'dve_border_opacity')),
    # Tier 1 — USK DVE shadow
    0x004D: ('DVEKeyShadowEnable',               *_op(_usk, 'dve_shadow_enable')),
    0x0058: ('DVEKeyShadowDirection',            *_op(_usk, 'dve_shadow_direction')),
    0x0059: ('DVEKeyShadowAltitude',             *_op(_usk, 'dve_shadow_altitude')),

    # Tier 2 — Advanced Chroma Key
    0x012C: ('AdvancedChromaKeyForegroundLevel',      *_op(_usk, 'ack_foreground_level')),
    0x012D: ('AdvancedChromaKeyBackgroundLevel',      *_op(_usk, 'ack_background_level')),
    0x012E: ('AdvancedChromaKeyKeyEdge',              *_op(_usk, 'ack_key_edge')),
    0x012F: ('AdvancedChromaKeySpillSuppress',        *_op(_usk, 'ack_spill_suppress')),
    0x0130: ('AdvancedChromaKeyFlareSuppress',        *_op(_usk, 'ack_flare_suppress')),
    0x0131: ('AdvancedChromaKeyForegroundBrightness', *_op(_usk, 'ack_foreground_brightness')),
    0x0132: ('AdvancedChromaKeyForegroundContrast',   *_op(_usk, 'ack_foreground_contrast')),
    0x0133: ('AdvancedChromaKeyForegroundColour',     *_op(_usk, 'ack_foreground_colour')),
    0x0134: ('AdvancedChromaKeyForegroundRed',        *_op(_usk, 'ack_foreground_red')),
    0x0135: ('AdvancedChromaKeyForegroundGreen',      *_op(_usk, 'ack_foreground_green')),
    0x0136: ('AdvancedChromaKeyForegroundBlue',       *_op(_usk, 'ack_foreground_blue')),
    0x0137: ('AdvancedChromaKeySamplingModeEnabled',  *_op(_usk, 'ack_sampling_mode_enabled')),
    0x0138: ('AdvancedChromaKeyPreviewEnabled',       *_op(_usk, 'ack_preview_enabled')),
    0x0139: ('AdvancedChromaKeyCursorXPosition',      *_op(_usk, 'ack_cursor_x')),
    0x013A: ('AdvancedChromaKeyCursorYPosition',      *_op(_usk, 'ack_cursor_y')),
    0x013B: ('AdvancedChromaKeyCursorSize',           *_op(_usk, 'ack_cursor_size')),
    # Tier 2 — FlyKey keyframe ops
    0x0050: ('FlyKeySetKeyFrame',                     *_op(_usk, 'flykey_set_keyframe')),
    0x0052: ('FlyKeyRunToFull',                       *_op(_usk, 'flykey_run_to_full')),
    0x0054: ('FlyKeyRunToKeyFrame',                   *_op(_usk, 'flykey_run_to_keyframe')),
    0x0056: ('FlyKeyRunToInfinity',                   *_op(_usk, 'flykey_run_to_infinity')),
    # Tier 2 — Fairlight EQ
    0x014D: ('FairlightAudioMixerInputSourceEqualiserGain',          *_op(_fl, 'eq_gain')),
    0x014E: ('FairlightAudioMixerInputSourceEqualiserBandEnabled',   *_op(_fl, 'eq_band_enabled')),
    0x014F: ('FairlightAudioMixerInputSourceEqualiserBandShape',     *_op(_fl, 'eq_band_shape')),
    0x0150: ('FairlightAudioMixerInputSourceEqualiserBandRange',     *_op(_fl, 'eq_band_range')),
    0x0151: ('FairlightAudioMixerInputSourceEqualiserBandFrequency', *_op(_fl, 'eq_band_frequency')),
    0x0152: ('FairlightAudioMixerInputSourceEqualiserBandGain',      *_op(_fl, 'eq_band_gain')),
    0x0153: ('FairlightAudioMixerInputSourceEqualiserBandQFactor',   *_op(_fl, 'eq_band_qfactor')),

    # Tier 3 — Fairlight dynamics + headphones
    0x0147: ('FairlightAudioMixerInputSourceInputGain',                *_op(_fl, 'input_gain')),
    0x0156: ('FairlightAudioMixerInputSourceDynamicsGain',             *_op(_fl, 'dynamics_gain')),
    0x0157: ('FairlightAudioMixerInputSourceExpanderEnabled',          *_op(_fl, 'expander_enabled')),
    0x0158: ('FairlightAudioMixerInputSourceExpanderGateModeEnabled',  *_op(_fl, 'expander_gate_mode_enabled')),
    0x0159: ('FairlightAudioMixerInputSourceExpanderThreshold',        *_op(_fl, 'expander_threshold')),
    0x015A: ('FairlightAudioMixerInputSourceExpanderRange',            *_op(_fl, 'expander_range')),
    0x015B: ('FairlightAudioMixerInputSourceExpanderRatio',            *_op(_fl, 'expander_ratio')),
    0x015C: ('FairlightAudioMixerInputSourceExpanderAttack',           *_op(_fl, 'expander_attack')),
    0x015D: ('FairlightAudioMixerInputSourceExpanderHold',             *_op(_fl, 'expander_hold')),
    0x015E: ('FairlightAudioMixerInputSourceExpanderRelease',          *_op(_fl, 'expander_release')),
    0x015F: ('FairlightAudioMixerInputSourceCompressorEnabled',        *_op(_fl, 'comp_enabled')),
    0x0160: ('FairlightAudioMixerInputSourceCompressorThreshold',      *_op(_fl, 'comp_threshold')),
    0x0161: ('FairlightAudioMixerInputSourceCompressorRatio',          *_op(_fl, 'comp_ratio')),
    0x0162: ('FairlightAudioMixerInputSourceCompressorAttack',         *_op(_fl, 'comp_attack')),
    0x0163: ('FairlightAudioMixerInputSourceCompressorHold',           *_op(_fl, 'comp_hold')),
    0x0164: ('FairlightAudioMixerInputSourceCompressorRelease',        *_op(_fl, 'comp_release')),
    0x0165: ('FairlightAudioMixerInputSourceLimiterEnabled',           *_op(_fl, 'limiter_enabled')),
    0x0166: ('FairlightAudioMixerInputSourceLimiterThreshold',         *_op(_fl, 'limiter_threshold')),
    0x0167: ('FairlightAudioMixerInputSourceLimiterAttack',            *_op(_fl, 'limiter_attack')),
    0x0168: ('FairlightAudioMixerInputSourceLimiterHold',              *_op(_fl, 'limiter_hold')),
    0x0169: ('FairlightAudioMixerInputSourceLimiterRelease',           *_op(_fl, 'limiter_release')),
    0x0184: ('FairlightAudioMixerHeadphoneOutGain',          *_op(_fl, 'hp_gain')),
    0x0185: ('FairlightAudioMixerHeadphoneOutMasterGain',    *_op(_fl, 'hp_gain')),
    0x0186: ('FairlightAudioMixerHeadphoneOutTalkbackGain',  *_op(_fl, 'hp_gain')),
    0x0187: ('FairlightAudioMixerHeadphoneOutSidetoneGain',  *_op(_fl, 'hp_gain')),
    # HyperDeck binding (RE'd live 2026-06-09)
    0x0110: ('HyperDeckNetworkAddress', *_op(_hd, 'hd_network_address')),
    0x011A: ('HyperDeckInput',          *_op(_hd, 'hd_input')),
}

# Reverse map: XML id → (op_code, encoder). Built once at module load.
_ENCODE_BY_ID = {
    xml_id: (op_code, encoder)
    for op_code, (xml_id, _decoder, encoder) in _KNOWN_OPS.items()
}


def _check_known_ops_symmetry() -> None:
    """Module-load guard: every op in ``_KNOWN_OPS`` must have BOTH a
    decoder and an encoder. Raises ``RuntimeError`` if anything is
    missing.

    Without this guard, an op that has only a decoder would round-trip
    through save (wire → XML) but fail upload (XML → wire); an op with
    only an encoder is the inverse failure. Both halves are required
    to keep save/restore symmetric.

    The unit test ``test_known_ops_table_entries_are_3_tuples``
    (tests/unit/test_macro_decoder.py) runs this same check in pytest.
    NOTE: despite the "module-load" framing, this function has NO caller
    today — it is not invoked at import, so the symmetry guarantee rests
    entirely on that unit test running.
    """
    for op_code, entry in _KNOWN_OPS.items():
        if not isinstance(entry, tuple) or len(entry) != 3:
            raise RuntimeError(
                f"_KNOWN_OPS[0x{op_code:04x}]: expected 3-tuple "
                f"(xml_id, decoder, encoder), got {entry!r}")
        xml_id, decoder, encoder = entry
        if not isinstance(xml_id, str) or not xml_id:
            raise RuntimeError(
                f"_KNOWN_OPS[0x{op_code:04x}]: xml_id must be a "
                f"non-empty string, got {xml_id!r}")
        if not callable(decoder):
            raise RuntimeError(
                f"_KNOWN_OPS[0x{op_code:04x}] {xml_id!r}: decoder "
                f"is not callable — every op needs a wire→XML decoder")
        if not callable(encoder):
            raise RuntimeError(
                f"_KNOWN_OPS[0x{op_code:04x}] {xml_id!r}: encoder is "
                f"not callable — every op needs an XML→wire encoder, "
                f"otherwise save/restore round-trip is broken")


def decode_macro_bytecode(raw: bytes, mx: Optional[dict] = None) -> List[dict]:
    """Parse macro bytecode into a list of op-descriptor dicts.

    Each dict has:
      - ``'id'``: the Software Control op id, or ``'Unknown_0xNNNN'``
        for op codes we haven't mapped yet.
      - per-op param attributes matching what Software Control emits
        in its XML ``<Op>`` elements (e.g. ``mixEffectBlockIndex``,
        ``input``, ``onAir``).
      - For unknown ops: ``'rawParams'`` — hex-encoded parameter bytes,
        so the op survives a round trip even without semantic decoding.

    ``mx`` is an optional atemwire mixerstate dict; passing it improves
    source-id → symbolic-name resolution for `<input>` parameters
    (renamed cameras come back with their renamed labels).
    """
    out: List[dict] = []
    o = 0
    n = len(raw)
    while o < n:
        if o + 4 > n:
            logger.warning("macro bytecode truncated at offset %d (need 4-byte header)", o)
            break
        op_len = _u16le(raw, o)
        op_type = _u16le(raw, o + 2)
        if op_len < 4:
            logger.warning("macro bytecode: bogus op_len %d at offset %d", op_len, o)
            break
        if o + op_len > n:
            logger.warning(
                "macro bytecode: op_len %d at offset %d overruns buffer (len %d)",
                op_len, o, n)
            break
        params = raw[o + 4:o + op_len]
        entry = _KNOWN_OPS.get(op_type)
        if entry is not None:
            xml_id, decoder, _encoder = entry
            try:
                attrs = decoder(params, mx)
            except Exception as e:  # noqa: BLE001
                logger.warning("decode failure for op 0x%04x at %d: %s",
                               op_type, o, e)
                attrs = {'rawParams': params.hex(), 'decodeError': str(e)}
                xml_id = f'Unknown_0x{op_type:04x}'
            out.append({'id': xml_id, **attrs})
        else:
            out.append({
                'id': f'Unknown_0x{op_type:04x}',
                'rawParams': params.hex(),
            })
        o += op_len
    return out


def encode_single_op(op: dict) -> bytes:
    """Encode one op-descriptor dict into its wire bytecode (4-byte
    header + params).

    The op dict must have:
      - ``'id'``: the Software Control op id (matches what the decoder
        produced) OR ``'Unknown_0xNNNN'`` for op codes the decoder
        couldn't map. Unknown ops round-trip via ``rawParams`` (hex).
      - per-op param attributes — same shape as the decoder output.

    Raises ``ValueError`` if the op id has no encoder registered, or
    if a known op is missing a required attribute, or if Unknown ops
    have malformed ``rawParams``. Pure function — never swallows.

    This is the per-op extraction of ``encode_macro_bytecode``. Use it
    directly when the caller needs to decide per-op what to do with
    unknown ops (skip vs. fail). ``encode_macro_bytecode`` batches a
    list and is strict (any unknown op aborts the whole batch).
    """
    op_id = op.get('id', '')

    # Unknown op-id — round-trip via rawParams. The decoder always
    # stamps ``Unknown_0xNNNN`` with hex params; we reverse that.
    if op_id.startswith('Unknown_0x'):
        try:
            op_code = int(op_id[len('Unknown_0x'):], 16)
        except ValueError as exc:
            raise ValueError(
                f"malformed Unknown op id {op_id!r}") from exc
        params_hex = op.get('rawParams', '')
        try:
            params = bytes.fromhex(params_hex)
        except ValueError as exc:
            raise ValueError(
                f"op {op_id} has malformed rawParams: {exc}") from exc
        op_len = 4 + len(params)
        return struct.pack('<HH', op_len, op_code) + params

    # Known op-id — look up the encoder and pack.
    entry = _ENCODE_BY_ID.get(op_id)
    if entry is None:
        raise ValueError(f"no encoder for op id {op_id!r}")
    op_code, encoder = entry
    try:
        params = encoder(op)
    except KeyError as exc:
        raise ValueError(
            f"op {op_id}: missing attribute {exc.args[0]!r}") from exc
    op_len = 4 + len(params)
    return struct.pack('<HH', op_len, op_code) + params


def encode_macro_bytecode(ops: List[dict]) -> bytes:
    """Inverse of ``decode_macro_bytecode``: pack a list of op-descriptor
    dicts back into the wire bytecode the live ATEM stores.

    Strict: any unknown op-id, missing attribute, or unresolvable
    source name raises ``ValueError`` and aborts the whole batch.
    Callers that want skip-on-unknown semantics should iterate
    ``encode_single_op`` themselves and decide per-op.
    """
    return b''.join(encode_single_op(op) for op in ops)


# Public surface — callers shouldn't dig into internal helpers.
__all__ = [
    'download_macro_bytecode',
    'decode_macro_bytecode',
    'encode_single_op',
    'encode_macro_bytecode',
    'upload_macro_bytecode',
    'MACRO_STORE',
]
