# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
import hashlib
import struct

from atemwire.imaging import rle_encode


class TransferTask:
    def __init__(self, store, slot, upload=False, clear=False):
        self.tid = None
        self.state = None
        self.upload = upload
        # A "clear" task has no FTSU/FTDC data arc: the machinery takes the
        # store lock, sends CSTL while holding it, and releases per-frame.
        # Needed because the switcher silently ignores a bare CSTL from a
        # session that does not hold the still-store lock.
        self.clear = clear

        self.store = store
        self.slot = slot

        self.data = None
        self.data_length = None
        self.hash = None

        self.send_length = None
        self.send_done = 0

        self.name = None
        self.description = None

    def calculate_hash(self):
        hasher = hashlib.md5(self.data)
        self.hash = hasher.digest()
        self.data_length = len(self.data)

    def compress(self):
        compressed = rle_encode(self.data)
        self.data = compressed
        self.send_length = len(self.data)

    def __repr__(self):
        direction = 'clear' if self.clear else (
            'upload' if self.upload else 'download')
        return f'<TransferTask {direction} store={self.store} slot={self.slot}>'

    def to_tcp(self):
        name = self.name.encode() if self.name else b''
        description = self.description.encode() if self.description else b''
        header = struct.pack('>HH Hx? 64s 128s 16s II', self.tid or 0, self.store, self.slot, self.upload,
                             name, description, self.hash, self.send_length, self.data_length)

        # Large packets, let TCP fragmentation deal with it
        chunksize = 16000
        buffer = self.data
        packets = []
        while True:
            chunk = buffer[0:chunksize]
            buffer = buffer[chunksize:]
            packet = header + chunk
            packets.append((b'*XFR', packet))
            if len(buffer) == 0:
                break
        return packets


class TransferQueueFlushed:
    def __init__(self):
        pass
