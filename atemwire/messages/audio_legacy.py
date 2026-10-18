# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Legacy audio mixer messages (pre-Fairlight, smaller ATEMs).

Wire packets (incoming only):
    AMMO — master bus properties (volume + AFV)
    AMmO — monitor bus properties (volume / mute / solo / dim)
    AMIP — per-input channel-strip properties
    AMTl — audio mixer tally state (variable-length)

The readers at the bottom (``legacy_audio_*``) give ``build_full_state``
the same audio shape the Fairlight readers do, and ``audio_mixer_type``
says which of the two a switcher has.
"""

import math
import struct
from typing import Optional

from atemwire.state import decode_name, safe_bool, safe_int
from atemwire.messages._dsl import Recv, boolean, u16
from atemwire.messages._dsl import Send, boolean, i16, string, u8, u16, u32  # noqa: F401  (restored upstream commands)


class AudioMixerMasterPropertiesField(Recv):
    """``AMMO`` — master bus settings on legacy audio units.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Program gain
    2      2    ?      padding
    4      1    bool   Audio follows video (fade-to-black)
    5      3    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'AMMO'
    PRETTY = 'audio-mixer-master-properties'

    volume = u16    (at=0)
    afv    = boolean(at=4)

    def __repr__(self):
        return f'<audio-master-properties: volume={self.volume} afv={self.afv}>'


class AudioMixerMonitorPropertiesField(Recv):
    """``AMmO`` — monitor bus settings on legacy audio units.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    bool   Monitoring enabled
    1      1    ?      padding
    2      2    u16    Volume
    4      1    bool   Mute
    5      1    bool   Solo
    6      2    u16    Solo source index
    8      1    bool   Dim
    9      1    ?      padding
    10     2    u16    Dim volume
    ====== ==== ====== ===========
    """
    CODE = 'AMmO'
    PRETTY = 'audio-mixer-monitor-properties'

    enabled     = boolean(at=0)
    volume      = u16    (at=2)
    mute        = boolean(at=4)
    solo        = boolean(at=5)
    solo_source = u16    (at=6)
    dim         = boolean(at=8)
    dim_volume  = u16    (at=10)

    def __repr__(self):
        return f'<audio-monitor-properties: volume={self.volume}>'


class AudioMixerTallyField(Recv):
    """``AMTl`` — tally state on legacy audio units. Variable-length
    payload (count + N entries), so parsing is hand-rolled.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Number of tally lights
    n      2    u16    Audio source
    n+2    1    bool   Is mixed in (on/off)
    ====== ==== ====== ===========
    """
    CODE = 'AMTl'
    PRETTY = 'audio-mixer-tally'

    def __init__(self, raw: bytes):
        self.raw = raw
        offset = 0
        self.num, = struct.unpack_from('>H', raw, offset)
        offset += 2
        self.tally = {}
        for _ in range(self.num):
            source, tally = struct.unpack_from('>H?', raw, offset)
            strip_id = f'{source}.0'
            self.tally[strip_id] = tally
            offset += 3

    def __repr__(self):
        return f'<audio-mixer-tally {self.tally}>'


class AtemEqBandPropertiesField(Recv):
    """``AEBP`` — EQ band settings on Fairlight strips. One field per
    band, six bands per strip.

    The parsing is preserved from the original implementation
    byte-for-byte; the wire-layout offsets in the original docstring
    don't quite match the actual struct format — trust the format string.
    """
    CODE = 'AEBP'
    PRETTY = 'atem-eq-band-properties'
    KEY_FORMAT = struct.Struct('>H14xB')

    def __init__(self, raw: bytes):
        self.raw = raw
        values = struct.unpack('>H 2x 4x 6x BB B ? B B x B 4x H i H 2x', raw)
        self.index = values[0]
        self.is_split = values[1]
        self.subchannel = values[2]
        self.band_index = values[3]
        self.band_enabled = values[4]
        self.band_possible_filters = values[5]
        self.band_filter = values[6]
        self.band_freq_range = values[7]
        self.band_frequency = values[8]
        self.band_gain = values[9]
        self.band_q = values[10]

        self.strip_id = str(self.index)
        if self.is_split == 0xff:
            self.strip_id += '.' + str(self.subchannel)
        else:
            self.strip_id += '.0'

    def __repr__(self):
        filters = {
            0x01: 'low-shelf', 0x02: 'low-pass', 0x04: 'bell',
            0x08: 'notch', 0x10: 'high-pass', 0x20: 'high-shelf',
        }
        f_name = filters.get(self.band_filter, f'filter {self.band_filter}')
        on = '[on]' if self.band_enabled else '[off]'
        return (f'<atem-eq-band-properties {self.strip_id} band '
                f'{self.band_index} {f_name}{on} freq {self.band_frequency} '
                f'gain {self.band_gain} Q {self.band_q}>')


class AtemMasterEqBandPropertiesField(Recv):
    """``AMBP`` — master-out EQ band. Same band fields as ``AEBP`` minus
    the 16-byte per-strip prefix (index / split / subchannel). One packet
    per band; keyed on ``band_index`` so all six bands persist in
    mixerstate rather than overwriting a single slot."""
    CODE = 'AMBP'
    PRETTY = 'atem-master-eq-band-properties'
    KEY_FORMAT = struct.Struct('>B')   # key = band_index @ offset 0

    def __init__(self, raw: bytes):
        self.raw = raw
        v = struct.unpack('>B ? B B x B 4x H i H 2x', raw)
        self.band_index = v[0]
        self.band_enabled = v[1]
        self.band_possible_filters = v[2]
        self.band_filter = v[3]
        self.band_freq_range = v[4]
        self.band_frequency = v[5]
        self.band_gain = v[6]
        self.band_q = v[7]

    def __repr__(self):
        on = '[on]' if self.band_enabled else '[off]'
        return (f'<atem-master-eq-band-properties band {self.band_index}'
                f'{on} freq {self.band_frequency} gain {self.band_gain} '
                f'Q {self.band_q}>')


_PLUG_NAMES = {
    0: 'Internal', 1: 'SDI', 2: 'HDMI',
    3: 'Component', 4: 'Composite', 5: 'SVideo',
    32: 'XLR', 64: 'AES', 128: 'RCA',
}


class AudioInputField(Recv):
    """``AMIP`` — input description on the legacy (pre-Fairlight) ATEM
    audio mixer. Exposed in mixerstate as ``audio-input``.

    16 bytes from ATEM 8.0 firmware on, 20 before it; the first 14 are the
    same in both (LibAtem's ``AudioMixerInputGetCommand`` and its ``V8``
    twin). Only the first 16 are read, so either length parses."""
    CODE = 'AMIP'
    PRETTY = 'audio-input'
    KEY_FORMAT = struct.Struct('>H')

    def __init__(self, raw: bytes):
        self.raw = raw
        (self.index, self.type, self.number, self.plug, self.state,
         self.volume, self.balance) = struct.unpack_from(
            '>HB 2x B x BB x Hh 2x', raw)
        self.strip_id = f'{self.index}.0'

    def plug_name(self):
        return _PLUG_NAMES.get(self.plug, 'Analog')

    def __repr__(self):
        return (f'<audio-input index={self.index} type={self.type} '
                f'plug={self.plug}>')


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


class AudioInputCommand(Send):
    """``CAMI`` — one channel strip of the legacy (pre-Fairlight) audio mixer.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (bit 0 mix option, 1 volume, 2 balance)
    2      2    u16    Source index
    4      1    u8     Mix option [0 off, 1 on, 2 AFV]
    6      2    u16    Volume [0 - 65381]
    8      2    i16    Balance [-10000 - 10000]
    ====== ==== ====== ===========
    """
    CODE = 'CAMI'
    SIZE = 12
    MASK_AT = 0

    source     = u16(at=2)
    mix_option = u8 (at=4, mask_bit=0)
    volume     = u16(at=6, mask_bit=1)
    balance    = i16(at=8, mask_bit=2)

    def __init__(self, source, balance=None, volume=None, on=None, afv=None):
        mix_option = None
        if on is not None:
            mix_option = int(bool(on))
        elif afv is not None:
            mix_option = int(bool(afv)) * 2
        super().__init__(source=source, mix_option=mix_option, volume=volume, balance=balance)


class AudioMasterPropertiesCommand(Send):
    """``CAMM`` — master channel of the legacy audio mixer.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask (bit 0 volume, bit 2 AFV)
    2      2    u16    Master volume [0 - 65381]
    6      1    bool   AFV (follow fade-to-black)
    ====== ==== ====== ===========
    """
    CODE = 'CAMM'
    SIZE = 8
    MASK_AT = 0

    volume = u16    (at=2, mask_bit=0)
    afv    = boolean(at=6, mask_bit=2)

    def __init__(self, volume=None, afv=None):
        super().__init__(volume=volume, afv=afv)


class AudioMonitorPropertiesCommand(Send):
    """``CAMm`` — monitor bus of the legacy audio mixer.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Mask
    1      1    bool   Enabled
    2      2    u16    Monitor volume [0 - 65381]
    4      1    bool   Mute
    5      1    bool   Solo
    6      2    u16    Solo source
    8      1    bool   Dim
    10     2    u16    Dim volume
    ====== ==== ====== ===========

    Mask bits: 0 enabled, 1 volume, 2 mute, 3 solo, 4 solo source, 5 dim,
               6 dim volume.
    """
    CODE = 'CAMm'
    SIZE = 12
    MASK_AT = 0

    enabled     = boolean(at=1, mask_bit=0)
    volume      = u16    (at=2, mask_bit=1)
    mute        = boolean(at=4, mask_bit=2)
    solo        = boolean(at=5, mask_bit=3)
    solo_source = u16    (at=6, mask_bit=4)
    dim         = boolean(at=8, mask_bit=5)
    dim_volume  = u16    (at=10, mask_bit=6)

    def __init__(self, enabled=None, volume=None, mute=None, solo=None, solo_source=None,
                 dim=None, dim_volume=None):
        super().__init__(enabled=enabled, volume=volume, mute=mute, solo=solo,
                         solo_source=solo_source, dim=dim, dim_volume=dim_volume)


class SendAudioLevelsCommand(Send):
    """``SALN`` — opt in to legacy audio level (meter) packets.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    bool   Enable sending levels
    1      3    ?      unknown
    ====== ==== ====== ===========
    """
    CODE = 'SALN'
    SIZE = 4

    enable = boolean(at=0)

    def __init__(self, enable):
        super().__init__(enable=enable)


def set_audio_input(conn, source, balance=None, volume=None, on=None, afv=None):
    conn.send(AudioInputCommand(source=int(source), balance=balance, volume=volume, on=on, afv=afv))


def set_audio_master(conn, volume=None, afv=None):
    conn.send(AudioMasterPropertiesCommand(volume=volume, afv=afv))


def set_audio_monitor(conn, **props):
    """Set any of enabled / volume / mute / solo / solo_source / dim / dim_volume."""
    conn.send(AudioMonitorPropertiesCommand(**props))


def enable_audio_levels(conn, enable=True):
    """Ask a legacy-audio switcher to stream level packets."""
    conn.send(SendAudioLevelsCommand(bool(enable)))


# =============================================================================
# Units
# -----------------------------------------------------------------------------
# Volume on the legacy mixer (AMIP, AMMO, AMmO and the CAMI / CAMM / CAMm
# writes) is a u16 gain: 32768 is 0 dB, 65381 the top of the fader (+6 dB),
# 0 silence (-inf). Balance is an i16, ±10000 at the ends, shown as pan ±100
# the way the Fairlight strips show theirs. The scales are LibAtem's and
# atem-connection's; no legacy switcher has checked them here.
# =============================================================================

LEGACY_VOLUME_UNITY = 32768   # 0 dB
LEGACY_VOLUME_MAX = 65381     # +6 dB


def legacy_volume_to_db(volume) -> Optional[float]:
    """Wire volume (u16) → dB, two places: ``20 * log10(v / 32768)``.

    0 is silence and gives ``None`` (JSON ``null``), as the Fairlight
    readers give a silenced fader."""
    v = safe_int(volume, 0)
    if v <= 0:
        return None
    return round(20 * math.log10(v / LEGACY_VOLUME_UNITY), 2)


def legacy_db_to_volume(db) -> int:
    """dB → wire volume (u16): ``floor(10 ** (dB / 20) * 32768)``, clamped
    to 0..65381. ``None``, ``-inf`` and NaN are silence (0).

    +6.0 dB gives 65380, one step under the top: 65381 is +6.00003 dB."""
    if db is None:
        return 0
    f = float(db)
    if f != f or f == float('-inf'):
        return 0
    v = math.floor(10 ** (min(f, 20.0) / 20) * LEGACY_VOLUME_UNITY)
    return max(0, min(LEGACY_VOLUME_MAX, v))


def legacy_pan_to_balance(pan) -> int:
    """Pan ±100 (as the strips show it) → wire balance, clamped to ±10000."""
    return max(-10000, min(10000, int(round(float(pan) * 100))))


# =============================================================================
# Readers
# -----------------------------------------------------------------------------
# The legacy counterparts of fairlight.py's readers, in the same shapes so
# build_full_state's audio section looks the same on either mixer. What the
# legacy mixer doesn't have (input gain, EQ, dynamics, delay) is left out
# rather than filled with zeros.
# =============================================================================

_LEGACY_MIX_OPTION_NAMES = {0: 'Off', 1: 'On', 2: 'AudioFollowVideo'}

# External audio inputs have no input-properties entry to name them by.
# Source ids from LibAtem's AudioSource; media players are 2001-2004.
_LEGACY_EXTERNAL_AUDIO_NAMES = {
    1001: ('XLR', 'XLR'),
    1101: ('AES', 'AES/EBU'),
    1201: ('RCA', 'RCA'),
    1301: ('Mic1', 'Mic 1'),
    1302: ('Mic2', 'Mic 2'),
}


def audio_mixer_type(mx) -> str:
    """Which audio mixer the switcher has, from what it has sent:
    ``'fairlight'`` once ``FAMP`` has arrived, ``'legacy'`` once ``AMMO``
    has, ``'unknown'`` before either (and on a switcher that sends
    neither)."""
    if mx.get('fairlight-master-properties') is not None:
        return 'fairlight'
    if mx.get('audio-mixer-master-properties') is not None:
        return 'legacy'
    return 'unknown'


def legacy_audio_master(mx) -> dict:
    """Read AMMO into ``{present, volume_db, afv}``: ``fairlight_master``'s
    keys without the EQ and dynamics ones."""
    node = mx.get('audio-mixer-master-properties')
    if node is None:
        return {'present': False, 'volume_db': 0.0, 'afv': False}
    return {
        'present': True,
        'volume_db': legacy_volume_to_db(getattr(node, 'volume', 0)),
        'afv': safe_bool(getattr(node, 'afv', False), False),
    }


def legacy_audio_strips(mx) -> list:
    """One strip per AMIP source, shaped like ``fairlight_strips``' entries.

    Every legacy strip is stereo: ``channel`` -1, ``strip_id``
    ``"<source>.0"``. ``mix_type`` takes the Fairlight names (``'Off'``,
    ``'On'``, ``'AudioFollowVideo'``); ``mix_state`` is the legacy wire
    value (0, 1, 2), not Fairlight's bitfield. ``input_type`` is AMIP's
    type, which means what FAIP's does (0 video input, 1 media player,
    2 external audio); ``plug`` is ``AudioInputField.plug_name()``.

    Names as ``fairlight_strips`` gives them: a video input's from
    input-properties, ``MP<n>`` for a media player, and an external audio
    input's from its source id. Ordered video inputs, media players,
    external audio, each by source.
    """
    nodes = mx.get('audio-input', {}) or {}
    inputs = mx.get('input-properties', {}) or {}
    strips = []
    for node in nodes.values():
        source = safe_int(getattr(node, 'index', 0), 0)
        input_type = safe_int(getattr(node, 'type', 0), 0)
        state = safe_int(getattr(node, 'state', 0), 0)
        if input_type == 1 and 2001 <= source <= 2099:
            short, long = f'MP{source - 2000}', f'Media Player {source - 2000}'
        elif input_type == 2 and source in _LEGACY_EXTERNAL_AUDIO_NAMES:
            short, long = _LEGACY_EXTERNAL_AUDIO_NAMES[source]
        else:
            ip = inputs.get(source)
            ip_short = decode_name(getattr(ip, 'short_name', b''), '') if ip else ''
            ip_long = decode_name(getattr(ip, 'name', b''), '') if ip else ''
            short = ip_short or ip_long or f'Src{source}'
            long = ip_long or short
        strips.append({
            'source': source,
            'channel': -1,
            'subchannel': 0,
            'split': False,
            'strip_id': f'{source}.0',
            'name': long,
            'short_name': short,
            'mix_type': _LEGACY_MIX_OPTION_NAMES.get(state, str(state)),
            'mix_state': state,
            'volume_db': legacy_volume_to_db(getattr(node, 'volume', 0)),
            'pan': round(safe_int(getattr(node, 'balance', 0), 0) / 100.0, 2),
            'input_type': input_type,
            'plug': _PLUG_NAMES.get(safe_int(getattr(node, 'plug', 0), 0), 'Analog'),
        })
    strips.sort(key=lambda s: (s['input_type'], s['source']))
    return strips


def legacy_audio_headphones(mx) -> dict:
    """Read the monitor bus (AMmO) where ``fairlight_headphones`` reads
    FMHP: ``present``, ``volume_db``, ``unmuted``, then the monitor's own
    ``enabled``, ``dim``, ``dim_level``, ``solo`` and ``solo_source``.

    ``dim_level`` is the wire value / 100: LibAtem reads it as a level
    0-100.00, atem-connection passes it raw. Neither treats it as a gain,
    so it is not given in dB."""
    node = mx.get('audio-mixer-monitor-properties')
    if node is None:
        return {'present': False, 'volume_db': 0.0, 'unmuted': True,
                'enabled': False, 'dim': False, 'dim_level': 0.0,
                'solo': False, 'solo_source': None}
    return {
        'present': True,
        'volume_db': legacy_volume_to_db(getattr(node, 'volume', 0)),
        'unmuted': not safe_bool(getattr(node, 'mute', False), False),
        'enabled': safe_bool(getattr(node, 'enabled', False), False),
        'dim': safe_bool(getattr(node, 'dim', False), False),
        'dim_level': round(safe_int(getattr(node, 'dim_volume', 0), 0) / 100.0, 2),
        'solo': safe_bool(getattr(node, 'solo', False), False),
        'solo_source': safe_int(getattr(node, 'solo_source', 0), 0),
    }


def legacy_audio_solo(mx) -> dict:
    """Read AMmO's solo into ``fairlight_solo``'s shape:
    ``{any_soloed, strip_id}``, the strip id ``"<solo_source>.0"``."""
    node = mx.get('audio-mixer-monitor-properties')
    if node is None or not safe_bool(getattr(node, 'solo', False), False):
        return {'any_soloed': False, 'strip_id': None}
    return {'any_soloed': True,
            'strip_id': f"{safe_int(getattr(node, 'solo_source', 0), 0)}.0"}


# =============================================================================
# Diagnostics
# -----------------------------------------------------------------------------
# No legacy switcher has been on the wire here, so AtemProtocol logs the first
# AMIP per source and the first AMMO, AMmO and AMLv a connection receives, at
# INFO, raw and decoded, prefixed "legacy-audio:", and the mixer it found at
# each connect. A log from a legacy unit is then enough to confirm the layouts
# above or correct them. Only legacy switchers send these four packets, so a
# Fairlight one logs none of them (its mixer line is DEBUG).
# =============================================================================

LEGACY_AUDIO_DIAGNOSTIC_KEYS = frozenset({
    'audio-input', 'audio-mixer-master-properties',
    'audio-mixer-monitor-properties', 'audio-meter-levels',
})


def _diagnostic_value(value):
    if isinstance(value, float):
        return round(value, 1)
    if isinstance(value, tuple):
        return tuple(_diagnostic_value(v) for v in value)
    if isinstance(value, dict):
        return {k: _diagnostic_value(v) for k, v in value.items()}
    return value


def legacy_audio_diagnostic(contents) -> str:
    """One line for the log: the code, the source for AMIP, the payload in
    hex and every decoded attribute, plus dB for a volume and the plug's
    name. AMLv levels are rounded to 0.1 dB."""
    values = {k: _diagnostic_value(v) for k, v in vars(contents).items()
              if k not in ('raw', 'strip_id')}
    if 'volume' in values:
        values['volume_db'] = legacy_volume_to_db(values['volume'])
    if 'plug' in values:
        values['plug_name'] = _PLUG_NAMES.get(values['plug'], 'Analog')
    raw = bytes(getattr(contents, 'raw', b''))
    where = f' source {contents.index}' if type(contents).CODE == 'AMIP' else ''
    decoded = ' '.join(f'{k}={v}' for k, v in values.items())
    return f'{type(contents).CODE}{where}, {len(raw)} bytes: {raw.hex()} -> {decoded}'
