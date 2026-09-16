# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
File transfer messages — bulk download/upload of media-pool / macro store
content. The wire protocol is FTSU (download request) → FTDa stream →
FTUA acks → FTDC complete; or FTSD (upload request) → FTDa stream → FTFD
metadata → FTDC complete.

Wire packets (outgoing):
    FTSU — request a download
    FTSD — request an upload
    FTDa — chunk of data (variable-length payload)
    FTFD — uploaded file metadata (name, description, MD5)
    FTUA — acknowledgement for an FTDa packet

Wire packets (incoming):
    FTDa — incoming chunk of data
    FTDE — transfer error
    FTDC — transfer complete
    FTCD — continue-data (chunk size + count for the next phase)
"""

import struct

from atemwire.messages._dsl import Recv, Send, u8, u16, u32


# -----------------------------------------------------------------------------
# Outgoing — transfer requests + payload + ack
# -----------------------------------------------------------------------------


class TransferDownloadRequestCommand(Send):
    """``FTSU`` — request a download from the switcher.

    The trailing 4-byte block ``[u1 u2 u3 u4]`` is a magic suffix the ATEM
    expects. ``u1`` is 0x03 for macro-store transfers (store=0xffff),
    0x00 for stills. ``u2 u3 u4`` are constant ``0xd0 0x9b 0x8c``.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Transfer id
    2      2    u16    Store
    4      4    u32    Slot
    8      4    u8[]   Magic suffix
    ====== ==== ====== ===========
    """
    CODE = 'FTSU'
    SIZE = 12

    transfer = u16(at=0)
    store    = u16(at=2)
    slot     = u32(at=4)
    _u1      = u8 (at=8)
    _u2      = u8 (at=9)
    _u3      = u8 (at=10)
    _u4      = u8 (at=11)

    def __init__(self, transfer, store, slot):
        u1 = 0x03 if store == 0xffff else 0x00
        super().__init__(transfer=transfer, store=store, slot=slot,
                         _u1=u1, _u2=0xd0, _u3=0x9b, _u4=0x8c)


class TransferUploadRequestCommand(Send):
    """``FTSD`` — request an upload to the switcher.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Transfer id
    2      2    u16    Store
    4      2    ?      padding
    6      2    u16    Slot
    8      4    u32    Data length
    12     2    u16    Mode bitfield
    14     2    ?      padding
    ====== ==== ====== ===========

    Mode bits: 0 unknown, 1 Write-RLE, 2 Clear, 256 Write-uncompressed,
               512 Pre-erase.
    """
    CODE = 'FTSD'
    SIZE = 16

    MODE_WRITE_RLE = 1
    MODE_WRITE     = 256
    MODE_ERASE     = 512

    transfer = u16(at=0)
    store    = u16(at=2)
    slot     = u16(at=6)
    length   = u32(at=8)
    mode     = u16(at=12)

    def __init__(self, transfer, store, slot, length, mode):
        super().__init__(transfer=transfer, store=store, slot=slot,
                         length=length, mode=mode)


class TransferDataCommand(Send):
    """``FTDa`` — outgoing chunk of upload data.

    Variable-length payload — the chunk data is appended after a 4-byte
    header (transfer id + size). Doesn't fit the static-layout DSL so
    this overrides ``get_command`` directly.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Transfer id
    2      2    u16    Size
    4      N    u8[]   Chunk data
    ====== ==== ====== ===========
    """
    CODE = 'FTDa'

    def __init__(self, transfer, data):
        self.transfer = transfer
        self.data = data

    def get_command(self) -> bytes:
        payload = struct.pack('>HH', self.transfer, len(self.data)) + self.data
        header = struct.pack('>H 2x 4s', len(payload) + 8, self.CODE.encode())
        return header + payload


class TransferFileDataCommand(Send):
    """``FTFD`` — uploaded file metadata (name, description, MD5).

    Sent after all FTDa chunks. Strings are fixed-length NUL-padded;
    hash is exactly 16 bytes.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Transfer id
    2      64   str    Name
    66     128  str    Description
    194    16   u8[]   MD5 hash
    210    2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'FTFD'

    def __init__(self, transfer, hash, name=None, description=None):
        self.transfer = transfer
        self.hash = hash
        self.name = name
        self.description = description

    def get_command(self) -> bytes:
        name = self.name.encode() if self.name is not None else b''
        description = self.description.encode() if self.description is not None else b''
        payload = struct.pack(
            '>H 64s 128s 16s 2x',
            self.transfer, name, description, self.hash,
        )
        header = struct.pack('>H 2x 4s', len(payload) + 8, self.CODE.encode())
        return header + payload


class TransferAckCommand(Send):
    """``FTUA`` — acknowledge an incoming FTDa chunk.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Transfer id
    2      2    u16    Slot
    ====== ==== ====== ===========
    """
    CODE = 'FTUA'
    SIZE = 4

    transfer = u16(at=0)
    slot     = u16(at=2)

    def __init__(self, transfer, slot):
        super().__init__(transfer=transfer, slot=slot)


# -----------------------------------------------------------------------------
# Incoming — data / error / complete / continue
# -----------------------------------------------------------------------------


class FileTransferDataField(Recv):
    """``FTDa`` — incoming chunk of download data.

    Variable-length payload; the chunk data is everything after the
    4-byte header.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Transfer id
    2      2    u16    Data length
    4      N    u8[]   Chunk data
    ====== ==== ====== ===========
    """
    CODE = 'FTDa'
    PRETTY = 'file-transfer-data'

    def __init__(self, raw: bytes):
        self.raw = raw
        self.transfer, self.size = struct.unpack('>HH', raw[0:4])
        self.data = raw[4:(4 + self.size)]

    def __repr__(self):
        return f'<file-transfer-data transfer={self.transfer} size={self.size}>'


class FileTransferErrorField(Recv):
    """``FTDE`` — transfer aborted with error.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Transfer id
    2      1    u8     Error code (1=try-again, 2=not-found, 5=no-lock,
                       6=lock-lost: the store was granted to another
                       session before this transfer was serviced)
    3      1    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'FTDE'
    PRETTY = 'file-transfer-error'

    transfer = u16(at=0)
    status   = u8 (at=2)

    def __repr__(self):
        errors = {1: 'try-again', 2: 'not-found', 5: 'no-lock', 6: 'lock-lost'}
        s = errors.get(self.status, f'unknown ({self.status})')
        return f'<file-transfer-error transfer={self.transfer} status={s}>'


class FileTransferDataCompleteField(Recv):
    """``FTDC`` — file transfer complete notification.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Transfer id
    2      1    u8     u1 (always 1)
    3      1    u8     u2 (always 2 or 6)
    ====== ==== ====== ===========
    """
    CODE = 'FTDC'
    PRETTY = 'file-transfer-data-complete'

    transfer = u16(at=0)
    u1       = u8 (at=2)
    u2       = u8 (at=3)

    def __repr__(self):
        return (f'<file-transfer-complete transfer={self.transfer} '
                f'u1={self.u1} u2={self.u2}>')


class FileTransferContinueDataField(Recv):
    """``FTCD`` — chunk-size + count for the next FTDa phase.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Transfer id
    2      4    ?      padding
    6      2    u16    Chunk size
    8      2    u16    Chunk count
    10     2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'FTCD'
    PRETTY = 'file-transfer-continue-data'

    transfer = u16(at=0)
    size     = u16(at=6)
    count    = u16(at=8)

    def __repr__(self):
        return (f'<file-transfer-continue transfer={self.transfer} '
                f'size={self.size} count={self.count}>')
