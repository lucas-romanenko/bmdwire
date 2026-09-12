# SPDX-License-Identifier: LGPL-3.0-only
"""Memory-safety hardening in the mediaconvert C extension.

Pins the fixes for three classes of crash-on-input:

  * a live ``assert()`` (built without -DNDEBUG) SIGABRT'd the whole
    process when the RLE header word 0xFEFEFEFEFEFEFEFE appeared in the
    input — reachable from operator data, since native-format stills in
    a profile archive are rle_encode'd without conversion
  * the frame loops consumed fixed 8-byte groups without checking the
    input length, reading past the end on ragged input (and the
    ``len == 1`` special case in rle_encode read and wrote 8 bytes
    through 1-byte buffers)
  * every call leaked its input Py_buffer, pinning a frame-sized bytes
    object per call

Requires the compiled ``atemwire.mediaconvert`` extension (present in the
image; extract it into ./atemwire for host runs — see MEMORY).
"""
import sys

import pytest

pytest.importorskip("atemwire.mediaconvert")

from atemwire.imaging import atem_to_rgb, rgb_to_atem, rle_encode

RLE_HEADER = bytes([0xFE] * 8)


# --- the SIGABRT: header sentinel in input -------------------------------


def test_rle_header_word_raises_instead_of_aborting():
    data = b"\x00" * 8 + RLE_HEADER + b"\x01" * 8
    with pytest.raises(ValueError):
        rle_encode(data)


def test_rle_header_word_first_raises():
    with pytest.raises(ValueError):
        rle_encode(RLE_HEADER)


# --- ragged-length inputs ------------------------------------------------


def test_rle_rejects_ragged_length():
    with pytest.raises(ValueError):
        rle_encode(b"\x00" * 9)


def test_rle_rejects_one_byte_input():
    # The old len==1 branch copied a full 8-byte word through 1-byte
    # buffers on both sides.
    with pytest.raises(ValueError):
        rle_encode(b"\xfe")


def test_atem_to_rgb_rejects_ragged_length():
    with pytest.raises(ValueError):
        atem_to_rgb(b"\x00" * 12, 2, 1)


def test_rgb_to_atem_rejects_ragged_length():
    with pytest.raises(ValueError):
        rgb_to_atem(b"\x00" * 12, 2, 1, False)


def test_empty_inputs_return_empty():
    assert rle_encode(b"") == b""
    assert atem_to_rgb(b"", 0, 0) == b""
    assert rgb_to_atem(b"", 0, 0, False) == b""


# --- encoding behaviour is otherwise unchanged ---------------------------


def test_rle_characterization_literal_then_run():
    # [W, X, X, X, X] -> W, X literal, then a run block for the remaining
    # three repeats: HEADER, count=3 (big-endian u64), X.
    w1 = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    x = b"\x11" * 8
    out = rle_encode(w1 + x * 4)
    assert out == w1 + x + RLE_HEADER + (3).to_bytes(8, "big") + x


def test_rle_incompressible_passthrough():
    data = bytes(range(32))
    assert rle_encode(data) == data


# --- the Py_buffer leak --------------------------------------------------


def test_input_buffers_are_released():
    frame = bytes(range(64))
    baseline = sys.getrefcount(frame)
    for _ in range(20):
        atem_to_rgb(frame, 16, 1)
        rgb_to_atem(frame, 16, 1, False)
        rle_encode(frame)
    # The leaked Py_buffer held a reference per call; 60 calls made the
    # refcount climb by 60.
    assert sys.getrefcount(frame) == baseline
