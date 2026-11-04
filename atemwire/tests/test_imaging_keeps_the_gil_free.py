# SPDX-License-Identifier: LGPL-3.0-only
"""A still's decode doesn't hold up the process's other threads
(2026-11-04).

The native YCbCr->RGB conversion held the GIL for the whole frame: 33 ms
for 1080p, 130 ms for 4K, longer than the ~80 ms after which a switcher
resends what it hasn't had ACKed, so every connection's UDP thread stood
still. It now runs in pieces, joined to the same bytes. rle_decode walked
the frame 8 bytes at a time in Python on the connection's worker thread;
it copies the literal runs whole now, with the same result.
"""
import os
import random
import struct

from atemwire import imaging
import atemwire.mediaconvert as mc


def _reference_rle_decode(data):
    """rle_decode as it was, 8 bytes at a time."""
    result = bytearray()
    offset = 0
    while offset < len(data):
        block = data[offset:offset + 8]
        if block == b'\xfe' * 8:
            offset += 8
            count, = struct.unpack_from('>Q', data, offset)
            offset += 8
            result += data[offset:offset + 8] * count
            offset += 8
        else:
            result += block
            offset += 8
    return result


def _frame(rng):
    parts = []
    for _ in range(rng.randint(0, 40)):
        r = rng.random()
        if r < 0.3:
            parts.append(b'\xfe' * 8 + struct.pack('>Q', rng.randint(0, 50))
                         + bytes(rng.randrange(256) for _ in range(8)))
        elif r < 0.5:                            # 0xfe bytes off the 8-byte grid
            parts.append(bytes([rng.choice([0xfe, 0xfe, 1])]) * rng.choice([7, 8, 9, 13, 16, 24]))
        else:
            parts.append(bytes(rng.randrange(256) for _ in range(8 * rng.randint(0, 5))))
    data = b''.join(parts)
    return data[:len(data) - len(data) % 8]


def _outcome(decode, data):
    try:
        return decode(data)
    except Exception as e:                       # a header with an absurd count
        return type(e)


def test_rle_decode_gives_what_it_always_gave():
    rng = random.Random(20261104)
    for _ in range(400):
        data = _frame(rng)
        assert _outcome(imaging.rle_decode, data) == _outcome(_reference_rle_decode, data), data.hex()


def test_rle_decode_returns_a_bytearray():
    assert isinstance(imaging.rle_decode(b'\x01' * 8), bytearray)


def test_atem_to_rgb_in_pieces_is_the_same_conversion():
    width, height = 1920, 1080
    raw = os.urandom(width * height * 4)
    assert len(raw) > imaging.CONVERT_PIECE
    expected = mc.atem_to_rgb(raw, width, height)
    assert imaging.atem_to_rgb(raw, width, height) == expected
    assert imaging.atem_to_rgb(bytearray(raw), width, height) == expected


def test_a_small_or_ragged_frame_goes_to_the_native_call_whole():
    assert imaging.atem_to_rgb(b'\x00' * 16, 2, 2) == mc.atem_to_rgb(b'\x00' * 16, 2, 2)
