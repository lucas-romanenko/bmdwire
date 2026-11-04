# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
import struct
import atemwire.mediaconvert as mc


# atem_to_rgb converts in pieces of this many input bytes. The native
# conversion holds the GIL for the whole call: 33 ms for a 1080p still and
# 130 ms for a 4K one, measured 2026-11-04, longer than the ~80 ms after
# which a switcher resends what it hasn't had ACKed. Every connection's UDP
# thread in the process stood still for it, mid-download, and the
# resends it came back to buried the next still. Each 8-byte group (two
# pixels) converts on its own, so the pieces join to the same bytes.
CONVERT_PIECE = 1024 * 1024


def atem_to_rgb(data, width, height):
    """Wrapper for the native function, run in CONVERT_PIECE pieces so
    other threads get the GIL between them."""
    if len(data) <= CONVERT_PIECE or len(data) % 8:
        return mc.atem_to_rgb(data, width, height)
    view = memoryview(data)
    pieces = [mc.atem_to_rgb(view[i:i + CONVERT_PIECE], width, height)
              for i in range(0, len(view), CONVERT_PIECE)]
    return b''.join(pieces)


def rgb_to_atem(data, width, height, premultiply=False):
    """Wrapper for the native function"""
    return mc.rgb_to_atem(data, width, height, premultiply)


def rle_encode(data):
    return mc.rle_encode(data)


_RLE_HEADER = b'\xfe' * 8


def rle_decode(data):
    """
    ATEM frames are compressed with a custom RLE encoding. Data in the frame is grouped in 8 byte chunks since
    that is exactly 2 pixels in the 10-bit YCbCr 4:2:2 data. Most of the data is sent without compression but
    if a 8 byte chunk is fully 0xfe then the following chunk is RLE compressed.

    An RLE compressed part is an 64 bit integer setting the repeat count following the 8-byte block of data
    to be repeated. This seems mainly useful to compress solid colors.

    The literal runs between headers are copied whole, found with
    ``bytes.find``. Walking the frame 8 bytes at a time in Python ran on
    the connection's own worker thread, which handled none of that
    switcher's packets meanwhile: 133 ms for a 12 MB 4K test frame, 18 ms
    now (2026-11-04).

    :param data:
    :return:
    """
    result = bytearray()
    offset = 0
    in_size = len(data)
    find = data.find
    while offset < in_size:
        header = find(_RLE_HEADER, offset)
        # Headers sit on the 8-byte grid (literals are 8 bytes, a header
        # group 24); 0xfe bytes spanning two words are pixel data.
        while header != -1 and (header - offset) % 8:
            header = find(_RLE_HEADER, header + 8 - (header - offset) % 8)
        if header == -1:
            result += data[offset:]
            break
        result += data[offset:header]
        count, = struct.unpack_from('>Q', data, header + 8)
        result += data[header + 16:header + 24] * count
        offset = header + 24
    return result
