# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Legacy audio mixer messages (pre-Fairlight, smaller ATEMs).

Wire packets (incoming only):
    AMMO — master bus properties (volume + AFV)
    AMmO — monitor bus properties (volume / mute / solo / dim)
    AMIP — per-input channel-strip properties
    AMTl — audio mixer tally state (variable-length)
"""

import struct

from atemwire.messages._dsl import Recv, boolean, u16


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


class AudioInputField(Recv):
    """``AMIP`` — input description on the legacy (pre-Fairlight) ATEM
    audio mixer. Exposed in mixerstate as ``audio-input``."""
    CODE = 'AMIP'
    PRETTY = 'audio-input'
    KEY_FORMAT = struct.Struct('>H')

    def __init__(self, raw: bytes):
        self.raw = raw
        (self.index, self.type, self.number, self.plug, self.state,
         self.volume, self.balance) = struct.unpack(
            '>HB 2x B x BB x Hh 2x', raw)
        self.strip_id = f'{self.index}.0'

    def plug_name(self):
        lut = {
            0: 'Internal', 1: 'SDI', 2: 'HDMI',
            3: 'Component', 4: 'Composite', 5: 'SVideo',
            32: 'XLR', 64: 'AES', 128: 'RCA',
        }
        return lut.get(self.plug, 'Analog')

    def __repr__(self):
        return (f'<audio-input index={self.index} type={self.type} '
                f'plug={self.plug}>')
