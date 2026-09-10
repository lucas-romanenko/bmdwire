# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Input properties + video mode messages.

Input properties carry the per-source label, port type, available routing,
etc. Video mode is the global resolution/framerate setting.

Wire packets:
    CInL — outgoing, set input label / port type
    CVdM — outgoing, change global video mode (destructive — causes resync)
    InPr — incoming, current input properties (one per source)
    VidM — incoming, current video mode
    _VMC — incoming, supported video modes capability descriptor
"""

import struct

from atemwire._state import _kv, decode_name
from atemwire.messages._dsl import Recv, Send, string, u8, u16


# Name → wire enum (matches VideoModeField.modes table). The XML form of
# the rate elides the decimal point (29.97 → "2997", 59.94 → "5994").
VIDEO_MODE_NAMES = {
    'NTSC': 0, '525i5994': 0,
    'PAL': 1, '625i50': 1,
    'NTSC_widescreen': 2,
    'PAL_widescreen': 3,
    '720p50': 4, '720p5994': 5, '720p60': 28,
    '1080i50': 6, '1080i5994': 7, '1080i60': 29,
    '1080p2398': 8, '1080p24': 9, '1080p25': 10,
    '1080p2997': 11, '1080p30': 26,
    '1080p50': 12, '1080p5994': 13, '1080p60': 27,
    '2160p2398': 14, '2160p24': 15, '2160p25': 16, '2160p2997': 17,
    '2160p50': 18, '2160p5994': 19,
    '4320p2398': 20, '4320p24': 21, '4320p25': 22, '4320p2997': 23,
    '4320p50': 24, '4320p5994': 25,
}


# -----------------------------------------------------------------------------
# Outgoing
# -----------------------------------------------------------------------------


class InputPropertiesCommand(Send):
    """``CInL`` — set labels and port type for one input.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask
    1      1    ?      padding
    2      2    u16    Source index
    4      20   str    Long label (NUL-padded UTF-8)
    24     4    str    Short label (4-char button label)
    28     2    u16    Port type
    30     2    ?      padding
    ====== ==== ====== ===========

    Mask bits: 0 long label, 1 short label, 2 port type.
    """
    CODE = 'CInL'
    SIZE = 32
    MASK_AT = 0

    source_index = u16   (at=2)
    label        = string(at=4, size=20)
    short_label  = string(at=24, size=4)
    port_type    = u16   (at=28)

    def __init__(self, source_index, label=None, short_label=None, port_type=None):
        self.source_index = source_index
        self.label = label
        self.short_label = short_label
        self.port_type = port_type

    def get_command(self) -> bytes:
        # Custom mask logic — set bits per actual passed argument, NOT
        # whether the field is non-empty. Avoid the DSL's automatic
        # mask_bit handling because string fields don't fit the same
        # "None means skip" pattern (empty string is a valid label).
        mask = 0
        if self.label is not None:
            mask |= 1 << 0
        if self.short_label is not None:
            mask |= 1 << 1
        if self.port_type is not None:
            mask |= 1 << 2
        buf = bytearray(self.SIZE)
        buf[0] = mask
        struct.pack_into('>H', buf, 2, self.source_index)
        if self.label is not None:
            label_b = self.label.encode('utf-8')[:20]
            struct.pack_into('>20s', buf, 4, label_b)
        if self.short_label is not None:
            short_b = self.short_label.encode('utf-8')[:4]
            struct.pack_into('>4s', buf, 24, short_b)
        if self.port_type is not None:
            struct.pack_into('>H', buf, 28, self.port_type)
        header = struct.pack('>H 2x 4s', len(buf) + 8, self.CODE.encode())
        return header + bytes(buf)


class VideoModeCommand(Send):
    """``CVdM`` — set the global video mode.

    Destructive — outputs drop briefly while the ATEM resyncs and
    clients may be force-disconnected. ``atemwire.profile.apply`` waits
    for the connection to settle before continuing.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Video mode ID (matches VideoModeField enum)
    1      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'CVdM'
    SIZE = 4

    mode = u8(at=0)

    def __init__(self, mode):
        super().__init__(mode=mode)


# -----------------------------------------------------------------------------
# Incoming
# -----------------------------------------------------------------------------


class InputPropertiesField(Recv):
    """``InPr`` — full per-source input description (label, port type,
    available routing flags). One field per input source.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Source index
    2      20   str    Long name
    22     4    str    Short name (button label)
    26     1    bool   Default name in use
    27     1    ?      padding
    28     2    u16    Source ports (bitfield: available connector types)
    30     2    u16    External port type bitfield
    32     1    u8     Port type (input/output category)
    33     1    ?      padding
    34     1    u8     Routing-availability bitfield (aux/multiview/SS/key/...)
    35     1    u8     M/E availability bitfield
    ====== ==== ====== ===========
    """
    CODE = 'InPr'
    PRETTY = 'input-properties'
    KEY_FORMAT = struct.Struct('>H')

    PORT_EXTERNAL         = 0
    PORT_BLACK            = 1
    PORT_BARS             = 2
    PORT_COLOR            = 3
    PORT_MEDIAPLAYER      = 4
    PORT_MEDIAPLAYER_KEY  = 5
    PORT_SUPERSOURCE      = 6
    PORT_PASSTHROUGH      = 7
    PORT_ME_OUTPUT        = 128
    PORT_AUX_OUTPUT       = 129
    PORT_KEY_MASK         = 130
    PORT_MULTIVIEW_OUTPUT = 131

    def __init__(self, raw: bytes):
        self.raw = raw
        fields = struct.unpack('>H 20s 4s ?x HH Bx BB', raw)
        self.index = fields[0]
        self.name = fields[1].split(b'\x00')[0].decode('utf-8', errors='replace')
        self.short_name = fields[2].split(b'\x00')[0].decode('utf-8', errors='replace')
        self.default_name = fields[3]
        self.source_ports = fields[4]
        self.external_port_type = fields[5]
        self.port_type = fields[6]

        self.available_aux              = fields[7] & (1 << 0) != 0
        self.available_multiview        = fields[7] & (1 << 1) != 0
        self.available_supersource_art  = fields[7] & (1 << 2) != 0
        self.available_supersource_box  = fields[7] & (1 << 3) != 0
        self.available_key_source       = fields[7] & (1 << 4) != 0
        self.available_aux1             = fields[7] & (1 << 5) != 0
        self.available_aux2             = fields[7] & (1 << 6) != 0
        self.available_usb              = fields[7] & (1 << 7) != 0

        # Per-M/E bus availability bits — which M/E program/preview buses
        # may take this source. This is how M/E re-entry rules arrive on
        # the wire (e.g. on a 4 M/E unit, "M/E 3 PGM" is flagged available
        # to the M/E 1/2 buses but not its own). Bits 2/3 only ever set by
        # 3+ M/E units; they read as 0 elsewhere.
        self.available_me1 = fields[8] & (1 << 0) != 0
        self.available_me2 = fields[8] & (1 << 1) != 0
        self.available_me3 = fields[8] & (1 << 2) != 0
        self.available_me4 = fields[8] & (1 << 3) != 0

    def __repr__(self):
        return (f'<input-properties: index={self.index} name={self.name!r} '
                f'short={self.short_name!r}>')


class VideoModeField(Recv):
    """``VidM`` — current global video mode.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Video mode enum
    1      3    ?      padding
    ====== ==== ====== ===========

    The ``mode`` integer maps to a ``(resolution, interlaced, rate,
    widescreen)`` tuple via ``_MODE_TABLE``. Helper methods produce a
    label string and a (width, height) pixel resolution.
    """
    CODE = 'VidM'
    PRETTY = 'video-mode'

    mode = u8(at=0)

    # (vertical resolution, interlaced, rate, widescreen)
    _MODE_TABLE = {
        0:  (525,  True,  59.94, False),
        1:  (625,  True,  50,    False),
        2:  (525,  True,  59.94, True),
        3:  (625,  True,  50,    True),
        4:  (720,  False, 50,    True),
        5:  (720,  False, 59.94, True),
        6:  (1080, True,  50,    True),
        7:  (1080, True,  59.94, True),
        8:  (1080, False, 23.98, True),
        9:  (1080, False, 24,    True),
        10: (1080, False, 25,    True),
        11: (1080, False, 29.97, True),
        12: (1080, False, 50,    True),
        13: (1080, False, 59.94, True),
        14: (2160, False, 23.98, True),
        15: (2160, False, 24,    True),
        16: (2160, False, 25,    True),
        17: (2160, False, 29.97, True),
        18: (2160, False, 50,    True),
        19: (2160, False, 59.94, True),
        20: (4320, False, 23.98, True),
        21: (4320, False, 24,    True),
        22: (4320, False, 25,    True),
        23: (4320, False, 29.97, True),
        24: (4320, False, 50,    True),
        25: (4320, False, 59.94, True),
        26: (1080, False, 30,    True),
        27: (1080, False, 60,    True),
        28: (720,  False, 60,    True),
        29: (1080, True,  60,    True),
    }

    def __init__(self, raw: bytes):
        super().__init__(raw)
        if self.mode not in self._MODE_TABLE:
            raise ValueError(f'Unknown resolution code {self.mode}, cannot continue')
        res, inter, rate, ws = self._MODE_TABLE[self.mode]
        self.resolution = res
        self.interlaced = inter
        self.rate = rate
        self.widescreen = ws

    def get_label(self):
        if self.resolution is None:
            return f'unknown [{self.mode}]'
        pi = 'i' if self.interlaced else 'p'
        aspect = ''
        if self.resolution < 720:
            aspect = ' 16:9' if self.widescreen else ' 4:3'
        return f'{self.resolution}{pi}{self.rate}{aspect}'

    def get_pixels(self):
        w, h = self.get_resolution()
        return w * h

    def get_resolution(self):
        lut = {
            525:  (720, 480),
            625:  (720, 576),
            720:  (1280, 720),
            1080: (1920, 1080),
            2160: (3840, 2160),
            4320: (7680, 4320),
        }
        return lut[self.resolution]

    def __repr__(self):
        return f'<video-mode: mode={self.mode}: {self.get_label()}>'


class VideoModeCapabilityField(Recv):
    """``_VMC`` — supported video modes + their multiview / downconvert options.

    Variable-length payload (count + N×13-byte mode descriptors), so
    parsing is hand-rolled.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Number of supported video modes
    2      2    ?      padding
    4      1    u8     Video mode N (per-mode block follows)
    5      3    ?      padding
    8      4    u32    Multiview modes bitfield
    12     4    u32    Downconvert modes bitfield
    16     1    bool   Requires reconfiguration
    ====== ==== ====== ===========
    """
    CODE = '_VMC'
    PRETTY = 'video-mode-capability'

    def __init__(self, raw: bytes):
        self.raw = raw
        count, = struct.unpack_from('>H', raw, 0)
        self.modes = []
        for i in range(count):
            vidm, multiview, downscale, reconfig = struct.unpack_from(
                '>B3x I I ?', raw, 4 + (i * 13))
            self.modes.append({
                'modenum': vidm,
                'mode': self._int_to_mode(vidm),
                'multiview': self._bitfield_to_modes(multiview),
                'downscale': self._bitfield_to_modes(downscale),
                'reconfigure': reconfig,
            })

    def _bitfield_to_modes(self, bitfield):
        return [self._int_to_mode(i)
                for i in range(32) if bitfield & (1 << i)]

    def _int_to_mode(self, mode):
        return VideoModeField(struct.pack('>1B3x', mode))

    def __repr__(self):
        labels = ' '.join(m['mode'].get_label() for m in self.modes)
        return f'<video-mode-capability: {labels}>'


# =============================================================================
# Operations
# =============================================================================


def set_input_label(conn, source, *, long_name=None, short_name=None):
    """Rename one input. Pass either or both labels; missing args leave
    that field on the switcher unchanged. Strings are UTF-8 encoded;
    ``long_name`` is truncated to 20 bytes and ``short_name`` to 4 bytes
    by the wire format. Bucket B (multi-field optional-kwarg setter)."""
    conn.send(InputPropertiesCommand(
        source_index=int(source),
        label=long_name,
        short_label=short_name,
    ))


def set_video_mode(conn, mode):
    """Set the switcher's main video mode.

    ``mode`` may be the integer enum (matches ``VideoModeField.mode``) or
    the XML / Software-Control string form (e.g. ``'1080p60'``,
    ``'1080p5994'``). Strings are case-sensitive and must match the
    ``VIDEO_MODE_NAMES`` table. Bucket B (enum table lookup).

    Destructive — the ATEM resyncs all outputs, drops video briefly, and
    may force-disconnect clients. Callers (typically ``Profile.apply``)
    should wait for the connection to settle before continuing."""
    if isinstance(mode, str):
        if mode not in VIDEO_MODE_NAMES:
            raise ValueError(f"unknown video mode name {mode!r}")
        mode_int = VIDEO_MODE_NAMES[mode]
    else:
        mode_int = int(mode)
    conn.send(VideoModeCommand(mode=mode_int))


# =============================================================================
# Readers
# =============================================================================


def input_label(mx, source):
    """Return {long, short} for an input source from InPr."""
    node = _kv(mx, 'input-properties', source)
    if node is None:
        return {'long': '', 'short': ''}
    return {
        'long': decode_name(getattr(node, 'name', b''), ''),
        'short': decode_name(getattr(node, 'short_name', b''), ''),
    }


def all_input_sources(mx):
    """Return every entry in ``input-properties`` as a flat list with
    metadata, for use by source-picker dropdowns. Each entry:
    ``{source, long, short, port_type, available_aux, available_key_source,
    available_multiview}``. Sorted by source id.

    Frontend (``$store.atem.sourcesFor(ctx)``) filters this list per
    dropdown context — 'aux', 'key_source', 'fill' — so dropdowns
    automatically scale to the connected ATEM's topology and pick up
    operator renames."""
    inputs = mx.get('input-properties', {}) or {}
    out = []
    for src_id, node in inputs.items():
        if node is None:
            continue
        try:
            source = int(src_id)
            port_type = int(getattr(node, 'port_type', -1))
        except Exception:
            continue
        out.append({
            'source': source,
            'long': decode_name(getattr(node, 'name', b''), ''),
            'short': decode_name(getattr(node, 'short_name', b''), ''),
            'port_type': port_type,
            'available_aux': bool(getattr(node, 'available_aux', False)),
            'available_key_source': bool(getattr(node, 'available_key_source', False)),
            'available_multiview': bool(getattr(node, 'available_multiview', False)),
            # Per-M/E bus availability (the wire's M/E re-entry rules) —
            # consumed by the control page's bus rows to offer M/E outputs
            # as sources on multi-M/E units.
            'available_me1': bool(getattr(node, 'available_me1', False)),
            'available_me2': bool(getattr(node, 'available_me2', False)),
            'available_me3': bool(getattr(node, 'available_me3', False)),
            'available_me4': bool(getattr(node, 'available_me4', False)),
        })
    out.sort(key=lambda x: x['source'])
    return out


def renamable_inputs(mx):
    """Return renamable inputs grouped to mirror Software Control's three
    Labels tabs: ``inputs`` (cameras), ``outputs`` (M/E, aux,
    multiview), ``media`` (color generators, media players + their key
    channels). Each entry: ``{'source': int, 'long': str, 'short': str}``,
    sorted by source id within its bucket. Internal fixed sources
    (Black, Color Bars, Key Mask, Program, Preview) are excluded —
    ATEM doesn't accept rename on those.

    Port type values come from ``InputPropertiesField`` constants:
        PORT_EXTERNAL=0     → inputs (cameras)
        PORT_COLOR=3        → media (color generators 1, 2)
        PORT_MEDIAPLAYER=4  → media (MP1, MP2, ...)
        PORT_MEDIAPLAYER_KEY=5 → media (MP1 Key, MP2 Key, ...)
        PORT_ME_OUTPUT=128  → outputs (program/preview taps)
        PORT_AUX_OUTPUT=129 → outputs (aux 1-N)
        PORT_MULTIVIEW_OUTPUT=131 → outputs (multiview windows)
    """
    inputs = mx.get('input-properties', {}) or {}
    out = {'inputs': [], 'outputs': [], 'media': []}
    for src_id, node in inputs.items():
        if node is None:
            continue
        try:
            source = int(src_id)
            port_type = int(getattr(node, 'port_type', -1))
        except Exception:
            continue
        entry = {
            'source': source,
            'long': decode_name(getattr(node, 'name', b''), ''),
            'short': decode_name(getattr(node, 'short_name', b''), ''),
        }
        if port_type == 0:
            out['inputs'].append(entry)
        elif port_type in (128, 129, 131):
            out['outputs'].append(entry)
        elif port_type in (3, 4, 5):
            out['media'].append(entry)
        # else: PORT_BLACK / PORT_BARS / PORT_KEY_MASK / etc. — fixed,
        # not included.
    for bucket in out.values():
        bucket.sort(key=lambda x: x['source'])
    return out
