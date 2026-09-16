# SPDX-License-Identifier: LGPL-3.0-only
"""The two DSL additions of 0.15: ``string(mask_bit=...)`` and ``tail``."""

import struct

import pytest

from atemwire.messages._dsl import Recv, Send, boolean, string, tail, u8, u16


class _Named(Send):
    CODE = 'TEST'
    SIZE = 12
    MASK_AT = 0
    name  = string(at=1, size=8, mask_bit=0)
    level = u16   (at=10, mask_bit=1)

    def __init__(self, name=None, level=None):
        super().__init__(name=name, level=level)


def test_string_mask_bit_sets_the_mask_only_when_given():
    assert _Named(name='abc').get_command()[8:] == struct.pack('>B 8s xH', 0x01, b'abc', 0)
    assert _Named(level=7).get_command()[8:] == struct.pack('>B 8s xH', 0x02, b'', 7)
    assert _Named(name='x', level=1).get_command()[8:] == struct.pack('>B 8s xH', 0x03, b'x', 1)
    assert _Named().get_command()[8:] == bytes(12)


class _WithTail(Send):
    CODE = 'TAIL'
    SIZE = 4
    kind = u8  (at=0)
    data = tail(pad_to=8)

    def __init__(self, kind, data=None):
        super().__init__(kind=kind, data=data)


def test_tail_appends_after_size_and_pads_short_values():
    assert _WithTail(1).get_command()[8:] == bytes([1, 0, 0, 0])                 # no tail: fixed bytes only
    assert _WithTail(1, b'ab').get_command()[8:] == bytes([1, 0, 0, 0]) + b'ab' + bytes(6)
    long = bytes(range(12))
    assert _WithTail(1, long).get_command()[8:] == bytes([1, 0, 0, 0]) + long   # longer than pad_to: untouched


def test_tail_header_length_counts_the_variable_part():
    raw = _WithTail(1, b'abc').get_command()
    assert struct.unpack('>H', raw[:2])[0] == len(raw) == 8 + 4 + 8


class _TailRecv(Recv):
    CODE = 'TAIR'
    SIZE = 2
    kind = u8  (at=0)
    rest = tail()


def test_tail_unpacks_everything_after_size():
    r = _TailRecv(bytes([9, 0]) + b'payload')
    assert r.kind == 9 and r.rest == b'payload'


def test_tail_must_be_last_and_single():
    with pytest.raises(TypeError):
        class _Bad(Send):
            CODE = 'BAD1'; SIZE = 2
            data = tail()
            after = u8(at=0)
    with pytest.raises(TypeError):
        class _Bad2(Send):
            CODE = 'BAD2'; SIZE = 2
            a = tail(); b = tail()
    with pytest.raises(TypeError):
        class _Bad3(Send):
            CODE = 'BAD3'
            data = tail()          # no SIZE to anchor it
