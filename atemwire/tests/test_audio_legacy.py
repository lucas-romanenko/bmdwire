# SPDX-License-Identifier: LGPL-3.0-only
"""The legacy (pre-Fairlight) audio mixer: ``AMIP``, ``AMMO``, ``AMmO``,
``AMLv``.

Nothing here comes from a switcher. There is no capture from a legacy unit
in this repository; the payloads are built from the layouts in
``messages/audio_legacy.py`` and ``messages/meters.py``, which agree with
LibAtem's and atem-connection's. A hardware report from a Production Studio
4K or a Television Studio HD is still the only real check.

No hardware: ``AtemProtocol(ip=...)`` creates an unbound UDP socket but
starts no thread and sends nothing; ``save_field_data`` is driven directly.
"""
import struct
from types import SimpleNamespace

import pytest

from atemwire.messages.audio_legacy import (
    audio_mixer_type, legacy_db_to_volume, legacy_pan_to_balance,
    legacy_volume_to_db,
)
from atemwire.protocol import AtemProtocol
from atemwire.state import build_full_state

_AMIP_FMT = '>HB 2x B x BB x Hh 2x'     # 16 bytes, ATEM 8.0 firmware on


def _amip(index, *, type=0, number=0, plug=1, state=1, volume=32768,
          balance=0, pad=0):
    return struct.pack(_AMIP_FMT, index, type, number, plug, state,
                       volume, balance) + bytes(pad)


@pytest.fixture
def proto():
    p = AtemProtocol('127.0.0.1')
    p.send_commands = lambda commands: None     # nothing reaches the wire
    try:
        yield p
    finally:
        p.transport.sock.close()
        p.transport.thread_queue.close()


@pytest.mark.parametrize('pad', [0, 4], ids=['16 bytes (8.0 on)', '20 bytes (before 8.0)'])
def test_amip_parses_either_length(proto, pad):
    """Before 8.0 firmware AMIP is 20 bytes. ``struct.unpack`` wanted exactly
    16, raised out of ``loop()`` and ended the worker: the whole connection,
    not just the audio panel."""
    proto.save_field_data(b'AMIP', _amip(3, plug=2, state=2, volume=65381,
                                         balance=-10000, pad=pad))
    node = proto.mixerstate['audio-input']['3.0']
    assert (node.index, node.type, node.plug, node.state, node.volume,
            node.balance) == (3, 0, 2, 2, 65381, -10000)
    assert node.plug_name() == 'HDMI'


# --- Units -------------------------------------------------------------------

@pytest.mark.parametrize('wire, db', [
    (0, None),            # silence: -inf, carried as null
    (32768, 0.0),         # unity
    (65381, 6.0),         # the top of the fader (+6.00003 dB)
])
def test_volume_to_db_anchors(wire, db):
    assert legacy_volume_to_db(wire) == db


@pytest.mark.parametrize('db, wire', [
    (None, 0),
    (float('-inf'), 0),
    (float('nan'), 0),
    (0.0, 32768),
    # floor(10 ** 0.3 * 32768) = floor(65380.78): one under the top, which
    # is +6.00003 dB. Anything louder clamps to the top.
    (6.0, 65380),
    (6.01, 65381),
    (10.0, 65381),
    (float('inf'), 65381),
    (-100.0, 0),          # 0.33 floors to silence
])
def test_db_to_volume(db, wire):
    assert legacy_db_to_volume(db) == wire


def test_volume_round_trips_through_db_within_a_step():
    for wire in (1, 100, 4096, 16384, 32767, 32768, 50000, 65381):
        back = legacy_db_to_volume(legacy_volume_to_db(wire))
        # Two decimal places of dB is about 0.12 % of the value.
        assert abs(back - wire) <= max(1, wire * 0.0013), wire


@pytest.mark.parametrize('pan, balance', [
    (0, 0), (100, 10000), (-100, -10000), (12.34, 1234), (250, 10000), (-250, -10000),
])
def test_pan_to_balance(pan, balance):
    assert legacy_pan_to_balance(pan) == balance


# --- Which mixer -------------------------------------------------------------

@pytest.mark.parametrize('keys, mixer', [
    ((), 'unknown'),
    (('audio-mixer-master-properties',), 'legacy'),
    (('fairlight-master-properties',), 'fairlight'),
    (('fairlight-master-properties', 'audio-mixer-master-properties'), 'fairlight'),
    (('audio-input', 'audio-mixer-monitor-properties'), 'unknown'),   # AMMO decides
])
def test_audio_mixer_type(keys, mixer):
    assert audio_mixer_type({k: SimpleNamespace() for k in keys}) == mixer


# --- build_full_state on a legacy mixer --------------------------------------

def _legacy_mx():
    """A Production Studio 4K-shaped legacy mixer: two cameras (one renamed),
    a media player, the XLR input; AFV master, monitor soloing camera 2."""
    def ip(name, short):
        return SimpleNamespace(name=name, short_name=short)

    def amip(index, type, plug, state, volume, balance):
        return SimpleNamespace(index=index, type=type, number=0, plug=plug,
                               state=state, volume=volume, balance=balance,
                               strip_id=f'{index}.0')

    return {
        'video-mode': SimpleNamespace(mode=12, rate=50, get_label=lambda: '1080p50'),
        'input-properties': {
            1: ip('Camera 1', 'CAM1'),
            2: ip('Pulpit', 'PULP'),
            2001: ip('Color 1', 'COL1'),     # the video-side id: not MP1's name
        },
        'audio-mixer-master-properties': SimpleNamespace(volume=32768, afv=True),
        'audio-mixer-monitor-properties': SimpleNamespace(
            enabled=True, volume=16384, mute=True, solo=True, solo_source=2,
            dim=True, dim_volume=5000),
        'audio-input': {
            # Out of order on purpose: strips sort by (type, source).
            '1001.0': amip(1001, 2, 32, 1, 65381, 0),
            '2001.0': amip(2001, 1, 0, 0, 0, 0),
            '2.0': amip(2, 0, 2, 2, 23197, 5000),
            '1.0': amip(1, 0, 1, 1, 32768, -10000),
        },
    }


def test_legacy_build_full_state_shape():
    audio = build_full_state(SimpleNamespace(mixerstate=_legacy_mx()))['audio']
    assert audio['mixer'] == 'legacy'
    assert audio['present'] is True
    assert audio['master'] == {'present': True, 'volume_db': 0.0, 'afv': True}
    assert audio['headphones'] == {
        'present': True, 'volume_db': -6.02, 'unmuted': False, 'enabled': True,
        'dim': True, 'dim_level': 50.0, 'solo': True, 'solo_source': 2,
    }
    assert audio['solo'] == {'any_soloed': True, 'strip_id': '2.0'}
    assert [s['strip_id'] for s in audio['strips']] == ['1.0', '2.0', '2001.0', '1001.0']
    cam1, cam2, mp1, xlr = audio['strips']
    assert cam1 == {
        'source': 1, 'channel': -1, 'subchannel': 0, 'split': False,
        'strip_id': '1.0', 'name': 'Camera 1', 'short_name': 'CAM1',
        'mix_type': 'On', 'mix_state': 1, 'volume_db': 0.0, 'pan': -100.0,
        'input_type': 0, 'plug': 'SDI', 'soloed': False,
    }
    assert (cam2['name'], cam2['short_name'], cam2['mix_type'], cam2['mix_state'],
            cam2['volume_db'], cam2['pan'], cam2['plug'], cam2['soloed']) == \
        ('Pulpit', 'PULP', 'AudioFollowVideo', 2, -3.0, 50.0, 'HDMI', True)
    assert (mp1['name'], mp1['short_name'], mp1['mix_type'], mp1['volume_db'],
            mp1['input_type']) == ('Media Player 1', 'MP1', 'Off', None, 1)
    assert (xlr['name'], xlr['short_name'], xlr['volume_db'], xlr['plug'],
            xlr['input_type']) == ('XLR', 'XLR', 6.0, 'XLR', 2)


def test_legacy_strips_leave_out_what_the_mixer_lacks():
    audio = build_full_state(SimpleNamespace(mixerstate=_legacy_mx()))['audio']
    absent = {'input_gain_db', 'delay_frames', 'eq_enable', 'eq_gain_db',
              'eq_bands', 'dynamics_makeup_db', 'compressor', 'limiter',
              'expander', 'analog_level'}
    for strip in audio['strips']:
        assert not absent & strip.keys(), strip['strip_id']
    assert not {'eq_enable', 'eq_gain_db', 'eq_bands',
                'dynamics_makeup_db'} & audio['master'].keys()


def test_legacy_not_present_until_an_input_arrives():
    mx = _legacy_mx()
    del mx['audio-input']
    audio = build_full_state(SimpleNamespace(mixerstate=mx))['audio']
    assert (audio['mixer'], audio['present'], audio['strips']) == ('legacy', False, [])


def test_legacy_monitor_absent_and_nothing_soloed():
    mx = _legacy_mx()
    del mx['audio-mixer-monitor-properties']
    audio = build_full_state(SimpleNamespace(mixerstate=mx))['audio']
    assert audio['headphones']['present'] is False
    assert audio['solo'] == {'any_soloed': False, 'strip_id': None}
    assert not any(s['soloed'] for s in audio['strips'])


def test_unknown_mixer_is_the_fairlight_section_not_present():
    mx = _legacy_mx()
    for k in ('audio-mixer-master-properties', 'audio-mixer-monitor-properties',
              'audio-input'):
        del mx[k]
    audio = build_full_state(SimpleNamespace(mixerstate=mx))['audio']
    assert audio['mixer'] == 'unknown'
    assert audio['present'] is False
    assert audio['master']['present'] is False
    assert 'eq_bands' in audio['master']     # the Fairlight section's keys


def test_legacy_audio_from_the_wire(proto):
    """The same through ``save_field_data``, from payloads in the parsers'
    layouts: AMIP lands keyed by strip id, and the state reads it."""
    proto.save_field_data(b'AMMO', struct.pack('>H2x?3x', 65381, False))
    proto.save_field_data(b'AMmO', struct.pack('>?xH??H?xH', True, 32768, False,
                                               False, 0, False, 0))
    proto.save_field_data(b'AMIP', _amip(1, plug=1, state=1, volume=32768, balance=2500))
    proto.save_field_data(b'AMIP', _amip(1301, type=2, plug=32, state=0,
                                         volume=0, balance=0, pad=4))
    assert set(proto.mixerstate['audio-input']) == {'1.0', '1301.0'}
    audio = build_full_state(proto)['audio']
    assert (audio['mixer'], audio['present']) == ('legacy', True)
    assert audio['master'] == {'present': True, 'volume_db': 6.0, 'afv': False}
    assert audio['headphones']['volume_db'] == 0.0
    assert audio['headphones']['unmuted'] is True
    assert audio['solo'] == {'any_soloed': False, 'strip_id': None}
    cam, mic = audio['strips']
    assert (cam['strip_id'], cam['mix_type'], cam['volume_db'], cam['pan']) == \
        ('1.0', 'On', 0.0, 25.0)
    assert (mic['strip_id'], mic['short_name'], mic['mix_type'], mic['volume_db']) == \
        ('1301.0', 'Mic1', 'Off', None)
