# SPDX-License-Identifier: LGPL-3.0-only
"""Macro floats decode as ATEM Software Control writes them: six significant
digits (bmdwire#17).

``macro_floats_asc.xml`` holds, verbatim, every distinct macro op with a
fractional value in twelve ASC Save Switcher State exports from one 1 M/E
Constellation HD (2026-04-27 to 2026-10-13), leaving out HyperDeck ops (they
carry addresses) and ops atemwire has no codec for. ASC writes those strings
from the bytecode the switcher holds, so decoding the bytes each op encodes to
must give ASC's strings back. Six decimals missed 50 of the 399 distinct
attribute strings in those files (``-327.679993`` for ``-327.68``,
``-0.080002`` for ``-0.0800018``, ``7.199997`` for ``7.2``, ...); six
significant digits misses none.

``GAIN_FMT`` is the macro recorded for the issue in ASC 9.3.2. Of the five
values the recording was meant to hold, it holds two: Camera 1's fader at -inf
and the master fader at -0.08. The bytes are what those strings encode to; the
silenced value's ``ec 51 b8 fe`` is also what bmdctl downloaded for the twelve
silenced strips of another macro on that switcher (its docs/HARDWARE.md,
2026-10-10).
"""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from atemwire.macrotransfer import decode_macro_bytecode, encode_single_op
from atemwire.macrotransfer._helpers import _fmt_scalar

FIXTURE = Path(__file__).with_name('macro_floats_asc.xml')
ASC_OPS = [dict(e.attrib) for e in ET.parse(FIXTURE).getroot().iter('Op')]

_FRACTION = re.compile(r'-?\d+\.\d+(e-?\d+)?')

# (ASC's op from the gain-fmt macro, its bytecode)
GAIN_FMT = [
    ({'id': 'FairlightAudioMixerInputSourceFaderGain', 'input': 'Camera1',
      'sourceId': '18446744073709486336', 'gain': '-327.68'},
     bytes.fromhex('14004b01' '0100' '4643' '0001ffffffffffff' 'ec51b8fe')),
    ({'id': 'FairlightAudioMixerMasterOutFaderGain', 'gain': '-0.0800018'},
     bytes.fromhex('08006b01' '85ebffff')),
]


@pytest.mark.parametrize('op', ASC_OPS,
                         ids=[f"{i}-{op['id']}" for i, op in enumerate(ASC_OPS)])
def test_every_asc_float_decodes_as_asc_wrote_it(op):
    decoded, = decode_macro_bytecode(encode_single_op(op))
    for attr, asc in op.items():
        if _FRACTION.fullmatch(asc):
            assert decoded[attr] == asc, attr


@pytest.mark.parametrize('op, raw', GAIN_FMT, ids=['camera1-silenced',
                                                   'master-0.08'])
def test_gain_fmt_macro(op, raw):
    assert decode_macro_bytecode(raw) == [op]
    assert encode_single_op(op) == raw


def test_silenced_master_fader():
    # The minimum fix the issue asked for.
    raw = bytes.fromhex('08006b01ec51b8fe')
    op, = decode_macro_bytecode(raw)
    assert op == {'id': 'FairlightAudioMixerMasterOutFaderGain',
                  'gain': '-327.68'}
    assert encode_single_op(op) == raw


def test_zero_is_written_plainly():
    assert _fmt_scalar(0.0) == '0'
    assert _fmt_scalar(-0.0) == '0'
