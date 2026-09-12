# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Multiviewer messages — quadrant layout, per-window source routing, VU /
safe-area overlay flags.

Wire packets (incoming only):
    MvPr — multiview layout (quadrant subdivisions + program/preview flip)
    MvIn — per-window source assignment + overlay-availability flags
    VuMC — per-window VU-meter overlay enable
    SaMw — per-window safe-area overlay enable
"""

import struct

from atemwire.messages._dsl import Recv, boolean, u8, u16
from atemwire.messages._dsl import Send, boolean, i16, string, u8, u16, u32  # noqa: F401  (restored upstream commands)


class MultiviewerPropertiesField(Recv):
    """``MvPr`` — multiview layout for one multiviewer output.

    The layout byte is a 4-bit bitfield: bit N = quadrant N is split into
    4 small windows. (0=top-left, 1=top-right, 2=bottom-left,
    3=bottom-right.) Default layout: bottom quadrants split, top windows
    full-size for program / preview.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Multiviewer index
    1      1    u8     Layout bitfield
    2      1    bool   Flip program/preview
    3      1    u8     u1 (unknown)
    ====== ==== ====== ===========
    """
    CODE = 'MvPr'
    PRETTY = 'multiviewer-properties'
    KEY_FORMAT = struct.Struct('>B')

    index  = u8     (at=0)
    layout = u8     (at=1)
    flip   = boolean(at=2)
    u1     = u8     (at=3)

    def __init__(self, raw: bytes):
        super().__init__(raw)
        self.top_left_small     = self.layout & 0x01 > 0
        self.top_right_small    = self.layout & 0x02 > 0
        self.bottom_left_small  = self.layout & 0x04 > 0
        self.bottom_right_small = self.layout & 0x08 > 0

    def __repr__(self):
        return (f'<multiviewer-properties mv={self.index} layout={self.layout} '
                f'flip={self.flip} u1={self.u1}>')


class MultiviewerInputField(Recv):
    """``MvIn`` — per-window source routing and overlay availability.

    Window numbering varies by switcher (see Software Control output for
    the specific model layout). On Mini Extreme: row-major from
    upper-left. On non-Extreme Minis: contiguous numbering with ``vu``
    and ``safearea`` only meaningful on certain windows.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Multiviewer index
    1      1    u8     Window index
    2      2    u16    Source index
    4      1    bool   VU meter overlay supported on this window
    5      1    bool   Safe-area overlay supported on this window
    6      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'MvIn'
    PRETTY = 'multiviewer-input'
    KEY_FORMAT = struct.Struct('>BB')

    index    = u8     (at=0)
    window   = u8     (at=1)
    source   = u16    (at=2)
    vu       = boolean(at=4)
    safearea = boolean(at=5)

    def __repr__(self):
        return (f'<multiviewer-input mv={self.index} win={self.window} '
                f'source={self.source}>')


class MultiviewerVuField(Recv):
    """``VuMC`` — VU-meter overlay enabled flag for one window.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Multiviewer index
    1      1    u8     Window index
    2      1    bool   VU enabled
    3      1    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'VuMC'
    PRETTY = 'multiviewer-vu'
    KEY_FORMAT = struct.Struct('>BB')

    index   = u8     (at=0)
    window  = u8     (at=1)
    enabled = boolean(at=2)

    def __repr__(self):
        return (f'<multiviewer-vu mv={self.index} win={self.window} '
                f'enabled={self.enabled}>')


class MultiviewerSafeAreaField(Recv):
    """``SaMw`` — safe-area overlay enabled flag for one window.

    Generally only enabled on the preview window.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Multiviewer index
    1      1    u8     Window index
    2      1    bool   Safe-area enabled
    3      1    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'SaMw'
    PRETTY = 'multiviewer-safe-area'
    KEY_FORMAT = struct.Struct('>BB')

    index   = u8     (at=0)
    window  = u8     (at=1)
    enabled = boolean(at=2)

    def __repr__(self):
        return (f'<multiviewer-safe-area mv={self.index} win={self.window} '
                f'enabled={self.enabled}>')


# -----------------------------------------------------------------------------
# Restored upstream commands (0.15, 2026-09-11)
#
# These Send classes existed in upstream pyatem and were dropped from the
# fork because nothing exercised them. They are back, declared in the DSL
# with the exact byte layout of upstream's struct.pack strings and pinned
# byte-for-byte in tests/test_restored_upstream_commands.py. They have NOT
# been re-verified against a switcher in this fork; treat them as upstream
# did, and Wireshark-check before relying on a write on your model.
# -----------------------------------------------------------------------------


class MultiviewInputCommand(Send):
    """``CMvI`` — route a source to a window of a multiview output.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Multiviewer index
    1      1    u8     Window index
    2      2    u16    Source index
    ====== ==== ====== ===========
    """
    CODE = 'CMvI'
    SIZE = 4

    index  = u8 (at=0)
    window = u8 (at=1)
    source = u16(at=2)

    def __init__(self, index, window, source):
        super().__init__(index=index, window=window, source=source)


class MultiviewPropertiesCommand(Send):
    """``CMvP`` — set a multiview output's layout and program/preview swap.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (bit 0 layout, bit 1 swap)
    1      1    u8     Multiviewer index
    2      1    u8     Layout (see MultiviewerPropertiesField)
    3      1    bool   Swap program/preview
    ====== ==== ====== ===========
    """
    CODE = 'CMvP'
    SIZE = 4
    MASK_AT = 0

    index  = u8     (at=1)
    layout = u8     (at=2, mask_bit=0)
    swap   = boolean(at=3, mask_bit=1)

    def __init__(self, index, layout=None, swap=None):
        super().__init__(index=index, layout=layout, swap=swap)


def set_multiviewer_input(conn, window, source, index=0):
    """Route ``source`` into ``window`` of multiviewer ``index``."""
    conn.send(MultiviewInputCommand(index=index, window=int(window), source=int(source)))


def set_multiviewer_layout(conn, layout, index=0):
    conn.send(MultiviewPropertiesCommand(index=index, layout=int(layout)))


def set_multiviewer_swap(conn, swap, index=0):
    """Swap the program and preview windows of multiviewer ``index``."""
    conn.send(MultiviewPropertiesCommand(index=index, swap=bool(swap)))
