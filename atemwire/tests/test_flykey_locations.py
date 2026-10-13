# SPDX-License-Identifier: LGPL-3.0-only
"""Fly-key Run to Infinity locations, pinned to a capture of ATEM Software
Control (bmdwire#10).

On 2026-10-13 ASC 9.3.2 ran USK 1 of M/E 1 on a 1 M/E Constellation HD
(firmware 9.2.1) to each of its ten locations. Wireshark saw one RFlK per
click, payload ``02 00 00 6e 04 XX 00 00`` with the location at byte 5, and
the switcher's KeFS echoes carried the same byte. The same ten runs were
recorded into macro slot 20 (index 19); ``FLYKEY_10_MACRO`` is that slot's
bytecode as downloaded from the switcher, and ASC's Save Switcher State named
its ops as ``CLICKS`` does, in order.

So the live command and macro op 0x0056 share one enum: 0 CentreOfKey and
1-9 the 3x3 grid, whose middle (5) is MiddleCentre. Before this the macro
table had no 5 and refused MiddleCentre, and the op's docstring called 5 the
centre and had no 0.
"""
import pytest

from atemwire.helpers import (
    FLYKEY_LOCATION_BY_NAME, FLYKEY_LOCATION_NAMES, flykey_location,
)
from atemwire.macrotransfer import (
    decode_macro_bytecode, encode_macro_bytecode, encode_single_op,
)
from atemwire.macrotransfer._helpers import (
    _FLYKEY_LOCATION_NAMES, _FLYKEY_LOCATION_TO_INT,
)
from atemwire.messages.upstream_keyer import (
    KeyPropertiesFlyField, run_flying_key_infinite_direction,
)

# Click order: ASC's name for each op of the recorded macro, and the byte
# that click's RFlK carried at payload byte 5.
CLICKS = [
    ('TopLeft', 0x01),
    ('TopCentre', 0x02),
    ('TopRight', 0x03),
    ('MiddleLeft', 0x04),
    ('CentreOfKey', 0x00),
    ('MiddleCentre', 0x05),
    ('MiddleRight', 0x06),
    ('BottomLeft', 0x07),
    ('BottomCentre', 0x08),
    ('BottomRight', 0x09),
]

# Slot 20 as downloaded: ten 0x0056 ops, each u16 LE length 8, u16 LE code,
# then M/E, keyer, location and marker 0x02.
FLYKEY_10_MACRO = bytes.fromhex(
    '0800560000000102'  # TopLeft
    '0800560000000202'  # TopCentre
    '0800560000000302'  # TopRight
    '0800560000000402'  # MiddleLeft
    '0800560000000002'  # CentreOfKey
    '0800560000000502'  # MiddleCentre
    '0800560000000602'  # MiddleRight
    '0800560000000702'  # BottomLeft
    '0800560000000802'  # BottomCentre
    '0800560000000902'  # BottomRight
)


class _FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd.get_command())


def _op(location):
    return {'id': 'FlyKeyRunToInfinity', 'mixEffectBlockIndex': '0',
            'keyIndex': '0', 'location': location}


def test_one_table_for_the_command_and_the_macro_codec():
    assert _FLYKEY_LOCATION_NAMES is FLYKEY_LOCATION_NAMES
    assert _FLYKEY_LOCATION_TO_INT is FLYKEY_LOCATION_BY_NAME
    assert FLYKEY_LOCATION_NAMES == {byte: name for name, byte in CLICKS}
    assert FLYKEY_LOCATION_BY_NAME == dict(CLICKS)


@pytest.mark.parametrize('name, byte', CLICKS)
def test_rflk_payload_matches_the_capture(name, byte):
    # ASC sends 0x6e in padding byte 3; atemwire sends 0.
    for direction in (name, byte):
        conn = _FakeConn()
        run_flying_key_infinite_direction(conn, keyer=0, direction=direction,
                                          me=0)
        cmd, = conn.sent
        assert cmd[4:8] == b'RFlK'
        assert cmd[8:] == bytes([0x02, 0x00, 0x00, 0x00, 0x04, byte, 0x00, 0x00])


@pytest.mark.parametrize('name, byte', CLICKS)
def test_kefs_reads_the_echoed_location(name, byte):
    # Payload bytes 0-6 of the two KeFS per click: the echo (run-to 4,
    # flags 0), then the run's end a second later (at infinite, flag 0x08).
    echo = KeyPropertiesFlyField(bytes([0, 0, 0, 0, 0x04, byte, 0x00]))
    end = KeyPropertiesFlyField(bytes([0, 0, 0, 0, 0x00, byte, 0x08]))
    assert echo.run_to_infinite_index == byte
    assert end.run_to_infinite_index == byte
    assert FLYKEY_LOCATION_NAMES[end.run_to_infinite_index] == name
    assert not echo.at_keyframe_infinite and end.at_keyframe_infinite


def test_recorded_macro_decodes_to_ascs_names_in_click_order():
    assert decode_macro_bytecode(FLYKEY_10_MACRO) == [
        _op(name) for name, _ in CLICKS]


def test_recorded_macro_encodes_back_byte_for_byte():
    assert encode_macro_bytecode(
        decode_macro_bytecode(FLYKEY_10_MACRO)) == FLYKEY_10_MACRO


@pytest.mark.parametrize('name, byte', CLICKS)
def test_each_name_encodes_to_its_byte(name, byte):
    # MiddleCentre used to raise "unknown FlyKey location".
    assert encode_single_op(_op(name)) == bytes(
        [0x08, 0x00, 0x56, 0x00, 0x00, 0x00, byte, 0x02])


def test_flykey_location_lookup():
    assert flykey_location('MiddleCentre') == 5
    assert flykey_location('CentreOfKey') == 0
    assert flykey_location(5) == 5
    assert flykey_location('5') == 5      # numbers pass through, as before
    assert flykey_location('Centre') is None
    assert flykey_location(None) is None


def test_an_unknown_name_still_refuses_to_encode():
    with pytest.raises(ValueError, match="unknown FlyKey location 'Centre'"):
        encode_single_op(_op('Centre'))
