# SPDX-License-Identifier: LGPL-3.0-only
"""Characterization snapshot for ``ATEMStateMixin.build_full_state``.

REFACTOR Stage 0 (see docs/history/refactor-2026-06.md). ``build_full_state`` is the one
function the control-topology refactor mutates (Stages 2-3) that sits in
the test blind spot — every other mutated atemwire path is covered by the
per-feature migration tests. This test pins the CURRENT output shape so
those stages are diffable; it asserts nothing about any desired future
shape.

Harness: mirrors the conftest ``FakeAtemProtocol`` pattern the pool /
connection / capture tests use — ``build_full_state(connection)`` is
duck-typed on ``.mixerstate`` (+ ``.last_run_macro_index``), so the fake
protocol instance itself stands in for the connection. The mixerstate
nodes are plain ``SimpleNamespace`` fakes shaped like the per-feature
migration tests' Recv fakes.

The fake topology is a representative single-M/E switcher:

  - video-mode 1080p50 (rate 50 → display fps 25, exercising the
    half-rate rule) + a 2-entry _VMC mode list
  - program/preview bus on ME0; aux 0-2 populated (3-5 pin defaults)
  - transition: settings (style + next-transition selection), position,
    and the full mix/dip/wipe/DVE/stinger detail nodes
  - USK0 with base + on-air + luma populated; chroma/pattern/mask/DVE/
    fly absent (pins their default dicts); USK1-3 fully default
  - DSK0 with state + properties (incl. mask) + base
  - FtB rate/state/enabled; color generators 0-1; macros 0 + 2 used
    (1 and 3-11 pin the "Macro N" placeholder path);
    last_run_macro_index latched to 0
  - one bound HyperDeck slot (pins the emitted-slots count fallback —
    no 'topology' field present)
  - 8 input-properties sources spanning port types 0/2/3/4/5/128/129
    (pins all_input_sources ordering + renamable_inputs bucketing,
    including the fixed-source exclusion)
  - Fairlight: master + camera strip 1.0 + media-player strip 2001.0,
    FAIP for both, one EQ band on 1.0, compressor + limiter on 1.0,
    empty expander dict, headphones, solo latched onto strip 1.0

Normalization: the state dict is round-tripped through
``json.dumps(sort_keys=True)`` so dict key order is irrelevant and int
dict keys (colorGenerators) compare in their JSON string form. The
build runs twice from independently-built fakes and must match exactly
— build_full_state reads only the mixerstate we construct, so any
mismatch is genuine non-determinism.

To regenerate after an INTENTIONAL behaviour change:

    UPDATE_BUILD_FULL_STATE_SNAPSHOT=1 pytest \
        tests/unit/test_build_full_state_characterization.py

then review the snapshot diff like code.
"""

import json
import os
from types import SimpleNamespace

from atemwire.state import build_full_state
from atemwire.testing import FakeAtemProtocol

_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'build_full_state_snapshot.json',
)
_MULTI_ME_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'build_full_state_multi_me_snapshot.json',
)


def _node(**attrs):
    return SimpleNamespace(**attrs)


class _FakeVideoMode:
    """video-mode node: VidM surface build_full_state actually reads
    (mode id + field rate + label)."""

    mode = 12
    rate = 50  # field rate 50 → display fps 25 (the >= 48 half rule)

    def get_label(self):
        return '1080p50'

    def get_resolution(self):
        return (1920, 1080)


class _FakeVMCMode:
    def __init__(self, label):
        self._label = label

    def get_label(self):
        return self._label


def _make_mixerstate():
    return {
        'video-mode': _FakeVideoMode(),
        'video-mode-capability': _node(modes=[
            {'modenum': 12, 'mode': _FakeVMCMode('1080p50')},
            {'modenum': 27, 'mode': _FakeVMCMode('1080p60')},
        ]),

        # Buses (ME0) + aux 0-2 (3-5 left absent → pin the 0 default).
        'program-bus-input': {0: _node(source=2)},
        'preview-bus-input': {0: _node(source=1)},
        'aux-output-source': {0: _node(source=3010),
                              1: _node(source=10010),
                              2: _node(source=0)},

        # Transition block — settings keyed per M/E since Stage 4A
        # (TrSS carries the M/E index at byte 0).
        'transition-settings': {0: _node(
            style=1,
            next_transition_bkgd=True, next_transition_key1=True,
            next_transition_key2=False, next_transition_key3=False,
            next_transition_key4=False)},
        'transition-position': {0: _node(in_transition=False, position=0.0,
                                         frames_remaining=0)},
        'transition-mix': {0: _node(rate=25)},
        'transition-dip': {0: _node(rate=30, source=2001)},
        'transition-wipe': {0: _node(rate=20, pattern=5, source=1000,
                                     positionx=5000, positiony=5000,
                                     softness=1000, symmetry=5000, width=500,
                                     reverse=False, flipflop=True)},
        'transition-dve': {0: _node(rate=15, fill_source=3010,
                                    key_source=3011, key_enable=True,
                                    key_clip=500, key_gain=700,
                                    key_premultiplied=False, key_invert=False,
                                    style=2, reverse=False, flipflop=True)},
        'transition-stinger': {0: _node(rate=25, mediaplayer=3010,
                                        duration=150, triggerpoint=34,
                                        preroll=48, key_clip=500,
                                        key_gain=700, key_premultiplied=True,
                                        key_invert=False)},

        # USK0 only — luma keyer; the other USK sub-dicts pin defaults.
        'key-on-air': {0: {0: _node(enabled=True)}},
        'key-properties-base': {0: {0: _node(type=0, fill_source=4,
                                             key_source=5)}},
        'key-properties-luma': {0: {0: _node(clip=220, gain=300,
                                             key_inverted=False,
                                             premultiplied=True)}},

        # DSK0.
        'dkey-state': {0: _node(on_air=True, is_transitioning=False,
                                is_autotransitioning=False,
                                frames_remaining=0)},
        'dkey-properties': {0: _node(rate=30, tie=False, premultiplied=True,
                                     clip=660, gain=825, invert_key=False,
                                     masked=True, top=9000, bottom=-9000,
                                     left=-16000, right=16000)},
        'dkey-properties-base': {0: _node(fill_source=6, key_source=7)},

        # Fade to black.
        'fade-to-black': {0: _node(rate=50)},
        'fade-to-black-state': {0: _node(done=False, transitioning=False,
                                         frames_remaining=0)},
        'fade-to-black-enabled': _node(disabled=False),

        # Color generators (saturation/luma are unit 0..1 on the node).
        'color-generator': {0: _node(hue=120.0, saturation=0.75, luma=0.5),
                            1: _node(hue=300.0, saturation=1.0, luma=0.25)},

        # Macros — slots 0 and 2 used; the gaps pin "Macro N" placeholders.
        'macro-properties': {0: _node(is_used=True, name=b'Open Show\x00\x00'),
                             2: _node(is_used=True, name=b'Half Time\x00')},

        # One bound HyperDeck slot; no 'topology' node, so _build_hyperdecks
        # falls back to the emitted-slot count (max(emitted)+1 == 1).
        'hyperdeck-settings': {0: _node(network_address='10.0.0.50', input=2,
                                        configured=True)},

        # Input properties across the port-type buckets.
        'input-properties': {
            1: _node(name=b'Camera 1\x00', short_name=b'CAM1', port_type=0,
                     available_aux=True, available_key_source=True,
                     available_multiview=True),
            2: _node(name=b'Camera 2\x00', short_name=b'CAM2', port_type=0,
                     available_aux=True, available_key_source=True,
                     available_multiview=True),
            1000: _node(name=b'Color Bars\x00', short_name=b'Bars',
                        port_type=2, available_aux=True,
                        available_key_source=True, available_multiview=True),
            2001: _node(name=b'Color 1\x00', short_name=b'COL1', port_type=3,
                        available_aux=True, available_key_source=False,
                        available_multiview=True),
            3010: _node(name=b'Media Player 1\x00', short_name=b'MP1',
                        port_type=4, available_aux=True,
                        available_key_source=True, available_multiview=True),
            3011: _node(name=b'Media Player 1 Key\x00', short_name=b'MP1K',
                        port_type=5, available_aux=False,
                        available_key_source=True, available_multiview=False),
            8001: _node(name=b'Auxiliary 1\x00', short_name=b'AUX1',
                        port_type=129, available_aux=False,
                        available_key_source=False, available_multiview=True),
            10010: _node(name=b'ME 1 Program\x00', short_name=b'PGM',
                         port_type=128, available_aux=True,
                         available_key_source=False, available_multiview=True),
        },

        # Fairlight. Wire ints are 0.01-dB / 0.01-unit scaled.
        'fairlight-master-properties': _node(volume=-300, eq_enable=True,
                                             eq_gain=150, dynamics_gain=0,
                                             afv=False),
        'fairlight-strip-properties': {
            '1.0': _node(volume=-600, gain=0, pan=0, state=2, delay=0,
                         eq_enable=False, eq_gain=0, dynamics_gain=0,
                         is_split=0x01),
            '2001.0': _node(volume=-1200, gain=0, pan=-2000, state=4, delay=2,
                            eq_enable=True, eq_gain=100, dynamics_gain=50,
                            is_split=0x01),
        },
        'fairlight-audio-input': {
            1: _node(type=0, split=0x01, level=0, number=0),
            2001: _node(type=1, split=0x01, level=0, number=0),
        },
        'atem-eq-band-properties': {
            '1.0': {0: _node(band_index=0, band_enabled=True,
                             band_filter=0x01, band_possible_filters=0x3f,
                             band_frequency=100, band_gain=300, band_q=100,
                             band_freq_range=1)},
        },
        'fairlight-compressor-properties': {
            '1.0': _node(index=1, is_split=0x01, subchannel=0, enabled=True,
                         threshold=-1000, attack=140, hold=0, release=7000,
                         ratio=200),
        },
        'fairlight-limiter-properties': {
            '1.0': _node(index=1, is_split=0x01, subchannel=0, enabled=False,
                         threshold=-800, attack=110, hold=0, release=5000),
        },
        'fairlight-expander-properties': {},
        'fairlight-headphones': _node(volume=500, unmuted=True),
        'fairlight-solo': _node(solo=True, channel=1, subchannel=0,
                                is_split_lr=0x01),
    }


def _build_state_normalized():
    """Stand up a fake-connected protocol, drive the topology through
    build_full_state, and JSON-normalize the result."""
    fake = FakeAtemProtocol('192.0.2.10')
    fake.connect()
    fake.mixerstate.update(_make_mixerstate())
    # ATEMConnection latches this from MRPr events; build_full_state
    # reads it for the lastRunMacro block.
    fake.last_run_macro_index = 0
    return json.loads(json.dumps(build_full_state(fake), sort_keys=True))


def test_build_full_state_characterization_snapshot():
    state = _build_state_normalized()

    # Determinism guard: an independently-built fake must produce the
    # exact same dict (there is nothing time- or order-dependent here).
    assert state == _build_state_normalized()

    if os.environ.get('UPDATE_BUILD_FULL_STATE_SNAPSHOT') == '1':
        with open(_SNAPSHOT_PATH, 'w') as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write('\n')

    with open(_SNAPSHOT_PATH) as f:
        expected = json.load(f)

    assert state == expected


# =============================================================================
# Multi-M/E (REFACTOR Stage 4A) — all M/Es populate from their own state
# =============================================================================
#
# No multi-M/E hardware is available locally (the bench switcher is 1 M/E), so this fake
# 2-M/E topology IS the primary gate for the multi-M/E backend until the
# Romania deployment can validate against real hardware. The fake extends
# the single-M/E topology: ME0 keeps its values; ME1 gets DISTINCT values
# on every per-M/E surface (program/preview, transition style + selection
# + rates + per-style detail, USKs, FtB) so cross-M/E bleed-through shows
# up as an assertion failure, not a snapshot diff to squint at.
#
# usksPerMe is deliberately asymmetric ([4, 2] from _MeC) so the per-M/E
# USK sizing is exercised, matching the Constellation pattern where
# secondary M/Es carry fewer keyers.


def _make_multi_me_mixerstate():
    mx = _make_mixerstate()
    mx.update({
        # _top — 2 M/E units. hyperdecks/dsks etc. set explicitly so the
        # snapshot pins capability-driven sizing rather than fallbacks.
        'topology': _node(me_units=2, sources=40, downstream_keyers=2,
                          aux_outputs=6, mixminus_outputs=0, mediaplayers=2,
                          multiviewers=2, rs485=1, hyperdecks=2, dve=1,
                          stingers=1, supersources=1,
                          multiviewer_routable=True),
        'mixer-effect-config': {0: _node(index=0, keyers=4),
                                1: _node(index=1, keyers=2)},

        # Buses — ME1 distinct from ME0 (2/1).
        'program-bus-input': {0: _node(source=2), 1: _node(source=4)},
        'preview-bus-input': {0: _node(source=1), 1: _node(source=5)},

        # Transition settings keyed per M/E — different style AND a
        # different next-transition selection per M/E.
        'transition-settings': {
            0: _node(style=1,
                     next_transition_bkgd=True, next_transition_key1=True,
                     next_transition_key2=False, next_transition_key3=False,
                     next_transition_key4=False),
            1: _node(style=2,
                     next_transition_bkgd=False, next_transition_key1=False,
                     next_transition_key2=True, next_transition_key3=False,
                     next_transition_key4=False),
        },
        'transition-position': {
            0: _node(in_transition=False, position=0.0, frames_remaining=0),
            1: _node(in_transition=True, position=0.25, frames_remaining=12),
        },
        'transition-mix': {0: _node(rate=25), 1: _node(rate=50)},
        'transition-dip': {0: _node(rate=30, source=2001),
                           1: _node(rate=60, source=2002)},
        'transition-wipe': {
            0: _node(rate=20, pattern=5, source=1000,
                     positionx=5000, positiony=5000, softness=1000,
                     symmetry=5000, width=500, reverse=False, flipflop=True),
            1: _node(rate=40, pattern=10, source=2001,
                     positionx=2500, positiony=7500, softness=2000,
                     symmetry=2500, width=1000, reverse=True, flipflop=False),
        },
        'transition-dve': {
            0: _node(rate=15, fill_source=3010, key_source=3011,
                     key_enable=True, key_clip=500, key_gain=700,
                     key_premultiplied=False, key_invert=False,
                     style=2, reverse=False, flipflop=True),
            1: _node(rate=35, fill_source=1, key_source=2,
                     key_enable=False, key_clip=250, key_gain=900,
                     key_premultiplied=True, key_invert=True,
                     style=5, reverse=True, flipflop=False),
        },
        'transition-stinger': {
            0: _node(rate=25, mediaplayer=3010, duration=150,
                     triggerpoint=34, preroll=48, key_clip=500,
                     key_gain=700, key_premultiplied=True, key_invert=False),
            1: _node(rate=75, mediaplayer=3020, duration=300,
                     triggerpoint=68, preroll=24, key_clip=250,
                     key_gain=350, key_premultiplied=False, key_invert=True),
        },

        # USKs — ME0 keeps USK0 luma from the base fake; ME1's two keyers
        # get distinct base + on-air values (keyer 1 on-air, unlike ME0
        # where keyer 0 is on-air).
        'key-on-air': {0: {0: _node(enabled=True)},
                       1: {1: _node(enabled=True)}},
        'key-properties-base': {
            0: {0: _node(type=0, fill_source=4, key_source=5)},
            1: {0: _node(type=2, fill_source=7, key_source=8),
                1: _node(type=3, fill_source=3020, key_source=3021)},
        },
        'key-properties-luma': {
            0: {0: _node(clip=220, gain=300, key_inverted=False,
                         premultiplied=True)},
            1: {0: _node(clip=440, gain=600, key_inverted=True,
                         premultiplied=False)},
        },

        # Fade to black — distinct rates; ME1 mid-fade.
        'fade-to-black': {0: _node(rate=50), 1: _node(rate=100)},
        'fade-to-black-state': {
            0: _node(done=False, transitioning=False, frames_remaining=0),
            1: _node(done=False, transitioning=True, frames_remaining=37),
        },
    })
    return mx


def _build_multi_me_state_normalized():
    fake = FakeAtemProtocol('192.0.2.10')
    fake.connect()
    fake.mixerstate.update(_make_multi_me_mixerstate())
    fake.last_run_macro_index = 0
    return json.loads(json.dumps(build_full_state(fake), sort_keys=True))


def test_build_full_state_multi_me_characterization_snapshot():
    state = _build_multi_me_state_normalized()

    # Determinism guard, same as the single-M/E snapshot.
    assert state == _build_multi_me_state_normalized()

    if os.environ.get('UPDATE_BUILD_FULL_STATE_SNAPSHOT') == '1':
        with open(_MULTI_ME_SNAPSHOT_PATH, 'w') as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write('\n')

    with open(_MULTI_ME_SNAPSHOT_PATH) as f:
        expected = json.load(f)

    assert state == expected


def test_multi_me_values_are_distinct_and_correctly_placed():
    """Each M/E's values land in its own mes[] entry — no cross-M/E
    bleed-through, no last-write-wins on the formerly-bare TrSS."""
    state = _build_multi_me_state_normalized()
    mes = state['mes']

    assert len(mes) == 2
    assert state['topology']['meCount'] == 2
    assert state['topology']['usksPerMe'] == [4, 2]

    # Buses.
    assert mes[0]['program'] == 2 and mes[0]['preview'] == 1
    assert mes[1]['program'] == 4 and mes[1]['preview'] == 5

    # Transition style + selection (per-M/E TrSS).
    assert mes[0]['transition']['style'] == 1
    assert mes[1]['transition']['style'] == 2
    assert mes[0]['transition']['selection'] == {
        'background': True, 'key1': True, 'key2': False,
        'key3': False, 'key4': False}
    assert mes[1]['transition']['selection'] == {
        'background': False, 'key1': False, 'key2': True,
        'key3': False, 'key4': False}

    # The active-style rate resolves per M/E: ME0 dip (30 frames @25fps
    # -> "1:05"), ME1 wipe (40 frames -> "1:15").
    assert mes[0]['transition']['rate'] == '1:05'
    assert mes[1]['transition']['rate'] == '1:15'

    # Transition position — only ME1 is mid-transition.
    assert mes[0]['transition']['in_transition'] is False
    assert mes[1]['transition']['in_transition'] is True
    assert mes[1]['transition']['frames_remaining'] == 12

    # Per-style detail follows the M/E.
    assert mes[0]['transition']['wipe_pattern'] == 5
    assert mes[1]['transition']['wipe_pattern'] == 10
    assert mes[0]['transition']['stinger']['source'] == 3010
    assert mes[1]['transition']['stinger']['source'] == 3020
    assert mes[0]['transition']['dve']['fill_source'] == 3010
    assert mes[1]['transition']['dve']['fill_source'] == 1

    # USKs sized per M/E (_MeC keyers: 4 vs 2), values per M/E.
    assert len(mes[0]['usk']['data']) == 4
    assert len(mes[1]['usk']['data']) == 2
    assert mes[0]['usk']['states'] == [True, False, False, False]
    assert mes[1]['usk']['states'] == [False, True]
    assert mes[0]['usk']['data'][0]['fill_source'] == 4
    assert mes[1]['usk']['data'][0]['fill_source'] == 7
    assert mes[1]['usk']['data'][1]['fill_source'] == 3020
    assert mes[0]['usk']['data'][0]['luma_clip'] == 22.0
    assert mes[1]['usk']['data'][0]['luma_clip'] == 44.0

    # Fade to black.
    assert mes[0]['ftb']['rate_str'] == '2:00'
    assert mes[1]['ftb']['rate_str'] == '4:00'
    assert mes[0]['ftb']['in_transition'] is False
    assert mes[1]['ftb']['in_transition'] is True


def test_single_me_still_yields_one_entry():
    """The single-M/E fake (no _top, no _MeC) must keep producing a
    1-element mes array — the single-M/E no-regression contract."""
    state = _build_state_normalized()
    assert len(state['mes']) == 1
    assert len(state['mes'][0]['usk']['data']) == 4


# =============================================================================
# Topology (REFACTOR Stage 2) — each count traces to its wire field
# =============================================================================
#
# The snapshot above pins the no-capability-packets shape (the Stage 0
# fake deliberately has no 'topology' node — it pins _build_hyperdecks'
# fallback). These tests add the capability packets and assert each
# topology count traces to the wire field that carries it. _FAC / _MAC /
# _MvC have no parser in atemwire (raw bytes only), so those counts derive
# from the per-unit indexed packets (FASP / MPrp / MvIn), and the current
# _top layout has no color generator byte, so that count derives from the
# per-unit ColV packets.


def test_topology_counts_trace_to_wire_fields():
    fake = FakeAtemProtocol('192.0.2.10')
    fake.connect()
    fake.last_run_macro_index = 0
    fake.mixerstate.update(_make_mixerstate())
    fake.mixerstate.update({
        # _top (TopologyField)
        'topology': _node(me_units=2, sources=40, downstream_keyers=4,
                          aux_outputs=6, mixminus_outputs=0, mediaplayers=4,
                          multiviewers=2, rs485=1, hyperdecks=4, dve=1,
                          stingers=1, supersources=1,
                          multiviewer_routable=True),
        # _MeC (MixerEffectConfigField, one per M/E)
        'mixer-effect-config': {0: _node(index=0, keyers=4),
                                1: _node(index=1, keyers=2)},
        # _mpl (MediaplayerSlotsField)
        'mediaplayer-slots': _node(stills=20, clips=2),
        # MvIn (multiviewer-input, keyed (mv, window))
        'multiviewer-input': {0: {w: _node(source=0) for w in range(10)}},
    })

    topo = build_full_state(fake)['topology']

    assert topo == {
        'meCount': 2,            # _top.me_units
        'usksPerMe': [4, 2],     # _MeC[i].keyers
        'dsks': 4,               # _top.downstream_keyers
        'auxOutputs': 6,         # _top.aux_outputs
        'inputs': 2,             # InPr entries with port_type 0 (1, 2)
        'outputs': 1,            # InPr entries with port_type 129 (8001)
        'mediaPlayers': 4,       # _top.mediaplayers
        'mediaPoolStills': 20,   # _mpl.stills
        'mediaPoolClips': 2,     # _mpl.clips
        'colorGenerators': 2,    # ColV entries (max index + 1)
        'multiviewWindows': 10,  # MvIn windows on MV 0 (max index + 1)
        'fairlightStrips': 2,    # FASP entry count ('1.0' + '2001.0')
        'macros': 3,             # MPrp slots (max index 2 + 1)
    }


def test_topology_handshake_defaults_and_mec_fallback():
    # Before any capability packet arrives, every count is zero/empty.
    fake = FakeAtemProtocol('192.0.2.10')
    fake.connect()
    fake.mixerstate.update({'video-mode': _FakeVideoMode()})
    topo = build_full_state(fake)['topology']
    assert topo == {
        'meCount': 0, 'usksPerMe': [], 'dsks': 0, 'auxOutputs': 0,
        'inputs': 0, 'outputs': 0,
        'mediaPlayers': 0, 'mediaPoolStills': 0, 'mediaPoolClips': 0,
        'colorGenerators': 0, 'multiviewWindows': 0, 'fairlightStrips': 0,
        'macros': 0,
    }

    # _MeC arriving before _top still yields a usable meCount/usksPerMe.
    fake.mixerstate['mixer-effect-config'] = {0: _node(index=0, keyers=4)}
    topo = build_full_state(fake)['topology']
    assert topo['meCount'] == 1
    assert topo['usksPerMe'] == [4]
