# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Media messages — media-pool slot management (capture / clear / metadata)
plus media-player source loading.

Conceptually corresponds to the "Media" tab in ATEM Software Control:
the media pool (slot inventory + the still capture / clear actions)
and the media players (one per MP, each pointing at a slot).

Wire packets:
    Capt — outgoing, capture program-output frame to next free slot
    CSTL — outgoing, clear a media-pool still slot
    MPSS — outgoing, set still or clip on a media player
    MPfe — incoming, media-pool slot metadata (type, hash, name, used flag)
"""

import struct

from atemwire._state import _kv, decode_name, md5_hex, safe_bool, safe_int
from atemwire.messages._dsl import Recv, Send, u8


# -----------------------------------------------------------------------------
# Outgoing — media pool slot management
# -----------------------------------------------------------------------------


class CaptureStillCommand(Send):
    """``Capt`` — capture the current program-output frame to the media pool.

    Equivalent to "Capture Still" in the Output menu of the UI Switcher
    panel. No payload — the ATEM picks the next free slot and reports the
    result via subsequent ``MPfe`` updates.
    """
    CODE = 'Capt'
    SIZE = 0

    def __init__(self):
        super().__init__()


class ClearStillCommand(Send):
    """``CSTL`` — clear a media-pool still slot.

    WARNING: whether the switcher honours a BARE CSTL is model/firmware-
    dependent — a Constellation HD does (commit ``fe5c43b``), but a 1 M/E
    Production Studio 4K silently ignores it unless the sending session
    holds the still-store lock (observed live 2026-07-29). Don't send this
    directly; use ``ATEMConnection.clear_still`` / ``protocol.queue_clear``
    (the locked clear), which works on both.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Slot index (0-based)
    1      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'CSTL'
    SIZE = 4

    slot = u8(at=0)

    def __init__(self, slot):
        super().__init__(slot=slot)


# -----------------------------------------------------------------------------
# Outgoing — media player source loading
# -----------------------------------------------------------------------------


class MediaplayerSelectCommand(Send):
    """``MPSS`` — load a still or clip into a media player.

    Either ``still`` or ``clip`` must be specified; not both. The mask
    byte and ``source_type`` byte are derived from which arg was passed.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (bit 0 always set, bit 1 still, bit 2 clip)
    1      1    u8     Mediaplayer index
    2      1    u8     Source type (1 = still, 2 = clip)
    3      1    u8     Still index
    4      1    u8     Clip index
    5      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'MPSS'
    SIZE = 8

    mask        = u8(at=0)
    index       = u8(at=1)
    source_type = u8(at=2)
    still       = u8(at=3)
    clip        = u8(at=4)

    def __init__(self, index, still=None, clip=None):
        if still is not None and clip is not None:
            raise ValueError("Can only set still= or clip=, not both")
        if still is None and clip is None:
            raise ValueError("Either still or clip is required")

        mask = 1
        if still is not None:
            mask |= 1 << 1
            source_type = 1
        else:
            mask |= 1 << 2
            source_type = 2
        super().__init__(
            mask=mask, index=index, source_type=source_type,
            still=still if still is not None else 0,
            clip=clip if clip is not None else 0,
        )


# -----------------------------------------------------------------------------
# Incoming — media pool slot metadata
# -----------------------------------------------------------------------------


class MediaplayerFileInfoField(Recv):
    """``MPfe`` — media-pool slot metadata (type, hash, name, in-use flag).

    Variable-length payload because the slot name is a Pascal-style
    length-prefixed string at the end. The leading 23 bytes are fixed.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Slot type (0 = still)
    1      1    ?      padding
    2      2    u16    Slot index
    4      1    bool   Is used
    5      16   bytes  MD5 hash of slot data
    21     2    ?      padding
    23     N    pstr   Name (1-byte length prefix + UTF-8 bytes)
    ====== ==== ====== ===========
    """
    CODE = 'MPfe'
    PRETTY = 'mediaplayer-file-info'
    KEY_FORMAT = struct.Struct('>xxH')

    def __init__(self, raw: bytes):
        self.raw = raw
        namelen = max(0, len(raw) - 23)
        fmt = f'>Bx H ? 16s 2x {namelen}p'
        (self.type, self.index, self.is_used,
         self.hash, self.name) = struct.unpack(fmt, raw)

    def __repr__(self):
        return (f'<mediaplayer-file-info: type={self.type} index={self.index} '
                f'used={self.is_used} name={self.name}>')


# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def clear_still(conn, slot):
    """Clear a media pool still slot. ``slot`` is 0-indexed.

    WARNING: this is the BARE wire send, which some ATEM models silently
    ignore unless the session holds the still-store lock (see the
    ``ClearStillCommand`` docstring). Application code should use
    ``ATEMConnection.clear_still`` (the locked clear) instead."""
    conn.send(ClearStillCommand(slot=int(slot)))


def capture_still(conn):
    """Capture the current Program output into a new media pool slot.

    The ATEM allocates the slot itself; the caller observes the new slot
    via ``mediaplayer-file-info`` mixerstate events. The full capture
    pipeline (Phase 1/2/3 wait + raw download + PNG encode) lives in the
    application layer; this op is just the wire send."""
    conn.send(CaptureStillCommand())


def set_media_player_still(conn, player, slot):
    """Point a media player at a still in the media pool.

    Sets both source-type (→still) and slot in one MPSS packet — the
    MediaplayerSelectCommand wire format encodes a mask byte, the
    source_type field (1=still, 2=clip), and the slot index together."""
    conn.send(MediaplayerSelectCommand(index=int(player), still=int(slot)))


def set_media_player_clip(conn, player, slot):
    """Point a media player at a clip in the media pool. Sets source-type
    to 2=clip plus the slot."""
    conn.send(MediaplayerSelectCommand(index=int(player), clip=int(slot)))


# -----------------------------------------------------------------------------
# Readers
# -----------------------------------------------------------------------------


def mediaplayer_slots(mx):
    """Total still-slot capacity reported by the switcher."""
    node = mx.get('mediaplayer-slots')
    return safe_int(getattr(node, 'stills', 0), 0) if node else 0


def mediaplayer_slot_info(mx, idx):
    """Per-slot metadata: is_used, hex hash, decoded name. Bucket B
    because the dict structure can't collapse to scale= on a Recv."""
    node = _kv(mx, 'mediaplayer-file-info', idx)
    if node is None:
        return {'is_used': False, 'hash': '', 'name': ''}
    return {
        'is_used': safe_bool(getattr(node, 'is_used', False), False),
        'hash': md5_hex(getattr(node, 'hash', b''), ''),
        'name': decode_name(getattr(node, 'name', b''), ''),
    }


def mediaplayer_selected(mx, mp_idx):
    """Which media-pool slot a given media player is pointing at, and
    what kind (still vs clip via ``source_type``)."""
    node = _kv(mx, 'mediaplayer-selected', mp_idx)
    if node is None:
        return {'source_type': 0, 'slot': 0}
    return {
        'source_type': safe_int(getattr(node, 'source_type', 0), 0),
        'slot': safe_int(getattr(node, 'slot', 0), 0),
    }
