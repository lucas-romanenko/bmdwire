# SPDX-License-Identifier: LGPL-3.0-only
"""A Fairlight strip op with no ``sourceId`` encodes the stereo strip
(bmdwire#16).

``_resolve_source_id`` defaulted a missing or empty ``sourceId`` to
``0xFFFFFFFFFFFF0000`` (-65536 as i64), while its own comment named the
stereo sentinel -65280, which is ``0xFFFFFFFFFFFF0100``. Every live capture
in this suite, and all 24 Fairlight strip ops in a macro downloaded from a
1 M/E Constellation HD, carry ``…0100``; none carries ``…0000``. ASC writes
it unsigned, ``18446744073709486336``.
"""
import struct

import pytest

from atemwire.macrotransfer import decode_macro_bytecode, encode_single_op

STEREO = '18446744073709486336'      # 2**64 - 65280 = 0xFFFFFFFFFFFF0100

OPS = [
    # _e_fairlight_strip_fader_gain
    {'id': 'FairlightAudioMixerInputSourceFaderGain', 'input': 'Camera1', 'gain': '-0.05'},
    # _e_fairlight_strip_mix_type
    {'id': 'FairlightAudioMixerInputSourceMixType', 'input': 'Camera2', 'mixType': 'On'},
    # _strip_prefix_encode, through _e_strip_value_fp and _e_eq_band_enabled
    {'id': 'FairlightAudioMixerInputSourceEqualiserGain', 'input': 'Camera1', 'gain': '-3'},
    {'id': 'FairlightAudioMixerInputSourceEqualiserBandEnabled', 'input': 'Camera1',
     'band': '2', 'enabled': 'True'},
]
IDS = [op['id'].replace('FairlightAudioMixerInputSource', '') for op in OPS]


@pytest.mark.parametrize('missing', [None, ''], ids=['absent', 'empty'])
@pytest.mark.parametrize('op', OPS, ids=IDS)
def test_a_missing_source_id_encodes_the_stereo_strip(op, missing):
    without = dict(op)
    if missing is not None:
        without['sourceId'] = missing
    assert encode_single_op(without) == encode_single_op(dict(op, sourceId=STEREO))


@pytest.mark.parametrize('op', OPS, ids=IDS)
def test_it_decodes_back_to_the_stereo_id(op):
    raw = encode_single_op(op)
    # The 8-byte id after source (u16) and marker (u16), as the wire has it.
    assert raw[4 + 4:4 + 12] == struct.pack('<Q', 0xFFFFFFFFFFFF0100)
    [decoded] = decode_macro_bytecode(raw)
    assert decoded['sourceId'] == STEREO
