# SPDX-License-Identifier: LGPL-3.0-only
"""Apply-path tests for the profile-restore wiring added 2026-06.

Each test feeds synthetic profile XML (in ASC's attribute format) to the
relevant ``_apply_*`` function and asserts the expected wire op fired with
the value correctly converted from ASC units.

The fake connection records Command OBJECTS rather than bytes: the DSL
``Send.__init__`` does ``setattr(self, field, value)`` for every field, so
assertions read ``cmd.<field>`` directly (no byte/mask decoding). Fairlight
dynamics commands set the same attributes via their explicit ``__init__``.

No hardware; runs in-container against the built atemwire like the other
unit tests.
"""

import xml.etree.ElementTree as ET

from atemwire.profile.apply import (
    _apply_downstream_keys,
    _apply_fairlight,
    _apply_upstream_keyers,
)
from atemwire.profile.options import ApplyOptions, ApplyResult


class _RecConn:
    """Fake connection recording the Command objects sent."""

    def __init__(self, mixerstate=None):
        self.sent = []
        self.mixerstate = mixerstate or {}

    def send(self, cmd):
        self.sent.append(cmd)

    def code(self, code):
        return [c for c in self.sent if getattr(c, 'CODE', None) == code]


def _usk_root(key_xml):
    return ET.fromstring(
        '<Profile><MixEffectBlocks><MixEffectBlock index="0"><Keys>'
        f'{key_xml}'
        '</Keys></MixEffectBlock></MixEffectBlocks></Profile>')


def _usk_opts():
    opts = ApplyOptions()
    opts.mes[0].usk = [True, True, True, True]
    return opts


# --------------------------------------------------------------------------
# Chroma colourpicker cursor — size / position / sampled YCbCr (CACC)
# --------------------------------------------------------------------------

def test_apply_chroma_cursor_size_position_sampled():
    root = _usk_root(
        '<Key index="0" type="Chroma">'
        '<AdvancedChromaParameters cursorSize="6.25" cursorXPosition="8.0"'
        ' cursorYPosition="-4.5" sampledY="0.6748" sampledCb="0.1621"'
        ' sampledCr="0.1015"/></Key>')
    conn = _RecConn()
    _apply_upstream_keyers(conn, root, ApplyResult(), _usk_opts())

    caccs = conn.code('CACC')
    # Three disjoint CACC packets — identify each by its set field.
    size_cmd = next(c for c in caccs if c.size is not None)
    pos_cmd = next(c for c in caccs if c.x is not None)
    col_cmd = next(c for c in caccs if c.Y is not None)

    # cursorSize 6.25 → unit 0.0625 → wire round(0.0625*10000) = 625
    assert size_cmd.size == 625
    # cursorX 8.0 → unit 8/32 + 0.5 = 0.75 → round((0.75-0.5)*32000) = 8000
    assert pos_cmd.x == 8000
    # cursorY -4.5 → unit -4.5/18 + 0.5 = 0.25 → round((0.25-0.5)*18000) = -4500
    assert pos_cmd.y == -4500
    # sampled* × 10000 (inverse of save's *_raw / 10000)
    assert (col_cmd.Y, col_cmd.Cb, col_cmd.Cr) == (6748, 1621, 1015)


# --------------------------------------------------------------------------
# Fly-key geometry — position / size / rotation / rate (CKDV)
# --------------------------------------------------------------------------

def test_apply_fly_geometry():
    root = _usk_root(
        '<Key index="0" type="DVE">'
        '<FlyParameters enabled="True" xPosition="4.0" yPosition="-2.0"'
        ' xSize="1.5" ySize="0.5" rotation="30" rate="20"/></Key>')
    conn = _RecConn()
    _apply_upstream_keyers(conn, root, ApplyResult(), _usk_opts())

    ckdvs = conn.code('CKDV')

    def one(name):
        return next(getattr(c, name) for c in ckdvs
                    if getattr(c, name, None) is not None)

    assert one('pos_x') == 4000        # 4.0 × 1000
    assert one('pos_y') == -2000       # -2.0 × 1000
    assert one('size_x') == 1500       # size_thousandths(1.5)
    assert one('size_y') == 500        # size_thousandths(0.5)
    assert one('rotation') == 300      # 30 × 10
    # rate is resolved through _resolve_rate; confirm it was wired at all.
    assert any(getattr(c, 'rate', None) is not None for c in ckdvs)


def test_apply_fly_enable_only_when_no_geometry():
    # enable present, no geometry attrs -> no CKDV geometry packets.
    root = _usk_root('<Key index="0" type="DVE">'
                     '<FlyParameters enabled="True"/></Key>')
    conn = _RecConn()
    _apply_upstream_keyers(conn, root, ApplyResult(), _usk_opts())
    geo = [c for c in conn.code('CKDV')
           if any(getattr(c, n, None) is not None
                  for n in ('pos_x', 'pos_y', 'size_x', 'size_y', 'rotation'))]
    assert geo == []


# --------------------------------------------------------------------------
# Per-strip dynamics — Compressor / Limiter / Expander (CICP / CILP / CIXP)
# --------------------------------------------------------------------------

def test_apply_strip_dynamics_compressor_limiter_expander():
    root = ET.fromstring(
        '<Profile><FairlightAudioMixer><AudioInputs>'
        '<AudioInput id="1"><AudioSource id="-65280">'
        '<DynamicsProcessor makeupGain="3">'
        '<Compressor enabled="True" threshold="-30" ratio="2.5" attack="2"'
        ' hold="10" release="100"/>'
        '<Limiter enabled="True" threshold="-6" attack="1" hold="5"'
        ' release="80"/>'
        '<Expander enabled="True" gateMode="True" threshold="-40" range="20"'
        ' ratio="2" attack="3" hold="0" release="90"/>'
        '</DynamicsProcessor></AudioSource></AudioInput>'
        '</AudioInputs></FairlightAudioMixer></Profile>')
    conn = _RecConn()
    _apply_fairlight(conn, root, ApplyResult())

    comp = conn.code('CICP')[0]
    assert (comp.source, comp.channel, comp.enabled) == (1, -1, True)
    assert (comp.threshold, comp.ratio) == (-30.0, 2.5)
    assert (comp.attack, comp.hold, comp.release) == (2.0, 10.0, 100.0)

    lim = conn.code('CILP')[0]
    assert (lim.source, lim.channel, lim.enabled) == (1, -1, True)
    assert (lim.threshold, lim.attack, lim.hold, lim.release) == (
        -6.0, 1.0, 5.0, 80.0)

    exp = conn.code('CIXP')[0]
    assert (exp.source, exp.channel, exp.enabled) == (1, -1, True)
    assert exp.mode == 'gate'                       # gateMode="True"
    assert (exp.threshold, exp.range, exp.ratio) == (-40.0, 20.0, 2.0)
    assert (exp.attack, exp.hold, exp.release) == (3.0, 0.0, 90.0)


def test_apply_master_compressor_and_limiter():
    """Master <MasterOutDynamicsProcessor> Compressor/Limiter -> CMCP/CMLP,
    the master-bus Send commands (no source/channel). Replaces the
    formerly-gated note_skipped stub; per-strip CICP/CILP path untouched."""
    root = ET.fromstring(
        '<Profile><FairlightAudioMixer>'
        '<MasterOutDynamicsProcessor makeupGain="0">'
        '<Compressor enabled="True" threshold="-23.5" ratio="6" attack="12"'
        ' hold="7" release="250"/>'
        '<Limiter enabled="True" threshold="-1" attack="2.2" hold="5"'
        ' release="100"/>'
        '</MasterOutDynamicsProcessor>'
        '</FairlightAudioMixer></Profile>')
    conn = _RecConn()
    _apply_fairlight(conn, root, ApplyResult())

    comp = conn.code('CMCP')
    assert len(comp) == 1
    c = comp[0]
    assert (c.enabled, c.threshold, c.ratio) == (True, -23.5, 6.0)
    assert (c.attack, c.hold, c.release) == (12.0, 7.0, 250.0)
    # Master command carries NO source/channel addressing (vs per-strip).
    assert not hasattr(c, 'source')

    lim = conn.code('CMLP')
    assert len(lim) == 1
    m = lim[0]
    assert (m.enabled, m.threshold) == (True, -1.0)
    assert (m.attack, m.hold, m.release) == (2.2, 5.0, 100.0)
    assert not hasattr(m, 'source')
    # The bogus per-strip dynamics on the master (source=0) must NOT fire.
    assert conn.code('CICP') == []
    assert conn.code('CILP') == []


def test_apply_master_eq_enable_gain_and_bands():
    """Master <MasterOutEqualizer> -> CFMP (enable@1 + gain) + one CMBP per
    band (band index, no source/channel). Replaces the broken CEBP source=0
    path; the master bands must NOT go out as CEBP."""
    root = ET.fromstring(
        '<Profile><FairlightAudioMixer masterOutFaderGain="0">'
        '<MasterOutEqualizer enabled="True" gain="-5.96">'
        '<EqualizerBand index="0" enabled="False" shape="HighPass"'
        ' frequencyRange="Low" frequency="46" gain="0" qFactor="0.71"/>'
        '<EqualizerBand index="2" enabled="True" shape="BandPass"'
        ' frequencyRange="MidLow" frequency="150" gain="20" qFactor="2.3"/>'
        '</MasterOutEqualizer>'
        '</FairlightAudioMixer></Profile>')
    conn = _RecConn()
    _apply_fairlight(conn, root, ApplyResult())

    # Master EQ enable + gain go out as SEPARATE single-field CFMP packets
    # (a combined-mask gain doesn't land on hardware), enable BEFORE gain.
    cfmp = conn.code('CFMP')
    en = [c for c in cfmp if c.eq_enable is not None]
    gn = [c for c in cfmp if c.eq_gain is not None]
    assert len(en) == 1 and en[0].eq_enable is True
    assert len(gn) == 1 and gn[0].eq_gain == -596       # -5.96 dB ×100
    assert cfmp.index(en[0]) < cfmp.index(gn[0])        # enable first

    # One CMBP per band, carrying the band index (no source attribute).
    bands = conn.code('CMBP')
    assert len(bands) == 2
    by_band = {b.band: b for b in bands}
    assert set(by_band) == {0, 2}
    assert not hasattr(bands[0], 'source')
    b2 = by_band[2]
    assert (b2.enabled, b2.frequency, b2.gain) == (True, 150, 2000)
    assert b2.q == 230                       # 2.3 ×100
    # The master bands must NOT be sent as per-strip CEBP (the old bug).
    assert conn.code('CEBP') == []


def test_apply_strip_expander_mode_expander_when_not_gate():
    root = ET.fromstring(
        '<Profile><FairlightAudioMixer><AudioInputs>'
        '<AudioInput id="2"><AudioSource id="-256">'
        '<DynamicsProcessor makeupGain="0">'
        '<Expander enabled="False" gateMode="False" threshold="-45"'
        ' range="18" ratio="1.1" attack="1.4" hold="0" release="93"/>'
        '</DynamicsProcessor></AudioSource></AudioInput>'
        '</AudioInputs></FairlightAudioMixer></Profile>')
    conn = _RecConn()
    _apply_fairlight(conn, root, ApplyResult())
    exp = conn.code('CIXP')[0]
    assert exp.source == 2 and exp.mode == 'expander'


# --------------------------------------------------------------------------
# DSK tie (CDsT)
# --------------------------------------------------------------------------

def test_apply_dsk_tie_true():
    root = ET.fromstring(
        '<Profile><DownstreamKeys>'
        '<DownstreamKey index="0" tie="True" fillSource="1" keySource="2"'
        ' rate="25"/></DownstreamKeys></Profile>')
    conn = _RecConn()
    _apply_downstream_keys(conn, root, ApplyResult())

    ties = conn.code('CDsT')
    assert len(ties) == 1
    assert ties[0].index == 0
    assert ties[0].tie is True


def test_apply_dsk_tie_false():
    root = ET.fromstring(
        '<Profile><DownstreamKeys>'
        '<DownstreamKey index="0" tie="False" rate="25"/>'
        '</DownstreamKeys></Profile>')
    conn = _RecConn()
    _apply_downstream_keys(conn, root, ApplyResult())
    assert conn.code('CDsT')[0].tie is False


def test_apply_downstream_keys_no_dsk_cap():
    """Apply is file-driven: a 4-DSK profile applies all four blocks.
    Mirrors the save-side _dsk_count fix — neither side caps at 2."""
    blocks = ''.join(
        f'<DownstreamKey index="{i}" tie="True" rate="25"/>'
        for i in range(4))
    root = ET.fromstring(
        f'<Profile><DownstreamKeys>{blocks}</DownstreamKeys></Profile>')
    conn = _RecConn()
    _apply_downstream_keys(conn, root, ApplyResult())
    assert [t.index for t in conn.code('CDsT')] == [0, 1, 2, 3]
