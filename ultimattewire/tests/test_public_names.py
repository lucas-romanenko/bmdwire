# SPDX-License-Identifier: MIT
"""``read_preamble`` and ``parse_section`` (the 9998 prelude: read it, take
one section out) are exported from the package since 1.14.1, for a caller
that wants a unit's identity without a client session (AV Server's device
facts). They are the ``_preamble`` module's functions, which stay."""
import ultimattewire
from ultimattewire import _preamble


def test_preamble_readers_are_exported_from_the_package():
    assert ultimattewire.read_preamble is _preamble.read_preamble
    assert ultimattewire.parse_section is _preamble.parse_section
    assert {'read_preamble', 'parse_section'} <= set(ultimattewire.__all__)


def test_parse_section_takes_one_section():
    text = 'IDENTITY:\nModel: Ultimatte 12 4K\nLabel: Keyer\n\nVERSION:\nProduct ID: BE8F\n\nEND PRELUDE:\n'
    assert ultimattewire.parse_section(text, 'IDENTITY') == 'Model: Ultimatte 12 4K\nLabel: Keyer'
