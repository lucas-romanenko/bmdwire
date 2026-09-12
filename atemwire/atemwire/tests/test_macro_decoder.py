# SPDX-License-Identifier: LGPL-3.0-only
"""Unit tests for atemwire.macrotransfer.decode_macro_bytecode.

Each test exercises one ``_KNOWN_OPS`` entry by feeding a hand-crafted
bytecode buffer (the same shape an ATEM emits when recording the
corresponding wire command) and asserting the decoded XML attributes.

The 11 op-codes added 2026-04-29 (DVE Y-size, DVE X/Y position, DVE
mask enable + four edges, DSK gain/mask-enable/pre-multiply) were
discovered empirically with an op-code discovery script (record each op
in isolation on a live switcher, download and diff the bytecode); the
bytecode samples below come from that run.
"""
import struct

import pytest

from atemwire.macrotransfer import (
    _KNOWN_OPS,
    decode_macro_bytecode,
)


def _pack_op(op_code: int, params: bytes) -> bytes:
    """Frame one op the way ATEM does: u16 LE total length, u16 LE
    type code, then params. ``params`` already excludes the 4-byte header."""
    op_len = 4 + len(params)
    return struct.pack('<HH', op_len, op_code) + params


def _decode_one(op_code: int, params: bytes) -> dict:
    raw = _pack_op(op_code, params)
    out = decode_macro_bytecode(raw)
    assert len(out) == 1, f"expected 1 op, got {out!r}"
    return out[0]


# ----- Existing decoders (regression coverage) ------------------------------

def test_decoder_xsize_unsigned():
    # size_x = 500 wire (XML 0.5). bytecode = 500 * 65.536 = 32768.
    op = _decode_one(0x0047, bytes([0x00, 0x00, 0x0c, 0x00]) +
                     struct.pack('<I', 32768))
    assert op == {'id': 'DVEAndFlyKeyXSize',
                  'mixEffectBlockIndex': '0', 'keyIndex': '0',
                  'xSize': '0.5'}


# ----- New decoders added 2026-04-29 ----------------------------------------

def test_decoder_ysize_unsigned():
    # size_y = 1500 wire (XML 1.5). bytecode = 1500 * 65.536 = 98304.
    op = _decode_one(0x0048, bytes([0x00, 0x01, 0x0c, 0x00]) +
                     struct.pack('<I', 98304))
    assert op == {'id': 'DVEAndFlyKeyYSize',
                  'mixEffectBlockIndex': '0', 'keyIndex': '1',
                  'ySize': '1.5'}


def test_decoder_xpos_signed_negative():
    # pos_x = -16000 wire (XML -16.0 — observed live).
    # The live capture's bytecode was -1048577 (off-by-one from the exact
    # -16000 * 65.536 = -1048576 due to BMD's rounding); both decode to
    # ~-16. Use the exact value here to confirm the divisor is right.
    op = _decode_one(0x004A, bytes([0x00, 0x00, 0x0c, 0x00]) +
                     struct.pack('<i', -1048576))
    assert op['id'] == 'DVEAndFlyKeyXPosition'
    assert op['xPosition'] == '-16'
    assert op['mixEffectBlockIndex'] == '0'
    assert op['keyIndex'] == '0'


def test_decoder_xpos_off_by_one_keeps_precision():
    # The -1048577 value seen live is what BMD's CKDV→bytecode pipeline
    # actually produces for wire=-16000. _fmt_scalar emits up to 6
    # decimals; it should not silently round to -16.
    op = _decode_one(0x004A, bytes([0x00, 0x00, 0x0c, 0x00]) +
                     struct.pack('<i', -1048577))
    assert op['xPosition'] == '-16.000015'


def test_decoder_ypos_signed_positive():
    # pos_y = 9000 wire (XML 9.0). bytecode = 9000 * 65.536 = 589824.
    op = _decode_one(0x004B, bytes([0x00, 0x00, 0x0c, 0x00]) +
                     struct.pack('<i', 589824))
    assert op == {'id': 'DVEAndFlyKeyYPosition',
                  'mixEffectBlockIndex': '0', 'keyIndex': '0',
                  'yPosition': '9'}


def test_decoder_dve_mask_enable_true():
    # u8 mE, u8 keyer, u8 enable, u8 _.
    op = _decode_one(0x0035, bytes([0x00, 0x02, 0x01, 0x00]))
    assert op == {'id': 'DVEKeyMaskEnable',
                  'mixEffectBlockIndex': '0', 'keyIndex': '2',
                  'enable': 'True'}


def test_decoder_dve_mask_enable_false():
    op = _decode_one(0x0035, bytes([0x00, 0x00, 0x00, 0x00]))
    assert op['enable'] == 'False'


def test_decoder_dve_mask_top_signed():
    # mask_top = -4500 wire (XML -4.5). bytecode = -4500 * 65.536 = -294912.
    op = _decode_one(0x0036, bytes([0x00, 0x00, 0x0c, 0x00]) +
                     struct.pack('<i', -294912))
    assert op == {'id': 'DVEKeyMaskTop',
                  'mixEffectBlockIndex': '0', 'keyIndex': '0',
                  'top': '-4.5'}


def test_decoder_dve_mask_bottom_signed():
    # mask_bottom = 7200 wire (XML 7.2 — matches reference XML sample).
    # bytecode = 7200 * 65.536 = 471859.2 → 471859.
    op = _decode_one(0x0037, bytes([0x00, 0x00, 0x0c, 0x00]) +
                     struct.pack('<i', 471859))
    assert op['id'] == 'DVEKeyMaskBottom'
    # 471859/65536 = 7.19998... — _fmt_scalar emits up to 6 decimals.
    assert op['bottom'].startswith('7.199')


def test_decoder_dve_mask_left_signed():
    # mask_left = -16000 wire (XML -16.0).
    op = _decode_one(0x0038, bytes([0x00, 0x00, 0x0c, 0x00]) +
                     struct.pack('<i', -1048576))
    assert op['id'] == 'DVEKeyMaskLeft'
    assert op['left'] == '-16'


def test_decoder_dve_mask_right_signed():
    # mask_right = 16000 wire (XML 16.0).
    op = _decode_one(0x0039, bytes([0x00, 0x00, 0x0c, 0x00]) +
                     struct.pack('<i', 1048576))
    assert op == {'id': 'DVEKeyMaskRight',
                  'mixEffectBlockIndex': '0', 'keyIndex': '0',
                  'right': '16'}


def test_decoder_dsk_gain_unsigned():
    # gain = 500 per-mille wire = 50% = XML 0.5 (divisor 65536).
    # bytecode = 0.5 * 65536 = 32768 (exactly representable).
    op = _decode_one(0x009D, bytes([0x00, 0x88, 0x6c, 0x02]) +
                     struct.pack('<I', 32768))
    assert op['id'] == 'DownstreamKeyGain'
    assert op['keyIndex'] == '0'
    assert op['gain'] == '0.5'


def test_decoder_dsk_mask_enable_true():
    # u8 dsk, u8 enable, u16 _.
    op = _decode_one(0x009E, bytes([0x00, 0x01, 0x0c, 0x00]))
    assert op == {'id': 'DownstreamKeyMaskEnable',
                  'keyIndex': '0', 'enable': 'True'}


def test_decoder_dsk_pre_multiply_false():
    op = _decode_one(0x00A4, bytes([0x00, 0x00, 0x6c, 0x02]))
    assert op == {'id': 'DownstreamKeyPreMultiply',
                  'keyIndex': '0', 'preMultiply': 'False'}


# ----- Decode/encode symmetry guard -----------------------------------------

def test_known_ops_table_entries_are_3_tuples():
    """Every entry in ``_KNOWN_OPS`` is ``(xml_id, decoder, encoder)``,
    with both decoder and encoder callable. Asymmetric entries (decoder
    only or encoder only) would silently break save/restore: a download
    via the decoder produces XML that can't be re-uploaded, or vice
    versa. The same check runs at module load via
    ``atemwire.macrotransfer._check_known_ops_symmetry`` so a broken
    table fails import even outside pytest.
    """
    for op_code, entry in _KNOWN_OPS.items():
        assert isinstance(entry, tuple) and len(entry) == 3, (
            f"_KNOWN_OPS[0x{op_code:04x}] is not a 3-tuple: {entry!r}")
        xml_id, decoder, encoder = entry
        assert isinstance(xml_id, str) and xml_id, op_code
        assert callable(decoder), op_code
        assert callable(encoder), (
            f"_KNOWN_OPS[0x{op_code:04x}] {xml_id!r}: encoder is not "
            f"callable — every op needs an XML→wire encoder")


def test_known_ops_symmetry_module_load_guard():
    """The module-load guard fires on a broken _KNOWN_OPS shape.
    Replaces an entry with a 2-tuple and checks the guard raises."""
    from atemwire import macrotransfer

    # Fake-edit the table with a 2-tuple, then run the guard.
    saved = dict(macrotransfer._KNOWN_OPS)
    try:
        macrotransfer._KNOWN_OPS[0x0002] = ('ProgramInput', lambda *a: {})
        with pytest.raises(RuntimeError, match='3-tuple'):
            macrotransfer._check_known_ops_symmetry()
        # Now try with a None encoder.
        macrotransfer._KNOWN_OPS[0x0002] = ('ProgramInput',
                                             lambda *a: {}, None)
        with pytest.raises(RuntimeError, match='encoder is not callable'):
            macrotransfer._check_known_ops_symmetry()
    finally:
        macrotransfer._KNOWN_OPS.clear()
        macrotransfer._KNOWN_OPS.update(saved)


def test_unknown_opcode_round_trips_as_unknown():
    """An op-code not in _KNOWN_OPS still survives decode as
    Unknown_0x... with raw params. This is the fallback so unfamiliar
    macros aren't lost wholesale on import."""
    raw = _pack_op(0x9999, b'\x01\x02\x03\x04')
    out = decode_macro_bytecode(raw)
    assert out == [{'id': 'Unknown_0x9999', 'rawParams': '01020304'}]
