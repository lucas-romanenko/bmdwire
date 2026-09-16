# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Fade-to-black messages — trigger / configure / state / enable.

Wire packets:
    FtbA — outgoing, trigger an FtB on a given M/E
    FtbC — outgoing, configure the FtB transition rate
    FEna — outgoing/incoming, enable/disable FtB for an M/E. Special-cased
            because the wire layout differs between enable and disable
            packets (reverse-engineered, not in BMD's protocol docs).
    FtbS — incoming, current FtB transition state
    FtbP — incoming, current FtB rate setting
"""

import struct

from atemwire.state import _kv, display_fps, safe_bool, safe_int
from atemwire.helpers import parse_rate
from atemwire.messages._dsl import Recv, Send, boolean, u8


# -----------------------------------------------------------------------------
# Outgoing — trigger / configure / enable
# -----------------------------------------------------------------------------


class FadeToBlackCommand(Send):
    """``FtbA`` — trigger the fade-to-black transition.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'FtbA'
    SIZE = 4

    index = u8(at=0)

    def __init__(self, index):
        super().__init__(index=index)


class FadeToBlackConfigCommand(Send):
    """``FtbC`` — configure the fade-to-black transition duration.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (always 1)
    1      1    u8     M/E index
    2      1    u8     Frames
    3      1    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'FtbC'
    SIZE = 4

    mask   = u8(at=0)
    index  = u8(at=1)
    frames = u8(at=2)

    def __init__(self, index, frames):
        super().__init__(mask=1, index=index, frames=frames)


class FadeToBlackEnableCommand(Send):
    """``FEna`` — enable or disable FtB for an M/E.

    Reverse-engineered, not in BMD's public protocol docs. The wire
    layout differs between enable and disable packets, which is why this
    class overrides ``get_command`` rather than declaring a static field
    layout:

      Enable packet:  ``[00 01 xx xx]``
      Disable packet: ``[00 <mE> 00 xx]``

    Ambiguity note: a multi-ME disable to ME2 produces ``[00 01 00]``,
    indistinguishable from an ME1 enable. Callers that only drive ME1
    are unaffected.
    """
    CODE = 'FEna'
    SIZE = 4

    def __init__(self, index, enable):
        self.index = index
        self.enable = enable

    def get_command(self) -> bytes:
        if self.enable:
            data = struct.pack('>BB 2x', 0x00, 0x01)
        else:
            data = struct.pack('>BBB x', 0x00, self.index, 0x00)
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


# -----------------------------------------------------------------------------
# Incoming — state / rate / enable
# -----------------------------------------------------------------------------


class FadeToBlackStateField(Recv):
    """``FtbS`` — current fade-to-black transition state.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    bool   FtB done (blinking button on control panel)
    2      1    bool   FtB transitioning (solid red on control panel)
    3      1    u8     Frames remaining in transition
    ====== ==== ====== ===========
    """
    CODE = 'FtbS'
    PRETTY = 'fade-to-black-state'
    KEY_FORMAT = struct.Struct('>B')

    index            = u8     (at=0)
    done             = boolean(at=1)
    transitioning    = boolean(at=2)
    frames_remaining = u8     (at=3)

    def __repr__(self):
        return (f'<fade-to-black-state: me={self.index}, done={self.done}, '
                f'transitioning={self.transitioning}, '
                f'frames-remaining={self.frames_remaining}>')


class FadeToBlackField(Recv):
    """``FtbP`` — fade-to-black rate setting.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Rate (frames)
    2      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'FtbP'
    PRETTY = 'fade-to-black'
    KEY_FORMAT = struct.Struct('>B')

    index = u8(at=0)
    rate  = u8(at=1)

    def __repr__(self):
        return f'<fade-to-black: me={self.index}, rate={self.rate}>'


class FadeToBlackEnabledField(Recv):
    """``FEna`` — current FtB enabled/disabled state for an M/E.

    Decoding heuristic (reverse-engineered): bytes 0-1 of ``[00 01 ...]``
    are read as "enabled", anything else as "disabled". Unambiguous on
    single-ME switchers; on multi-ME a disable targeting ME2 looks like
    an ME1 enable. Callers that only drive ME1 are unaffected.

    :ivar enabled: FtB is enabled for the targeted M/E
    :ivar disabled: logical negation of ``enabled``
    """
    CODE = 'FEna'
    PRETTY = 'fade-to-black-enabled'

    def __init__(self, raw: bytes):
        self.raw = raw
        byte0 = raw[0] if len(raw) > 0 else 0
        byte1 = raw[1] if len(raw) > 1 else 0
        self.enabled = (byte0 == 0 and byte1 == 1)
        self.disabled = not self.enabled

    def __repr__(self):
        return f'<fade-to-black-enabled: enabled={self.enabled}>'


# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def fade_to_black(conn, me=0):
    conn.send(FadeToBlackCommand(index=me))


def set_ftb_rate(conn, rate_str, me=0):
    """Parse the ``seconds:frames`` rate string against the ATEM's
    display fps and send the FtbC packet. Bucket B because the parse
    reads ``video-mode.rate`` from mixerstate via ``display_fps``."""
    conn.send(FadeToBlackConfigCommand(
        index=me,
        frames=parse_rate(rate_str, display_fps(conn.mixerstate)),
    ))


def set_ftb_disabled(conn, disabled, me=0):
    """Frontend sends ``disabled`` (bool). Map to FEna ``enable=!disabled``.
    Bucket B because the semantic flip isn't a numeric scale = DSL can't
    express it.

    ``me`` is accepted (the dispatch table threads it into every per-M/E
    verb) but DELIBERATELY IGNORED: the FEna disable packet carries the
    M/E index at the byte where the enable packet carries the 0x01 enable
    marker, so "disable ME2" is byte-identical to "enable ME1". Until a
    multi-M/E capture settles the real per-M/E layout, the send is pinned
    to ME1 (index 0) so it can never misfire against a background M/E."""
    conn.send(FadeToBlackEnableCommand(index=0, enable=not bool(disabled)))


# -----------------------------------------------------------------------------
# Readers
# -----------------------------------------------------------------------------


def ftb_active(mx, me=0):
    return safe_bool(_kv(mx, 'fade-to-black-state', me, attr='done'), False)


def ftb_in_transition(mx, me=0):
    return safe_bool(
        _kv(mx, 'fade-to-black-state', me, attr='transitioning'), False)


def ftb_frames_remaining(mx, me=0):
    return safe_int(
        _kv(mx, 'fade-to-black-state', me, attr='frames_remaining'), 0)


def ftb_rate(mx, me=0):
    return safe_int(_kv(mx, 'fade-to-black', me, attr='rate'), 25)


def ftb_disabled(mx):
    # FEna is a singleton — no ME index in the wire format.
    node = mx.get('fade-to-black-enabled')
    return safe_bool(getattr(node, 'disabled', False), False) if node else False
