# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Bus and switching messages — program / preview / aux / cut / auto.

Wire packets:
    DCut — outgoing, trigger a cut transition
    DAut — outgoing, trigger an auto transition
    CPgI — outgoing, set program bus source
    CPvI — outgoing, set preview bus source
    CAuS — outgoing, set aux output source
    PrgI — incoming, current program bus state
    PrvI — incoming, current preview bus state
    AuxS — incoming, current aux output routing
"""

import struct

from atemwire.state import safe_int
from atemwire.messages._dsl import Recv, Send, boolean, u8, u16


# -----------------------------------------------------------------------------
# Outgoing — transition triggers (cut / auto)
# -----------------------------------------------------------------------------


class CutCommand(Send):
    """``DCut`` — initiate a cut transition on the given M/E.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'DCut'
    SIZE = 4

    index = u8(at=0)

    def __init__(self, index):
        super().__init__(index=index)


class AutoCommand(Send):
    """``DAut`` — initiate an auto transition on the given M/E.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'DAut'
    SIZE = 4

    index = u8(at=0)

    def __init__(self, index):
        super().__init__(index=index)


# -----------------------------------------------------------------------------
# Outgoing — bus routing (program / preview / aux)
# -----------------------------------------------------------------------------


class ProgramInputCommand(Send):
    """``CPgI`` — set the program bus source on the given M/E.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    ?      padding
    2      2    u16    Source index
    ====== ==== ====== ===========
    """
    CODE = 'CPgI'
    SIZE = 4

    index  = u8 (at=0)
    source = u16(at=2)

    def __init__(self, index, source):
        super().__init__(index=index, source=source)


class PreviewInputCommand(Send):
    """``CPvI`` — set the preview bus source on the given M/E.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    ?      padding
    2      2    u16    Source index
    ====== ==== ====== ===========
    """
    CODE = 'CPvI'
    SIZE = 4

    index  = u8 (at=0)
    source = u16(at=2)

    def __init__(self, index, source):
        super().__init__(index=index, source=source)


class AuxSourceCommand(Send):
    """``CAuS`` — route a source to a specific AUX output.

    The mask byte at offset 0 is always 1 in this command (selects the
    "set source" operation) — encoded as a literal init in ``__init__``.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (always 1)
    1      1    u8     AUX output index
    2      2    u16    Source index
    ====== ==== ====== ===========
    """
    CODE = 'CAuS'
    SIZE = 4

    mask   = u8 (at=0)
    index  = u8 (at=1)
    source = u16(at=2)

    def __init__(self, index, source):
        super().__init__(mask=1, index=index, source=source)


# -----------------------------------------------------------------------------
# Incoming — bus and aux state
# -----------------------------------------------------------------------------


class ProgramBusInputField(Recv):
    """``PrgI`` — current program bus state for one M/E.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    ?      padding
    2      2    u16    Source index
    ====== ==== ====== ===========
    """
    CODE = 'PrgI'
    PRETTY = 'program-bus-input'
    KEY_FORMAT = struct.Struct('>B')

    index  = u8 (at=0)
    source = u16(at=2)

    def __repr__(self):
        return f'<program-bus-input: me={self.index} source={self.source}>'


class PreviewBusInputField(Recv):
    """``PrvI`` — current preview bus state for one M/E.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    ?      padding
    2      2    u16    Source index
    4      1    u8     1 if preview is mixed into program during a transition
    5      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'PrvI'
    PRETTY = 'preview-bus-input'
    KEY_FORMAT = struct.Struct('>B')

    index      = u8     (at=0)
    source     = u16    (at=2)
    in_program = boolean(at=4)

    def __repr__(self):
        flag = ' in-program' if self.in_program else ''
        return f'<preview-bus-input: me={self.index} source={self.source}{flag}>'


class AuxOutputSourceField(Recv):
    """``AuxS`` — current AUX output routing.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     AUX output index
    1      1    ?      padding
    2      2    u16    Source index
    ====== ==== ====== ===========
    """
    CODE = 'AuxS'
    PRETTY = 'aux-output-source'
    KEY_FORMAT = struct.Struct('>B')

    index  = u8 (at=0)
    source = u16(at=2)

    def __repr__(self):
        return f'<aux-output-source: aux={self.index}, source={self.source}>'


# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def set_program(conn, source, me=0):
    conn.send(ProgramInputCommand(index=me, source=int(source)))


def set_preview(conn, source, me=0):
    conn.send(PreviewInputCommand(index=me, source=int(source)))


def cut(conn, me=0):
    conn.send(CutCommand(index=me))


def auto(conn, me=0):
    conn.send(AutoCommand(index=me))


def set_aux_output(conn, aux, source):
    conn.send(AuxSourceCommand(index=int(aux), source=int(source)))


# -----------------------------------------------------------------------------
# Readers
# -----------------------------------------------------------------------------


def program_source(mx, me=0):
    entry = (mx.get('program-bus-input') or {}).get(me)
    return safe_int(getattr(entry, 'source', None), 0)


def preview_source(mx, me=0):
    entry = (mx.get('preview-bus-input') or {}).get(me)
    return safe_int(getattr(entry, 'source', None), 0)


def aux_source(mx, idx):
    entry = (mx.get('aux-output-source') or {}).get(idx)
    return safe_int(getattr(entry, 'source', None), 0)
