# SPDX-License-Identifier: LGPL-3.0-only
"""Unit tests for atemwire.macrotransfer.encode_macro_bytecode.

Each test feeds a single op-id with hand-crafted attributes and
asserts the produced bytes match the wire format documented in
``atemwire/docs/MACRO_FORMAT.md``. The encoder is the inverse of the
decoder; tests here mirror tests in test_macro_decoder.py.
"""
import struct

import pytest

from atemwire.macrotransfer import (
    decode_macro_bytecode, encode_macro_bytecode
)


def _pack_op(op_code: int, params: bytes) -> bytes:
    op_len = 4 + len(params)
    return struct.pack('<HH', op_len, op_code) + params


# ----- Single-op encoders ----------------------------------------------------

def test_encode_program_input_camera1():
    out = encode_macro_bytecode([
        {'id': 'ProgramInput', 'mixEffectBlockIndex': '0',
         'input': 'Camera1'},
    ])
    # op_len=8, op_code=0x0002, params: u8 me, u8 _, u16 LE source=1
    assert out == _pack_op(0x0002, bytes([0, 0]) + struct.pack('<H', 1))


def test_encode_preview_input_numeric():
    out = encode_macro_bytecode([
        {'id': 'PreviewInput', 'mixEffectBlockIndex': '0',
         'input': '3010'},
    ])
    assert out == _pack_op(0x0003, bytes([0, 0]) + struct.pack('<H', 3010))


def test_encode_cut_no_args():
    out = encode_macro_bytecode([
        {'id': 'Cut', 'mixEffectBlockIndex': '0'},
    ])
    # 4 zero bytes (me + 3 unused)
    assert out == _pack_op(0x0004, bytes(4))


def test_encode_macro_sleep_60_frames():
    out = encode_macro_bytecode([
        {'id': 'MacroSleep', 'frames': '60'},
    ])
    # u32 LE frames — no leading padding (decoder reads from offset 0).
    assert out == _pack_op(0x0007, struct.pack('<I', 60))


def test_encode_transition_style_dip():
    out = encode_macro_bytecode([
        {'id': 'TransitionStyle', 'mixEffectBlockIndex': '0',
         'style': 'Dip'},
    ])
    # me, style=1 (Dip), 2B pad
    assert out == _pack_op(0x0083, bytes([0, 1, 0, 0]))


def test_encode_transition_source_combined_layers():
    out = encode_macro_bytecode([
        {'id': 'TransitionSource', 'mixEffectBlockIndex': '0',
         'source': 'Background,Key2'},
    ])
    # me, _, u16 LE mask=0x05 (Background bit 0 + Key2 bit 2)
    assert out == _pack_op(0x0084,
                           bytes([0, 0]) + struct.pack('<H', 0x0005))


def test_encode_key_on_air_true():
    out = encode_macro_bytecode([
        {'id': 'KeyOnAir', 'mixEffectBlockIndex': '0',
         'keyIndex': '1', 'onAir': 'True'},
    ])
    assert out == _pack_op(0x0027, bytes([0, 1, 1, 0]))


def test_encode_luma_clip_marker():
    """The marker u16 at params offset 2 is uninitialized memory in
    Software Control's emit. Live recordings from a production switcher (Lots XML, 2026-04-30)
    consistently produce 0x000c here; older fixtures had 0x0000. The
    encoder uses 0x000c to match the live default — either is
    functionally equivalent (ATEM ignores the byte)."""
    out = encode_macro_bytecode([
        {'id': 'LumaKeyClip', 'mixEffectBlockIndex': '0',
         'keyIndex': '0', 'clip': '0.5'},
    ])
    # 0.5 fraction (50%) → Q16.16: 0.5 × 65536 = 32768.
    expected_params = (bytes([0, 0])
                       + struct.pack('<H', 0x000C)
                       + struct.pack('<I', 32768))
    assert out == _pack_op(0x0029, expected_params)


def test_encode_dsk_pre_multiply_zero_marker():
    out = encode_macro_bytecode([
        {'id': 'DownstreamKeyPreMultiply', 'keyIndex': '0',
         'preMultiply': 'False'},
    ])
    # The marker bytes used to be 0x026c; live ATEM recordings use 0.
    assert out == _pack_op(0x00A4, bytes([0, 0, 0, 0]))


def test_encode_aux_input_camera1():
    out = encode_macro_bytecode([
        {'id': 'AuxiliaryInput', 'auxiliaryIndex': '0',
         'input': 'Camera1'},
    ])
    # u8 aux, u8 _, u16 LE source
    assert out == _pack_op(0x001F, bytes([0, 0]) + struct.pack('<H', 1))


# ----- Fairlight ops (added 2026-04-30 — verified live on a production switcher) --------------

def test_encode_fairlight_strip_fader_gain_camera1():
    """Op 0x014b — 16-byte params: source(2) + marker(2) + sourceId(8) +
    gain×65536 i32 LE. Verified against live recording."""
    out = encode_macro_bytecode([
        {'id': 'FairlightAudioMixerInputSourceFaderGain',
         'input': 'Camera1',
         'sourceId': '18446744073709486336',
         'gain': '-0.05'},
    ])
    # source=1, marker=0x4346, sourceId=0xFFFFFFFFFFFF0000 (i64 LE),
    # gain = round(-0.05 × 65536) = -3277
    expected_params = (
        struct.pack('<H', 1)                # source
        + struct.pack('<H', 0x4346)          # marker
        + struct.pack('<Q', 0xFFFFFFFFFFFF0100)  # sourceId
        + struct.pack('<i', -3277)           # gain × 65536
    )
    assert out == _pack_op(0x014B, expected_params)
    # Live capture (slot 80 of v3 discovery) for the same setup:
    assert out.hex() == (
        '14004b01010046430001ffffffffffff33f3ffff'
    )


def test_encode_fairlight_strip_mix_type_off():
    """Op 0x014a — 16-byte params: source(2) + marker(2) + sourceId(8) +
    state(2) + trailing-marker(2). State: 1=Off, 2=On, 4=AFV."""
    out = encode_macro_bytecode([
        {'id': 'FairlightAudioMixerInputSourceMixType',
         'input': 'Camera1',
         'sourceId': '18446744073709486336',
         'mixType': 'Off'},
    ])
    expected_params = (
        struct.pack('<H', 1)                # source
        + struct.pack('<H', 0x4346)          # marker
        + struct.pack('<Q', 0xFFFFFFFFFFFF0100)  # sourceId
        + struct.pack('<H', 1)               # state = Off
        + struct.pack('<H', 0x000C)          # trailing marker
    )
    assert out == _pack_op(0x014A, expected_params)
    # Live capture (slot 91 of v2 discovery):
    assert out.hex() == (
        '14004a01010046430001ffffffffffff01000c00'
    )


def test_encode_fairlight_master_fader_gain():
    """Op 0x016b — 4-byte params: i32 LE gain × 65536. Master output
    fader (no source/sourceId since it's the master)."""
    out = encode_macro_bytecode([
        {'id': 'FairlightAudioMixerMasterOutFaderGain',
         'gain': '-1.08'},
    ])
    # gain = round(-1.08 × 65536) = -70779
    assert out == _pack_op(0x016B, struct.pack('<i', -70779))
    # Live capture (slot 83 of v3 discovery):
    assert out.hex() == '08006b0185ebfeff'


def test_decode_fairlight_strip_fader_gain():
    """Live-recorded bytecode decodes to the expected XML attrs."""
    raw = bytes.fromhex('14004b01010046430001ffffffffffff33f3ffff')
    out = decode_macro_bytecode(raw)
    assert out == [{
        'id': 'FairlightAudioMixerInputSourceFaderGain',
        'input': 'Camera1',
        'sourceId': '18446744073709486336',
        'gain': '-0.050003',
    }]


def test_decode_fairlight_strip_mix_type():
    raw = bytes.fromhex('14004a01010046430001ffffffffffff01000c00')
    out = decode_macro_bytecode(raw)
    assert out == [{
        'id': 'FairlightAudioMixerInputSourceMixType',
        'input': 'Camera1',
        'sourceId': '18446744073709486336',
        'mixType': 'Off',
    }]


def test_encode_ftb_enabled_true():
    """Op 0x0202 — params: u8 mEBI, u8 enabled, u16 0."""
    out = encode_macro_bytecode([
        {'id': 'FadeToBlackEnabled',
         'mixEffectBlockIndex': '0', 'enabled': 'True'},
    ])
    assert out == _pack_op(0x0202, bytes([0, 1, 0, 0]))


def test_encode_ftb_enabled_false():
    out = encode_macro_bytecode([
        {'id': 'FadeToBlackEnabled',
         'mixEffectBlockIndex': '0', 'enabled': 'False'},
    ])
    assert out == _pack_op(0x0202, bytes([0, 0, 0, 0]))


def test_encode_ftb_enabled_preserves_uninitialized_mEBI_byte():
    """Software Control's `mixEffectBlockIndex` byte is uninitialized
    memory garbage that the ATEM ignores. The encoder must round-trip
    whatever value the XML carries — no normalization."""
    for garbage_byte in (224, 14, 67, 139, 177, 170, 175, 129):
        out = encode_macro_bytecode([
            {'id': 'FadeToBlackEnabled',
             'mixEffectBlockIndex': str(garbage_byte),
             'enabled': 'True'},
        ])
        assert out == _pack_op(0x0202, bytes([garbage_byte, 1, 0, 0])), (
            f"mEBI={garbage_byte} did not round-trip")


def test_decode_ftb_enabled_true():
    raw = bytes.fromhex('0800020203010000')
    out = decode_macro_bytecode(raw)
    assert out == [{
        'id': 'FadeToBlackEnabled',
        'mixEffectBlockIndex': '3',
        'enabled': 'True',
    }]


def test_decode_ftb_enabled_false():
    raw = bytes.fromhex('0800020206000000')
    out = decode_macro_bytecode(raw)
    assert out == [{
        'id': 'FadeToBlackEnabled',
        'mixEffectBlockIndex': '6',
        'enabled': 'False',
    }]


def test_decode_ftb_enabled_keeps_garbage_mEBI_byte():
    """Confirm the garbage-byte is preserved through decode (sanity:
    decode reads it as-is, encode round-trips)."""
    # SC-observed garbage value 0xb7 = 183.
    raw = bytes.fromhex('08000202b7000000')
    out = decode_macro_bytecode(raw)
    assert out[0]['mixEffectBlockIndex'] == '183'
    assert out[0]['enabled'] == 'False'


def test_encode_video_mode_1080p60():
    """Op 0x000c — 4-byte params: u8 mode_id + 3 pad. 1080p60 = mode 27."""
    out = encode_macro_bytecode([
        {'id': 'VideoMode', 'videoMode': '1080p60'},
    ])
    assert out == _pack_op(0x000C, bytes([27, 0, 0, 0]))


def test_encode_video_mode_720p60():
    """Verified against live capture (slot 72 of discover_more_ops.py)
    where the ATEM was running 720p60 (mode=28)."""
    out = encode_macro_bytecode([
        {'id': 'VideoMode', 'videoMode': '720p60'},
    ])
    assert out == _pack_op(0x000C, bytes([28, 0, 0, 0]))
    assert out.hex() == '08000c001c000000'


def test_encode_video_mode_accepts_numeric():
    """Encoder accepts integer mode-id strings as well as named strings."""
    out = encode_macro_bytecode([
        {'id': 'VideoMode', 'videoMode': '27'},
    ])
    assert out == _pack_op(0x000C, bytes([27, 0, 0, 0]))


def test_decode_video_mode_canonical_name():
    """Decoder normalises mode_int to the numerical name (525i5994 not
    NTSC) — matches Software Control's XML preference."""
    raw = bytes.fromhex('08000c0000000000')  # mode 0
    out = decode_macro_bytecode(raw)
    assert out == [{'id': 'VideoMode', 'videoMode': '525i5994'}]


def test_decode_video_mode_unknown_int_falls_back_to_string():
    raw = bytes.fromhex('08000c00ff000000')  # mode 255 — not a real mode
    out = decode_macro_bytecode(raw)
    assert out == [{'id': 'VideoMode', 'videoMode': '255'}]


def test_decode_fairlight_master_fader_gain():
    raw = bytes.fromhex('08006b0185ebfeff')
    out = decode_macro_bytecode(raw)
    assert out == [{
        'id': 'FairlightAudioMixerMasterOutFaderGain',
        'gain': '-1.080002',
    }]


def test_fairlight_byte_identical_roundtrip_for_live_captures():
    """Bytecode → decode → encode → identical bytes for every live
    capture. This is the strongest correctness guarantee — if the
    decoder/encoder pair drift on any field, the bytes diverge."""
    captures = [
        '14004b01010046430001ffffffffffff33f3ffff',  # CAM1 vol -0.05
        '14004b01020046430001ffffffffffff33f3ffff',  # CAM2 vol -0.05
        '14004a01010046430001ffffffffffff01000c00',  # CAM1 mix Off
        '14004a01010046430001ffffffffffff02000c00',  # CAM1 mix On (synthetic)
        '14004a01010046430001ffffffffffff04000c00',  # CAM1 mix AFV (synthetic)
        '08006b0185ebfeff',                          # master -1.08
        '08006b01f5e8ffff',                          # master -0.09
        '08000c001c000000',                          # video-mode 720p60
        '08000c001b000000',                          # video-mode 1080p60 (synthetic)
        '08000c0000000000',                          # video-mode 525i5994 (synthetic)
        '0800020203010000',                          # FTB enabled mEBI=3
        '0800020206000000',                          # FTB disabled mEBI=6
        '08000202b7000000',                          # FTB disabled mEBI=183 (garbage)
        '0800020207010000',                          # FTB enabled mEBI=7
        '08000202b8010000',                          # FTB enabled mEBI=184 (garbage)
    ]
    for hex_in in captures:
        raw = bytes.fromhex(hex_in)
        ops = decode_macro_bytecode(raw)
        re = encode_macro_bytecode(ops)
        assert re == raw, (
            f"\ninput :  {hex_in}\noutput: {re.hex()}\nops: {ops}"
        )


# ----- Round-trips against the decoder --------------------------------------

@pytest.mark.parametrize('op,attrs', [
    # Source attributes use the decoder's preferred symbolic form
    # (the decoder normalises numeric source ids to ``Camera<N>`` for
    # camera ranges, ``Color1`` etc. for internal sources). Encoder
    # accepts both numeric and symbolic; the decoder always produces
    # symbolic.
    ('ProgramInput', {'mixEffectBlockIndex': '0', 'input': 'Camera1'}),
    ('PreviewInput', {'mixEffectBlockIndex': '0', 'input': 'Camera2'}),
    ('Cut', {'mixEffectBlockIndex': '0'}),
    ('Auto', {'mixEffectBlockIndex': '0'}),
    ('FadeToBlack', {'mixEffectBlockIndex': '0'}),
    ('TransitionStyle', {'mixEffectBlockIndex': '0', 'style': 'Wipe'}),
    ('TransitionSource', {'mixEffectBlockIndex': '0',
                          'source': 'Background,Key1,Key3'}),
    ('MacroSleep', {'frames': '15'}),
    ('AuxiliaryInput', {'auxiliaryIndex': '0', 'input': 'Camera5'}),
    ('KeyOnAir', {'mixEffectBlockIndex': '0', 'keyIndex': '0',
                  'onAir': 'True'}),
    ('KeyType', {'mixEffectBlockIndex': '0', 'keyIndex': '0',
                 'type': 'Chroma'}),
    ('KeyMaskEnable', {'mixEffectBlockIndex': '0', 'keyIndex': '0',
                       'enable': 'True'}),
    ('LumaKeyClip', {'mixEffectBlockIndex': '0', 'keyIndex': '0',
                     'clip': '0.5'}),
    ('LumaKeyGain', {'mixEffectBlockIndex': '0', 'keyIndex': '0',
                     'gain': '0.25'}),
    ('LumaKeyInvert', {'mixEffectBlockIndex': '0', 'keyIndex': '0',
                       'invert': 'False'}),
    ('LumaKeyPreMultiply', {'mixEffectBlockIndex': '0', 'keyIndex': '0',
                            'preMultiply': 'True'}),
    ('DownstreamKeyOnAir', {'keyIndex': '0', 'onAir': 'True'}),
    ('DownstreamKeyTie', {'keyIndex': '0', 'tie': 'True'}),
    ('DownstreamKeyTie', {'keyIndex': '1', 'tie': 'False'}),
    ('HyperDeckNetworkAddress', {'slot': '1', 'networkAddress': '10.20.30.40'}),
    ('HyperDeckNetworkAddress', {'slot': '0', 'networkAddress': '192.0.2.10'}),
    ('HyperDeckInput', {'slot': '3', 'input': '7'}),
    ('DownstreamKeyAuto', {'keyIndex': '0'}),
    ('DownstreamKeyRate', {'keyIndex': '0', 'rate': '25'}),
    ('DownstreamKeyMaskEnable', {'keyIndex': '0', 'enable': 'True'}),
    ('DownstreamKeyPreMultiply', {'keyIndex': '0', 'preMultiply': 'True'}),
    # Fairlight ops added 2026-04-30. The decoder formats gain as
    # `bytecode_int / 65536` to 6 decimal places, so round-trip
    # requires gain values exactly representable on that 1/65536
    # grid (e.g. 0.5 = 32768/65536).
    ('FairlightAudioMixerInputSourceFaderGain',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'gain': '0.5'}),
    ('FairlightAudioMixerInputSourceMixType',
     {'input': 'Camera2', 'sourceId': '18446744073709486336',
      'mixType': 'On'}),
    ('FairlightAudioMixerMasterOutFaderGain', {'gain': '0'}),
    # VideoMode added 2026-04-30. Decoder normalises to the numerical
    # name (e.g. 720p60 not "NTSC") for Software Control parity.
    ('VideoMode', {'videoMode': '1080p60'}),
    ('VideoMode', {'videoMode': '720p60'}),
    # FadeToBlackEnabled added 2026-04-30. mEBI is round-tripped as-is
    # (uninitialized SC byte the ATEM ignores).
    ('FadeToBlackEnabled',
     {'mixEffectBlockIndex': '0', 'enabled': 'True'}),
    ('FadeToBlackEnabled',
     {'mixEffectBlockIndex': '177', 'enabled': 'False'}),
    # Comprehensive close-out (Lots XML alignment, 2026-04-30) — Tier 1.
    ('TransitionMixRate', {'mixEffectBlockIndex': '0', 'rate': '55'}),
    ('TransitionDipRate', {'mixEffectBlockIndex': '0', 'rate': '50'}),
    ('TransitionDipInput', {'mixEffectBlockIndex': '0', 'input': 'Color1'}),
    ('TransitionWipeRate', {'mixEffectBlockIndex': '0', 'rate': '50'}),
    ('TransitionWipePattern',
     {'mixEffectBlockIndex': '0', 'pattern': 'HorizontalBarnDoor'}),
    ('TransitionWipeBorderSoftness',
     {'mixEffectBlockIndex': '0', 'softness': '0.5'}),
    ('TransitionWipeBorderWidth',
     {'mixEffectBlockIndex': '0', 'width': '0.25'}),
    ('TransitionWipeBorderFillInput',
     {'mixEffectBlockIndex': '0', 'input': 'Color2'}),
    ('TransitionWipeXPosition',
     {'mixEffectBlockIndex': '0', 'xPosition': '0.5'}),
    ('TransitionWipeYPosition',
     {'mixEffectBlockIndex': '0', 'yPosition': '0.25'}),
    ('TransitionWipeAndDVEFlipFlop',
     {'mixEffectBlockIndex': '0', 'flipFlop': 'True'}),
    ('TransitionWipeAndDVEReverse',
     {'mixEffectBlockIndex': '0', 'reverse': 'False'}),
    ('TransitionDVERate', {'mixEffectBlockIndex': '0', 'rate': '50'}),
    ('TransitionDVEPattern',
     {'mixEffectBlockIndex': '0', 'pattern': 'GraphicLogoWipe'}),
    ('TransitionDVEFillInput',
     {'mixEffectBlockIndex': '0', 'input': 'Color1'}),
    ('TransitionDVECutInput',
     {'mixEffectBlockIndex': '0', 'input': 'MediaPlayer1'}),
    ('TransitionDVECutInputEnable',
     {'mixEffectBlockIndex': '0', 'enable': 'True'}),
    ('TransitionStingerSourceMediaPlayer',
     {'mixEffectBlockIndex': '0', 'mediaPlayer': '1'}),
    ('TransitionStingerPreRoll',
     {'mixEffectBlockIndex': '0', 'preRoll': '3'}),
    ('TransitionStingerClipDuration',
     {'mixEffectBlockIndex': '0', 'clipDuration': '74'}),
    ('TransitionStingerTriggerPoint',
     {'mixEffectBlockIndex': '0', 'triggerPoint': '26'}),
    ('TransitionStingerMixRate',
     {'mixEffectBlockIndex': '0', 'mixRate': '6'}),
    ('TransitionStingerDVEClip',
     {'mixEffectBlockIndex': '0', 'clip': '0.5'}),
    ('TransitionStingerDVEGain',
     {'mixEffectBlockIndex': '0', 'gain': '0.25'}),
    ('TransitionStingerDVEInvert',
     {'mixEffectBlockIndex': '0', 'invert': 'True'}),
    ('TransitionStingerDVEPreMultiply',
     {'mixEffectBlockIndex': '0', 'preMultiply': 'False'}),
    ('ColorGeneratorHue', {'colorGeneratorIndex': '0', 'hue': '180'}),
    ('ColorGeneratorSaturation',
     {'colorGeneratorIndex': '0', 'saturation': '0.5'}),
    ('ColorGeneratorLuminescence',
     {'colorGeneratorIndex': '0', 'luminescence': '0.25'}),
    ('FadeToBlackRate', {'mixEffectBlockIndex': '0', 'rate': '50'}),
    ('KeyMaskTop',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'top': '8'}),
    ('KeyMaskBottom',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'bottom': '-8'}),
    ('KeyMaskLeft',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'left': '-15'}),
    ('KeyMaskRight',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'right': '15'}),
    ('DVEAndFlyKeyRate',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'rate': '50'}),
    ('DVEKeyBorderEnable',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'enable': 'True'}),
    ('DVEKeyBorderHue',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'hue': '300'}),
    ('DVEKeyBorderSaturation',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'saturation': '0.5'}),
    ('DVEKeyBorderLuminescence',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'luminescence': '0.5'}),
    ('DVEKeyBorderInnerWidth',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'innerWidth': '1.5'}),
    ('DVEKeyBorderOuterWidth',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'outerWidth': '1.5'}),
    ('DVEKeyBorderInnerSoftness',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'innerSoftness': '20'}),
    ('DVEKeyBorderOuterSoftness',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'outerSoftness': '13'}),
    ('DVEKeyBorderOpacity',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'opacity': '82'}),
    ('DVEKeyShadowEnable',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'enable': 'True'}),
    ('DVEKeyShadowDirection',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'direction': '44'}),
    ('DVEKeyShadowAltitude',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'altitude': '17'}),
    # Tier 2 — Advanced Chroma Key (16 ops, all simple fp wrappers).
    ('AdvancedChromaKeyForegroundLevel',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'foregroundLevel': '0.5'}),
    ('AdvancedChromaKeyBackgroundLevel',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'backgroundLevel': '0.5'}),
    ('AdvancedChromaKeyKeyEdge',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'keyEdge': '0.25'}),
    ('AdvancedChromaKeySpillSuppress',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'spillSuppress': '0.5'}),
    ('AdvancedChromaKeyFlareSuppress',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'flareSuppress': '0.5'}),
    ('AdvancedChromaKeyForegroundBrightness',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0',
      'foregroundBrightness': '-0.25'}),
    ('AdvancedChromaKeyForegroundContrast',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0',
      'foregroundContrast': '0.25'}),
    ('AdvancedChromaKeyForegroundColour',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0',
      'foregroundColour': '0.5'}),
    ('AdvancedChromaKeyForegroundRed',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0',
      'foregroundRed': '0.25'}),
    ('AdvancedChromaKeyForegroundGreen',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0',
      'foregroundGreen': '-0.25'}),
    ('AdvancedChromaKeyForegroundBlue',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0',
      'foregroundBlue': '0.25'}),
    ('AdvancedChromaKeySamplingModeEnabled',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'enabled': 'True'}),
    ('AdvancedChromaKeyPreviewEnabled',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'enabled': 'False'}),
    ('AdvancedChromaKeyCursorXPosition',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'xPosition': '10'}),
    ('AdvancedChromaKeyCursorYPosition',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'yPosition': '0.5'}),
    ('AdvancedChromaKeyCursorSize',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'size': '0.5'}),
    # Tier 2 — FlyKey ops.
    ('FlyKeySetKeyFrame',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'keyFrameIndex': '0'}),
    ('FlyKeyRunToKeyFrame',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'keyFrameIndex': '1'}),
    ('FlyKeyRunToFull',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0'}),
    ('FlyKeyRunToInfinity',
     {'mixEffectBlockIndex': '0', 'keyIndex': '0', 'location': 'TopLeft'}),
    # Tier 2 — Fairlight EQ.
    ('FairlightAudioMixerInputSourceEqualiserGain',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'gain': '0.5'}),
    ('FairlightAudioMixerInputSourceEqualiserBandEnabled',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'band': '0', 'enabled': 'True'}),
    ('FairlightAudioMixerInputSourceEqualiserBandShape',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'band': '2', 'shape': 'BandPass'}),
    ('FairlightAudioMixerInputSourceEqualiserBandRange',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'band': '1', 'range': 'MidLow'}),
    ('FairlightAudioMixerInputSourceEqualiserBandFrequency',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'band': '2', 'frequency': '168'}),
    ('FairlightAudioMixerInputSourceEqualiserBandGain',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'band': '0', 'gain': '2.5'}),
    ('FairlightAudioMixerInputSourceEqualiserBandQFactor',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'band': '2', 'qFactor': '2.5'}),
    # Tier 3 — Fairlight dynamics + headphones.
    ('FairlightAudioMixerInputSourceCompressorEnabled',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'enabled': 'True'}),
    ('FairlightAudioMixerInputSourceCompressorThreshold',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'threshold': '-37.5'}),
    ('FairlightAudioMixerInputSourceCompressorRatio',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'ratio': '2.5'}),
    ('FairlightAudioMixerInputSourceCompressorAttack',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'attack': '5.5'}),
    ('FairlightAudioMixerInputSourceCompressorHold',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'hold': '4.5'}),
    ('FairlightAudioMixerInputSourceCompressorRelease',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'release': '84.25'}),
    ('FairlightAudioMixerInputSourceLimiterThreshold',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'threshold': '-11.25'}),
    ('FairlightAudioMixerInputSourceLimiterAttack',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'attack': '1.5'}),
    ('FairlightAudioMixerInputSourceLimiterHold',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'hold': '2.5'}),
    ('FairlightAudioMixerInputSourceLimiterRelease',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'release': '85.875'}),
    ('FairlightAudioMixerInputSourceExpanderThreshold',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'threshold': '-44.25'}),
    ('FairlightAudioMixerInputSourceExpanderRange',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'range': '20.25'}),
    ('FairlightAudioMixerInputSourceExpanderRatio',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'ratio': '1.25'}),
    ('FairlightAudioMixerInputSourceExpanderAttack',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'attack': '3.5'}),
    ('FairlightAudioMixerInputSourceExpanderHold',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'hold': '9.25'}),
    ('FairlightAudioMixerInputSourceExpanderRelease',
     {'input': 'Camera1', 'sourceId': '18446744073709486336',
      'release': '97.75'}),
    ('FairlightAudioMixerInputSourceInputGain',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'gain': '1.5'}),
    ('FairlightAudioMixerInputSourceDynamicsGain',
     {'input': 'Camera1', 'sourceId': '18446744073709486336', 'gain': '9.5'}),
    ('FairlightAudioMixerHeadphoneOutGain', {'gain': '-13.5'}),
    ('FairlightAudioMixerHeadphoneOutMasterGain', {'gain': '-27.5'}),
    ('FairlightAudioMixerHeadphoneOutSidetoneGain', {'gain': '-18.5'}),
    ('FairlightAudioMixerHeadphoneOutTalkbackGain', {'gain': '-25.5'}),
])
def test_encode_decode_roundtrip(op, attrs):
    """encode → decode produces the same XML attributes (sans 'id')."""
    raw = encode_macro_bytecode([dict(attrs, id=op)])
    decoded = decode_macro_bytecode(raw)
    assert len(decoded) == 1
    expected = {**attrs, 'id': op}
    assert decoded[0] == expected, f'op={op}: {decoded[0]!r} != {expected!r}'


# ----- Unknown ops via rawParams --------------------------------------------

def test_encode_unknown_op_via_raw_params():
    """Decode → re-encode of an op-code not in _KNOWN_OPS round-trips
    via the rawParams attribute."""
    raw_in = _pack_op(0xABCD, b'\xde\xad\xbe\xef')
    decoded = decode_macro_bytecode(raw_in)
    assert decoded == [{'id': 'Unknown_0xabcd', 'rawParams': 'deadbeef'}]
    raw_out = encode_macro_bytecode(decoded)
    assert raw_out == raw_in


def test_encode_unsupported_op_id_raises():
    """An op id with no encoder and no Unknown_0x prefix is a
    programming error — fail loudly so the apply path can drop it."""
    with pytest.raises(ValueError, match='no encoder'):
        encode_macro_bytecode([{'id': 'NotAThing'}])


# ----- Whole-list multi-op encoding -----------------------------------------

def test_encode_two_ops_concatenates():
    out = encode_macro_bytecode([
        {'id': 'Cut', 'mixEffectBlockIndex': '0'},
        {'id': 'Auto', 'mixEffectBlockIndex': '0'},
    ])
    expected = _pack_op(0x0004, bytes(4)) + _pack_op(0x0005, bytes(4))
    assert out == expected


def test_encode_empty_list_yields_empty_bytes():
    assert encode_macro_bytecode([]) == b''
