# SPDX-License-Identifier: MIT
"""ultimattewire.media — loading a still into an Ultimatte's media pool.

Not in the text protocol: the document says images go in through Smart
Remote 4 or Software Control only. A capture of Software Control on an
Ultimatte 12 4K (software 2.3, 2026-11-11) shows it is the ordinary 9996
write frame (see :mod:`ultimattewire._protocol`) with opcode 0x0101,
between 0x0100 (preset slot) and 0x0103 (resource)::

    [0x0101][name_len:4BE][file name][data_len:4BE][PNG file bytes][0x00]

on a fresh connection; the unit answered one 0x00 byte and closed. The
PNG goes as the file is, with no conversion. The 9998 session then gets
an IMAGE LIST push with the name.

The unit takes any size into the pool, but refuses to put an image on a
frame buffer unless it matches the running video format ("Mismatched
formats: Image format is 3840x2160 and video format is 1920x1080"), so
:func:`upload_image` can check that first.

Removing an image is on 9998 (``Ultimatte.delete_image``).
"""

from __future__ import annotations

import re
import struct
from typing import Optional, Tuple

from ultimattewire._errors import UltimatteError
from ultimattewire._protocol import (CONNECT_TIMEOUT, READ_TIMEOUT,
                                     SETTINGS_PORT, upload_one)

OP_WRITE_IMAGE = 0x0101
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Frame size by the line count in a video format name (1080p25, 2160p50,
# 1080i59.94, 720p50, 4320p25).
_FORMAT_SIZES = {720: (1280, 720), 1080: (1920, 1080), 2160: (3840, 2160),
                 4320: (7680, 4320)}


class UltimatteImageError(UltimatteError):
    """The image can't go to this unit (not a PNG, wrong size, bad name),
    or the unit refused the upload."""


def png_size(data: bytes) -> Tuple[int, int]:
    """(width, height) from a PNG's IHDR. Raises UltimatteImageError if
    ``data`` isn't a PNG."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise UltimatteImageError("not a PNG file (the media pool takes PNG stills)")
    return struct.unpack(">II", data[16:24])


def format_size(video_format: str) -> Optional[Tuple[int, int]]:
    """The frame size of a video format name (``1080p25`` -> 1920x1080),
    or None for a name it can't read (``auto detect``, empty)."""
    m = re.match(r"\s*(\d+)[pi]", video_format or "", re.I)
    return _FORMAT_SIZES.get(int(m.group(1))) if m else None


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or "\n" in name or "\r" in name:
        raise UltimatteImageError(f"bad image name {name!r}")
    return name


def upload_image(host: str, name: str, data: bytes, *,
                 video_format: Optional[str] = None,
                 connect_timeout: float = CONNECT_TIMEOUT,
                 read_timeout: float = READ_TIMEOUT,
                 port: int = SETTINGS_PORT) -> Tuple[int, int]:
    """Send the PNG ``data`` to the unit's media pool as ``name``; return
    its (width, height). With ``video_format`` (the DEVICE block's running
    format) an image the unit couldn't put on a frame buffer is refused
    before anything is sent. ``read_timeout`` is per socket operation, so
    a big file on a slow link doesn't time out while it is moving."""
    name = check_name(name)
    size = png_size(data)
    want = format_size(video_format) if video_format else None
    if want and size != want:
        raise UltimatteImageError(
            f"{name} is {size[0]}x{size[1]}; the unit runs {video_format} "
            f"({want[0]}x{want[1]}) and won't assign a different size")
    try:
        upload_one(host, OP_WRITE_IMAGE, name, data,
                   connect_timeout=connect_timeout, read_timeout=read_timeout,
                   port=port)
    except (IOError, OSError) as e:
        raise UltimatteImageError(f"{host}: upload of {name} failed: {e}") from e
    return size
