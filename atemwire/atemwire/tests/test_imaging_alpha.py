# SPDX-License-Identifier: LGPL-3.0-only
"""Alpha-channel decode clamp in the mediaconvert C extension.

Switcher-captured stills (CaptureStill) mark opaque pixels with alpha
~1023 on the wire, while ``rgb_to_atem`` writes 937 for an 8-bit 255.
The unclamped decode ``(a - 16) / 3.6`` overflowed the byte cast for
values above ~934 — 1023 decoded to 23 (279 & 0xFF), rendering every
hardware capture ~91% transparent in profile-save PNGs (observed live
2026-07-11). These tests pin the clamp.

Requires the compiled ``atemwire.mediaconvert`` extension (present in the
image; extract it into ./atemwire for host runs — see MEMORY).
"""

import pytest

pytest.importorskip("atemwire.mediaconvert")

from atemwire.imaging import atem_to_rgb, rgb_to_atem


def _pack_block(a1, cb, y1, a2, cr, y2):
    """Pack one 8-byte wire block (2 pixels), mirroring the decoder's
    bit layout in mediaconvertmodule.c."""
    return bytes([
        (a1 >> 4) & 0xFF,
        ((a1 & 0x0F) << 4) | ((cb >> 6) & 0x0F),
        ((cb & 0x3F) << 2) | ((y1 >> 8) & 0x03),
        y1 & 0xFF,
        (a2 >> 4) & 0xFF,
        ((a2 & 0x0F) << 4) | ((cr >> 6) & 0x0F),
        ((cr & 0x3F) << 2) | ((y2 >> 8) & 0x03),
        y2 & 0xFF,
    ])


def _decode_alphas(alpha):
    """Decode one block with the given wire alpha on both pixels;
    return the two 8-bit alpha values."""
    block = _pack_block(alpha, 512, 502, alpha, 512, 502)  # neutral gray
    rgba = atem_to_rgb(block, 2, 1)
    return rgba[3], rgba[7]


@pytest.mark.parametrize("wire_alpha", [1023, 940, 4095])
def test_switcher_opaque_alpha_clamps_to_255(wire_alpha):
    # Pre-clamp these wrapped: 1023 -> 23, 940 -> 0, 4095 -> 109.
    assert _decode_alphas(wire_alpha) == (255, 255)


def test_encoder_max_alpha_decodes_to_255():
    # 937 is what rgb_to_atem writes for 8-bit 255; already in range,
    # must stay exact.
    assert _decode_alphas(937) == (255, 255)


def test_low_alpha_clamps_to_zero_not_bright():
    # (0 - 16) / 3.6 is negative; the unsigned cast would wrap bright.
    assert _decode_alphas(0) == (0, 0)


def test_opaque_rgba_roundtrip_keeps_full_alpha():
    rgba = bytes([200, 100, 50, 255] * 2)
    wire = rgb_to_atem(rgba, 2, 1, False)
    decoded = atem_to_rgb(wire, 2, 1)
    assert decoded[3] == 255
    assert decoded[7] == 255
