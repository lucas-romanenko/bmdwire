# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
System info messages — identity + handshake + topology + time + lifecycle.

Everything the ATEM tells the client about itself: firmware version,
product name, M/E config, media-player slot counts, hardware topology,
the internal clock + timecode mode, plus connection-lifecycle events
(initial state dump complete, auto-input video mode, file-transfer
complete on the OpenSwitcher TCP variant).

Plus one outgoing packet — ``TiRq`` — used to read the system time
(also doubles as a NOP / keepalive).

Wire packets:
    _ver — incoming, firmware major/minor version
    Time — incoming, current internal clock
    TCCc — incoming, timecode mode (freerun / time-of-day)
    _pin — incoming, product name + model number
    _MeC — incoming, M/E config (one packet per M/E unit)
    _mpl — incoming, media-player slot counts
    MPCE — incoming, currently-loaded source for one media player
    _top — incoming, hardware topology (M/E / source / DSK counts, etc.)
    AiVM — incoming, automatic input video-mode detection state
    InCm — incoming, initial state-dump complete sentinel
    *XFC — incoming, file-transfer complete (OpenSwitcher TCP-only event)
    TiRq — outgoing, request system time / NOP keepalive
"""

import struct

from atemwire.state import decode_name
from atemwire.messages._dsl import Recv, Send, boolean, string, u8, u16
from atemwire.messages._dsl import Send, boolean, i16, string, u8, u16, u32  # noqa: F401  (restored upstream commands)


class FirmwareVersionField(Recv):
    """``_ver`` — firmware major/minor version.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Major version
    2      2    u16    Minor version
    ====== ==== ====== ===========
    """
    CODE = '_ver'
    PRETTY = 'firmware-version'

    major = u16(at=0)
    minor = u16(at=2)

    def __init__(self, raw: bytes):
        super().__init__(raw)
        self.version = f'{self.major}.{self.minor}'

    def __repr__(self):
        return f'<firmware-version {self.version}>'


class TimeField(Recv):
    """``Time`` — current internal clock (timecode-formatted).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Hours
    1      1    u8     Minutes
    2      1    u8     Seconds
    3      1    u8     Frames
    4      1    bool   Drop-frame
    5      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'Time'
    PRETTY = 'time'

    hours     = u8     (at=0)
    minutes   = u8     (at=1)
    seconds   = u8     (at=2)
    frames    = u8     (at=3)
    dropframe = boolean(at=4)

    def total_seconds(self):
        return self.seconds + 60 * self.minutes + 3600 * self.hours

    def __repr__(self):
        return f'<time {self.hours}:{self.minutes}:{self.seconds}:{self.frames}>'


class TimeConfigField(Recv):
    """``TCCc`` — timecode mode (freerun vs time-of-day).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mode (0 = freerun, 1 = time-of-day)
    1      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'TCCc'
    PRETTY = 'time-config'

    mode = u8(at=0)

    def __repr__(self):
        return f'<time-config mode={self.mode}>'


class ProductNameField(Recv):
    """``_pin`` — product name + model number.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      40   str    Product name (NUL-padded UTF-8)
    40     1    u8     Model number
    41     3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = '_pin'
    PRETTY = 'product-name'

    name  = string(at=0, size=40)
    model = u8    (at=40)

    def __repr__(self):
        return f'<product-name {self.name} (model 0x{self.model:02X})>'


class DeviceIdentityField(Recv):
    """``WhoI`` — the switcher's self-identity.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      32   str    Device id (32 hex chars)
    32     16   str    IP address (NUL-padded)
    48     64   str    mDNS hostname (NUL-padded)
    112    64   str    Device name — the custom name set in ATEM Setup
                       (NUL-padded)
    ====== ==== ====== ===========

    Captured 2026-08-04 from an ATEM 1 M/E Constellation HD (176 bytes
    total; added to this library 2026-08-23). Not all
    firmware sends this packet — readers must fall back to
    ``product-name``.
    """
    CODE = 'WhoI'
    PRETTY = 'device-identity'

    device_id = string(at=0, size=32)
    ip        = string(at=32, size=16)
    hostname  = string(at=48, size=64)
    name      = string(at=112, size=64)

    def __repr__(self):
        return f'<device-identity {self.name} ({self.ip})>'


class MixerEffectConfigField(Recv):
    """``_MeC`` — basic M/E config (one packet per M/E unit).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     M/E index
    1      1    u8     Number of upstream keyers
    2      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = '_MeC'
    PRETTY = 'mixer-effect-config'
    KEY_FORMAT = struct.Struct('>B')

    index  = u8(at=0)
    keyers = u8(at=1)

    def __repr__(self):
        return f'<mixer-effect-config m/e {self.index}: keyers={self.keyers}>'


class MediaplayerSlotsField(Recv):
    """``_mpl`` — media-player slot counts.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Number of still slots
    1      1    u8     Number of clip slots
    2      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = '_mpl'
    PRETTY = 'mediaplayer-slots'

    stills = u8(at=0)
    clips  = u8(at=1)

    def __repr__(self):
        return f'<mediaplayer-slots: stills={self.stills} clips={self.clips}>'


class MediaplayerSelectedField(Recv):
    """``MPCE`` — current media-pool source loaded into one media player.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Media player index
    1      1    u8     Source type (1 = still, 2 = clip)
    2      1    u8     Source / slot index
    3      1    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'MPCE'
    PRETTY = 'mediaplayer-selected'
    KEY_FORMAT = struct.Struct('>B')

    index       = u8(at=0)
    source_type = u8(at=1)
    slot        = u8(at=2)

    def __repr__(self):
        return (f'<mediaplayer-selected: index={self.index} '
                f'type={self.source_type} slot={self.slot}>')


# =============================================================================
# Hardware topology
# =============================================================================


class TopologyField(Recv):
    """``_top`` — hardware topology / capability counts.

    All fields are u8 packed contiguously starting at offset 0. The
    payload typically extends to 28 bytes; only the first ~14 are
    consistent across all known ATEM models. The remaining bytes are
    model-specific and ignored.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Number of M/E units
    1      1    u8     Sources count
    2      1    u8     Downstream keyers
    3      1    u8     AUX outputs
    4      1    u8     MixMinus outputs
    5      1    u8     Media players
    6      1    u8     Multiviewers
    7      1    u8     RS-485 ports
    8      1    u8     Hyperdeck slots
    9      1    u8     DVE blocks
    10     1    u8     Stinger blocks
    11     1    u8     SuperSource blocks
    12     1    u8     (multiviewer routable, exposed as bool)
    ====== ==== ====== ===========
    """
    CODE = '_top'
    PRETTY = 'topology'

    me_units          = u8(at=0)
    sources           = u8(at=1)
    downstream_keyers = u8(at=2)
    aux_outputs       = u8(at=3)
    mixminus_outputs  = u8(at=4)
    mediaplayers      = u8(at=5)
    multiviewers      = u8(at=6)
    rs485             = u8(at=7)
    hyperdecks        = u8(at=8)
    dve               = u8(at=9)
    stingers          = u8(at=10)
    supersources      = u8(at=11)
    _byte12           = u8(at=12)

    def __init__(self, raw: bytes):
        super().__init__(raw)
        self.multiviewer_routable = self._byte12 == 1

    def __repr__(self):
        return (f'<topology, me={self.me_units} sources={self.sources} '
                f'aux={self.aux_outputs}>')


# =============================================================================
# Connection lifecycle (auto-video-mode / init complete / transfer complete)
# =============================================================================


class AutoInputVideoModeField(Recv):
    """``AiVM`` — automatic input video-mode detection state.

    Only present on hardware that can auto-detect a video mode from the
    input signal.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    bool   Auto-detection enabled
    1      1    bool   A video mode has been detected
    2      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'AiVM'
    PRETTY = 'auto-input-video-mode'

    enabled  = boolean(at=0)
    detected = boolean(at=1)

    def __repr__(self):
        return (f'<auto-input-video-mode: enabled={self.enabled} '
                f'detected={self.detected}>')


class Sdi3GLevelField(Recv):
    """``V3sl`` — 3G-SDI output level (Level A vs Level B).

    The level byte is present regardless of the current video format; it
    only affects the signal when the switcher outputs a 3G format, but it
    always reflects the configured setting. Confirmed live (ATEM 1 M/E
    Constellation HD): Level A dump = ``01 00 00 00``, Level B =
    ``00 00 00 00``.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Level: 0 = Level B, 1 = Level A
    1      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'V3sl'
    PRETTY = 'sdi-3g-level'

    level = u8(at=0)

    def __repr__(self):
        return (f'<sdi-3g-level: '
                f'{"LevelA" if self.level == 1 else "LevelB"} '
                f'(level={self.level})>')


class InitCompleteField(Recv):
    """``InCm`` — initial-state-dump complete notification.

    Sentinel that signals "all the per-feature state packets you'll get
    on connect have been sent." Payload is opaque (the 4 bytes appear to
    be a constant ``\\x01\\x00\\x00\\x00``); we don't try to decode them.
    """
    CODE = 'InCm'

    def __init__(self, raw: bytes):
        self.raw = raw

    def __repr__(self):
        return '<init-complete>'


class TransferCompleteField(Recv):
    """``*XFC`` — file-transfer complete (OpenSwitcher TCP-only event).

    NOT part of the standard ATEM UDP protocol; emitted by an
    OpenSwitcher TCP relay when an upstream ATEM finishes a transfer.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Store index
    2      2    u16    Slot index
    4      1    bool   Is upload (False = download)
    5      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = '*XFC'
    PRETTY = 'transfer-complete'

    store  = u16    (at=0)
    slot   = u16    (at=2)
    upload = boolean(at=4)

    def __repr__(self):
        return (f'<*transfer-complete: store={self.store} slot={self.slot} '
                f'upload={self.upload}>')


# =============================================================================
# Time request (outgoing)
# =============================================================================


class TimeRequestCommand(Send):
    """``TiRq`` — request the hardware's system time.

    Doubles as a NOP / keepalive command because the ATEM responds with
    a Time field regardless of whether time has changed. No payload.
    """
    CODE = 'TiRq'
    SIZE = 0

    def __init__(self):
        super().__init__()


# =============================================================================
# Readers (no operations — system_info is read-only at the app layer)
# =============================================================================
#
# display_fps already lives in atemwire.state (lifted in fade_to_black
# commit 5a) because every rate-resolving operation across multiple
# features needs it. The remaining readers below stay here as
# bucket-B aggregators specific to the system_info Recv classes.


def video_mode(mx):
    """Return {'format': str, 'id': int} for the current video mode, or
    None if VidM hasn't arrived yet. The 'format' label is normalized
    to strip a leading 'f' prefix some legacy callers expected stripped."""
    node = mx.get('video-mode')
    if node is None:
        return None
    try:
        label = node.get_label()
    except Exception:
        label = ''
    if isinstance(label, str) and label.startswith('f'):
        label = label[1:]
    try:
        mode_id = int(getattr(node, 'mode', -1))
    except Exception:
        mode_id = -1
    return {'format': label, 'id': mode_id}


def available_video_modes(mx):
    """Return the list of video modes this hardware supports, parsed from
    the ``_VMC`` (VideoModeCapability) state. Each entry is
    ``{'id': int, 'label': str}``. Empty list during handshake."""
    cap = mx.get('video-mode-capability')
    if cap is None:
        return []
    out = []
    for entry in getattr(cap, 'modes', []) or []:
        try:
            mode_id = int(entry.get('modenum'))
            field = entry.get('mode')
            label = field.get_label() if field is not None else ''
        except Exception:
            continue
        out.append({'id': mode_id, 'label': label})
    return out


def video_resolution(mx, default=(1920, 1080)):
    """Return (width, height) tuple for the current video mode, falling
    back to ``default`` if the field isn't populated or the helper
    raises."""
    node = mx.get('video-mode')
    if node is None:
        return default
    try:
        return node.get_resolution()
    except Exception:
        return default


def product_name(mx, default=''):
    """ATEM's self-reported product name from the ``_pin`` packet.
    atemwire stores the name field as bytes; we decode and strip NULs
    via ``decode_name``."""
    node = mx.get('product-name')
    if node is None:
        return default
    name = getattr(node, 'name', None) or getattr(node, 'product_name', None)
    return decode_name(name, default)


def device_name(mx, default=''):
    """The switcher's user-assigned name from ATEM Setup (``WhoI``).
    Empty when the firmware doesn't send the packet or no custom name is
    set — callers fall back to ``product_name``."""
    node = mx.get('device-identity')
    if node is None:
        return default
    return decode_name(getattr(node, 'name', None), default)


def sdi_3g_level(mx, default='LevelB'):
    """Resolve the 3G-SDI output level to its XML token ('LevelA' /
    'LevelB') from the live ``V3sl`` packet. Returns ``default`` when the
    packet is absent (e.g. models with no 3G output)."""
    node = mx.get('sdi-3g-level')
    if node is None:
        return default
    return 'LevelA' if getattr(node, 'level', 0) == 1 else 'LevelB'


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


class AutoInputVideoModeCommand(Send):
    """``AiVM`` — enable or disable automatic video-mode detection.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    bool   Enable
    1      3    ?      unknown
    ====== ==== ====== ===========
    """
    CODE = 'AiVM'
    SIZE = 4

    enable = boolean(at=0)

    def __init__(self, enable):
        super().__init__(enable=enable)


class SaveStartupStateCommand(Send):
    """``SRsv`` — "Save startup state" (four padding bytes, no fields)."""
    CODE = 'SRsv'
    SIZE = 4

    def __init__(self):
        super().__init__()


class ClearStartupStateCommand(Send):
    """``SRcl`` — "Clear startup state" (four padding bytes, no fields)."""
    CODE = 'SRcl'
    SIZE = 4

    def __init__(self):
        super().__init__()


class SetTimeOfDayCommand(Send):
    """``SToD`` — set the switcher's clock (Software Control does this at
    startup from the computer's clock).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      4    u32    Unix timestamp (seconds)
    4      4    ?      padding
    ====== ==== ====== ===========

    Accepts a ``datetime`` or an integer timestamp. Upstream packed the
    float ``timestamp()`` straight into a u32, which raises on Python 3;
    the seconds are truncated to an int here.
    """
    CODE = 'SToD'
    SIZE = 8

    timestamp = u32(at=0)

    def __init__(self, time):
        import datetime as _dt
        if isinstance(time, _dt.datetime):
            time = time.timestamp()
        super().__init__(timestamp=int(time))


def set_auto_video_mode(conn, enable):
    conn.send(AutoInputVideoModeCommand(bool(enable)))


def save_startup_state(conn):
    """Make the current switcher state the power-on state."""
    conn.send(SaveStartupStateCommand())


def clear_startup_state(conn):
    conn.send(ClearStartupStateCommand())


def set_time_of_day(conn, when=None):
    """Set the switcher clock; ``when`` defaults to now."""
    import datetime as _dt
    conn.send(SetTimeOfDayCommand(when if when is not None else _dt.datetime.now(_dt.timezone.utc)))
