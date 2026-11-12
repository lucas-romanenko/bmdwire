# SPDX-License-Identifier: MIT
"""ultimattewire.media — loading a still into an Ultimatte's media pool.

Not in the text protocol: the document says images go in through Smart
Remote 4 or Software Control only. Captures of Software Control on an
Ultimatte 12 4K (software 2.3, 2026-11-11 and -12) show it is the ordinary
9996 write frame (see :mod:`ultimattewire._protocol`) with opcode 0x0101,
between 0x0100 (preset slot) and 0x0103 (resource)::

    [0x0101][name_len:4BE][file name][data_len:4BE][file bytes][0x00]

on a fresh connection; the unit answered one 0x00 byte and closed. The
file goes as it is, with no conversion, and the frame doesn't say what it
is: a PNG and a JPG (``ffd8ffe0…JFIF``) went the same way, and the unit
put both in the pool and on a frame buffer. Other formats were not tried,
so only those two are sent. The 9998 session then gets an IMAGE LIST push
with the name. The pool counts space in 4 KiB blocks (an 8,601-byte PNG
took 12,288 of ``Available``).

The unit takes any size into the pool, but refuses to put an image on a
frame buffer unless it matches the running video format ("Mismatched
formats: Image format is 3840x2160 and video format is 1920x1080"), so
:func:`upload_image` can check that first: from the PNG's IHDR, or the
JPEG's frame header (SOF).

The file is sent in chunks as it is read (from bytes, a path, a binary
file or an iterable of chunks), with an optional ``progress(sent, total)``
callback.

Removing an image is on 9998 (``Ultimatte.delete_image``).
"""

from __future__ import annotations

import io
import itertools
import os
import re
import struct
from typing import Callable, Iterable, Iterator, Optional, Tuple, Union

from ultimattewire._errors import UltimatteError
from ultimattewire._protocol import (CONNECT_TIMEOUT, READ_TIMEOUT,
                                     SETTINGS_PORT, upload_stream)

OP_WRITE_IMAGE = 0x0101
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
PNG, JPEG = "png", "jpeg"
CHUNK_SIZE = 256 * 1024
# How far into a file its frame size is looked for. A JPEG's SOF comes
# after its APPn segments (EXIF, a thumbnail, ICC), each at most 64 KiB.
HEADER_LIMIT = 1024 * 1024

# JPEG start-of-frame markers, which carry the size: C0-CF less DHT (C4),
# JPG (C8) and DAC (CC).
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

Progress = Callable[[int, int], None]
ImageData = Union[bytes, bytearray, memoryview, str, "os.PathLike",
                  io.BufferedIOBase, Iterable[bytes]]

# Frame size by the line count in a video format name (1080p25, 2160p50,
# 1080i59.94, 720p50, 4320p25).
_FORMAT_SIZES = {720: (1280, 720), 1080: (1920, 1080), 2160: (3840, 2160),
                 4320: (7680, 4320)}


class UltimatteImageError(UltimatteError):
    """The image can't go to this unit (not a PNG or JPG, wrong size, bad
    name, a stream of unknown length), or the unit refused the upload."""


def png_size(data: bytes) -> Tuple[int, int]:
    """(width, height) from a PNG's IHDR. Raises UltimatteImageError if
    ``data`` isn't a PNG."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise UltimatteImageError("not a PNG file")
    return struct.unpack(">II", data[16:24])


def jpeg_size(data: bytes) -> Tuple[int, int]:
    """(width, height) from a JPEG's frame header (SOF). Raises
    UltimatteImageError if ``data`` isn't a JPEG or ends before its SOF."""
    size = _jpeg_scan(bytes(data[:HEADER_LIMIT]))
    if size is None:
        raise UltimatteImageError("JPEG ends before its frame header (SOF)")
    return size


def image_size(data: bytes) -> Tuple[str, Tuple[int, int]]:
    """(``"png"`` or ``"jpeg"``, (width, height)) of an image's first bytes.
    Raises UltimatteImageError for anything else."""
    found = _probe(bytes(data[:HEADER_LIMIT]))
    if found is None:
        raise UltimatteImageError("the image ends before its size")
    return found


def _probe(head: bytes) -> Optional[Tuple[str, Tuple[int, int]]]:
    """What ``head`` is and its size; None if more bytes are needed."""
    if head.startswith(PNG_SIGNATURE[:len(head)]) and len(head) < 24:
        return None
    if head.startswith(PNG_SIGNATURE):
        return PNG, png_size(head)
    if head.startswith(JPEG_SIGNATURE[:len(head)]) and len(head) < 3:
        return None
    if head.startswith(JPEG_SIGNATURE):
        size = _jpeg_scan(head)
        return None if size is None else (JPEG, size)
    raise UltimatteImageError(
        "not a PNG or JPG file (the only formats seen going into the media pool)")


def _jpeg_scan(data: bytes) -> Optional[Tuple[int, int]]:
    """Walk a JPEG's marker segments to its SOF: (width, height), or None
    if ``data`` ends first. Raises UltimatteImageError on a broken file."""
    if not data.startswith(JPEG_SIGNATURE):
        raise UltimatteImageError("not a JPEG file")
    i, n = 2, len(data)
    while True:
        if i >= n:
            return None
        if data[i] != 0xFF:
            raise UltimatteImageError(f"broken JPEG: no marker at byte {i}")
        while i < n and data[i] == 0xFF:            # fill bytes
            i += 1
        if i >= n:
            return None
        marker = data[i]
        i += 1
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # no length
            continue
        if marker in (0xD9, 0xDA):
            raise UltimatteImageError("broken JPEG: image data before a frame header")
        if i + 2 > n:
            return None
        length, = struct.unpack(">H", data[i:i + 2])
        if length < 2:
            raise UltimatteImageError(f"broken JPEG: segment length {length}")
        if marker in _JPEG_SOF:
            if i + 7 > n:
                return None
            height, width = struct.unpack(">HH", data[i + 3:i + 7])
            return width, height
        i += length


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


def upload_image(host: str, name: str, data: ImageData, *,
                 size: Optional[int] = None,
                 progress: Optional[Progress] = None,
                 chunk_size: int = CHUNK_SIZE,
                 video_format: Optional[str] = None,
                 connect_timeout: float = CONNECT_TIMEOUT,
                 read_timeout: float = READ_TIMEOUT,
                 port: int = SETTINGS_PORT) -> Tuple[int, int]:
    """Send a PNG or JPG to the unit's media pool as ``name``; return its
    (width, height).

    ``data`` is the file as bytes, a path, a binary file open for reading
    (sent from where it stands), or an iterable of bytes chunks. A file
    that can't tell its length and an iterable need ``size`` (the bytes
    to send). Nothing is held in memory but the first chunks, read for the
    image's size; ``progress(sent, total)`` is called after every chunk
    sent, and an exception from it abandons the upload.

    With ``video_format`` (the DEVICE block's running format) an image the
    unit couldn't put on a frame buffer is refused before anything is
    sent. ``read_timeout`` is per socket operation, so a big file on a
    slow link doesn't time out while it is moving."""
    name = check_name(name)
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    total, chunks, close = _source(data, size, chunk_size)
    try:
        head, chunks = _peek(chunks)
        kind, dims = head
        want = format_size(video_format) if video_format else None
        if want and dims != want:
            raise UltimatteImageError(
                f"{name} is {dims[0]}x{dims[1]}; the unit runs {video_format} "
                f"({want[0]}x{want[1]}) and won't assign a different size")
        try:
            upload_stream(host, OP_WRITE_IMAGE, name, total, chunks,
                          progress=progress, connect_timeout=connect_timeout,
                          read_timeout=read_timeout, port=port)
        except (IOError, OSError) as e:
            raise UltimatteImageError(f"{host}: upload of {name} failed: {e}") from e
    finally:
        close()
    return dims


def _source(data, size: Optional[int], chunk_size: int):
    """(total bytes, chunk iterator, close) for anything upload_image takes."""
    if isinstance(data, (bytes, bytearray, memoryview)):
        view = memoryview(data).cast("B")
        total = len(view) if size is None else size
        return total, (bytes(view[i:i + chunk_size])
                       for i in range(0, len(view), chunk_size)), lambda: None
    if isinstance(data, (str, os.PathLike)):
        try:
            f = open(data, "rb")
        except OSError as e:
            raise UltimatteImageError(f"can't read {os.fspath(data)}: {e}") from e
        total = os.fstat(f.fileno()).st_size if size is None else size
        return total, _read_chunks(f, chunk_size), f.close
    if hasattr(data, "read"):
        total = size if size is not None else _remaining(data)
        return total, _read_chunks(data, chunk_size), lambda: None
    if size is None:
        raise UltimatteImageError(
            "an iterable of chunks needs size= (the frame carries the length first)")
    return size, iter(data), lambda: None


def _read_chunks(f, chunk_size: int) -> Iterator[bytes]:
    return iter(lambda: f.read(chunk_size), b"")


def _remaining(f) -> int:
    """Bytes left in a binary file from where it stands."""
    try:
        return os.fstat(f.fileno()).st_size - f.tell()
    except (AttributeError, OSError, io.UnsupportedOperation):
        pass
    try:
        if f.seekable():
            here = f.tell()
            end = f.seek(0, io.SEEK_END)
            f.seek(here)
            return end - here
    except (AttributeError, OSError, io.UnsupportedOperation):
        pass
    raise UltimatteImageError(
        "this file can't tell its length: give size= (the frame carries it first)")


def _peek(chunks: Iterator[bytes]):
    """Read chunks until the image's kind and size are known; returns
    them and an iterator that still yields every byte."""
    chunks = iter(chunks)
    taken = []
    head = b""
    while True:
        found = _probe(head[:HEADER_LIMIT]) if head else None
        if found is not None:
            return found, itertools.chain(taken, chunks)
        if len(head) >= HEADER_LIMIT:
            raise UltimatteImageError(
                f"no image size in the first {HEADER_LIMIT} bytes")
        chunk = next(chunks, None)
        if chunk is None:
            raise UltimatteImageError(
                "the image ends before its size" if head else "the image is empty")
        chunk = bytes(chunk)
        taken.append(chunk)
        head += chunk
