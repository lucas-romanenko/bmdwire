# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Tally messages — broadcast-only state from the ATEM listing program /
preview status for each input.

Two parallel views of the same data:
- ``TlIn`` (TallyIndexField) — list indexed by physical input index
- ``TlSr`` (TallySourceField) — dict keyed by source index

Both are variable-length payloads (count + N tally entries) so the
parsing is hand-rolled.

Wire packets:
    TlIn — incoming, tally state by input index
    TlSr — incoming, tally state by source index
"""

import struct

from atemwire.messages._dsl import Recv


class TallyIndexField(Recv):
    """``TlIn`` — tally state for every input, indexed by input number.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Number of tally lights
    n      1    u8     Bitfield (bit0 = PROGRAM, bit1 = PREVIEW), repeated
    ====== ==== ====== ===========

    :ivar num: number of tally lights
    :ivar tally: list of ``(program, preview)`` boolean tuples
    """
    CODE = 'TlIn'
    PRETTY = 'tally-index'

    def __init__(self, raw: bytes):
        self.raw = raw
        offset = 0
        self.num, = struct.unpack_from('>H', raw, offset)
        offset += 2
        self.tally = []
        for _ in range(self.num):
            tally, = struct.unpack_from('>B', raw, offset)
            self.tally.append((tally & 1 != 0, tally & 2 != 0))
            offset += 1

    def __repr__(self):
        return f'<tally-index: num={self.num}, val={self.tally}>'


class TallySourceField(Recv):
    """``TlSr`` — tally state for every input, indexed by source ID.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Number of tally lights
    n      2    u16    Source index
    n+2    1    u8     Bitfield (bit0 = PROGRAM, bit1 = PREVIEW)
    ====== ==== ====== ===========

    :ivar num: number of tally lights
    :ivar tally: dict mapping source-index → ``(program, preview)``
    """
    CODE = 'TlSr'
    PRETTY = 'tally-source'

    def __init__(self, raw: bytes):
        self.raw = raw
        offset = 0
        self.num, = struct.unpack_from('>H', raw, offset)
        offset += 2
        self.tally = {}
        for _ in range(self.num):
            source, tally = struct.unpack_from('>HB', raw, offset)
            self.tally[source] = (tally & 1 != 0, tally & 2 != 0)
            offset += 3

    def __repr__(self):
        return f'<tally-source: num={self.num}, val={self.tally}>'
