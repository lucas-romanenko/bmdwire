# SPDX-License-Identifier: LGPL-3.0-only
"""Migration tests for the ``fairlight`` feature.

Phase 3 batch 11/22 — the final migrating feature. 7 operations +
9 readers + 3 constants + 1 helper (``_db_to_wire``) migrated to
``atemwire.messages.fairlight``. The ``atemwire.operations`` and
``atemwire.state`` shims that briefly preserved the legacy import
paths were deleted in the 2026-05-14 shim-removal close-out;
``ATEMStateMixin`` / ``build_full_state`` now live in
``atemwire._state``.

Coverage focus:

  - Operations wire-emission spot-checks per Send class (CFMP, CFSP,
    CEBP, CICP, CILP, CIXP, SFLN) — confirm the byte the dispatch
    table hands the connection has the right command code.
  - Setter contract: dB → wire 0.01-dB scaling via ``_db_to_wire``;
    ``-inf`` clamp on volume; named enums vs int passthrough for
    ``mix_option`` / EQ ``shape`` / ``frequency_range``; unknown
    enum names raise ValueError.
  - Reader spot-checks: ``fairlight_master`` / ``fairlight_strips``
    /``fairlight_eq_bands`` / ``fairlight_compressor`` /
    ``fairlight_expander`` / ``fairlight_headphones`` / default-when-
    missing path on every reader.
  - ``fairlight_solo``: the reader was FIXED 2026-06-10 — it now
    reads the real FAMS attribute names (``solo`` / ``channel`` /
    ``is_split_lr`` / ``subchannel``); the tests below pin the
    correct behaviour.
"""

import struct

import pytest

from atemwire.messages.fairlight import (
    FAIRLIGHT_EQ_FREQ_RANGES,
    FAIRLIGHT_EQ_SHAPES,
    FAIRLIGHT_MIX_OPTIONS,
    FairlightMasterCompressorPropertiesField,
    FairlightMasterLimiterPropertiesField,
    FairlightStripPropertiesField,
    _db_to_wire,
    enable_fairlight_levels,
    fairlight_audio_inputs,
    fairlight_compressor,
    fairlight_eq_bands,
    fairlight_expander,
    fairlight_headphones,
    fairlight_limiter,
    fairlight_master,
    fairlight_master_compressor,
    fairlight_master_limiter,
    fairlight_solo,
    fairlight_strips,
    set_fairlight_compressor,
    set_fairlight_eq_band,
    set_fairlight_expander,
    set_fairlight_limiter,
    set_fairlight_master,
    set_fairlight_master_compressor,
    set_fairlight_master_eq_band,
    set_fairlight_master_limiter,
    set_fairlight_strip,
)


class _FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd.get_command())


# ---------------------------------------------------------------------------
# Recv-shape fakes — duck-type only the attributes each reader reads.
# ---------------------------------------------------------------------------


class _FAMP:
    """FairlightMasterPropertiesField stand-in."""
    def __init__(self, volume=-1000, eq_enable=True, eq_gain=500,
                 dynamics_gain=300, afv=False):
        self.volume = volume
        self.eq_enable = eq_enable
        self.eq_gain = eq_gain
        self.dynamics_gain = dynamics_gain
        self.afv = afv


class _FASP:
    """FairlightStripPropertiesField stand-in. The strip-dict helper reads
    a much wider attribute set; we set up reasonable defaults for any
    field touched by ``_fairlight_strip_dict``."""
    def __init__(self, *, is_split=0x01, subchannel=0, volume=-1000,
                 gain=0, pan=0, state=2, delay=0, eq_enable=False,
                 eq_gain=0, dynamics_gain=0):
        self.is_split = is_split
        self.subchannel = subchannel
        self.volume = volume
        self.gain = gain
        self.pan = pan
        self.state = state
        self.delay = delay
        self.eq_enable = eq_enable
        self.eq_gain = eq_gain
        self.dynamics_gain = dynamics_gain


class _FAIP:
    """FairlightAudioInputField stand-in."""
    def __init__(self, type=0, number=0, level=0, split=0x01):
        self.type = type
        self.number = number
        self.level = level
        self.split = split


class _InPr:
    def __init__(self, name=b'Camera 1\x00', short_name=b'CAM1\x00'):
        self.name = name
        self.short_name = short_name


class _AEBP:
    """One EQ-band record (AEBP / AMBP)."""
    def __init__(self, *, band_index=0, band_enabled=True, band_filter=0x01,
                 band_freq_range=2, band_frequency=200, band_gain=500,
                 band_q=70, band_possible_filters=0x3f):
        self.band_index = band_index
        self.band_enabled = band_enabled
        self.band_filter = band_filter
        self.band_freq_range = band_freq_range
        self.band_frequency = band_frequency
        self.band_gain = band_gain
        self.band_q = band_q
        self.band_possible_filters = band_possible_filters


class _Dyn:
    """Compressor / limiter / expander field stand-in. ``_strip_id_for``
    reads ``index`` / ``is_split`` / ``subchannel``. ``_dynamics_block_dict``
    reads ``enabled`` / ``threshold`` / ``attack`` / ``hold`` / ``release``
    plus optional ``ratio`` / ``range``. Expander adds ``mode``."""
    def __init__(self, *, index=1, is_split=0x01, subchannel=0,
                 enabled=True, threshold=-2000, attack=100, hold=400,
                 release=1500, ratio=200, range=3000, mode=0):
        self.index = index
        self.is_split = is_split
        self.subchannel = subchannel
        self.enabled = enabled
        self.threshold = threshold
        self.attack = attack
        self.hold = hold
        self.release = release
        self.ratio = ratio
        self.range = range
        self.mode = mode


class _FMHP:
    def __init__(self, volume=-300, unmuted=True):
        self.volume = volume
        self.unmuted = unmuted


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_constants_values():
    assert FAIRLIGHT_MIX_OPTIONS == {'Off': 0x01, 'On': 0x02,
                                      'AudioFollowVideo': 0x04}
    assert FAIRLIGHT_EQ_SHAPES == {
        'LowShelf': 0x01, 'LowPass': 0x02, 'BandPass': 0x04,
        'Notch': 0x08, 'HighPass': 0x10, 'HighShelf': 0x20,
    }
    assert FAIRLIGHT_EQ_FREQ_RANGES == {'Low': 1, 'MidLow': 2,
                                         'MidHigh': 4, 'High': 8}


# ---------------------------------------------------------------------------
# _db_to_wire helper
# ---------------------------------------------------------------------------


def test_db_to_wire_scales_and_rounds():
    assert _db_to_wire(0.0) == 0
    assert _db_to_wire(-12.5, scale=100, lo=-10000, hi=1000) == -1250
    assert _db_to_wire(5.4321, scale=100, lo=-10000, hi=1000) == 543


def test_db_to_wire_negative_infinity_clamps_to_floor():
    assert _db_to_wire(float('-inf'), lo=-10000) == -10000
    assert _db_to_wire(float('-inf'), lo=0, hi=2000) == 0


def test_db_to_wire_clamps_high():
    assert _db_to_wire(999.0, hi=1000) == 1000


def test_db_to_wire_clamps_low():
    assert _db_to_wire(-999.0, lo=-2000) == -2000


# ---------------------------------------------------------------------------
# Operations — wire-code emission
# ---------------------------------------------------------------------------


def test_set_fairlight_master_emits_cfmp():
    conn = _FakeConn()
    set_fairlight_master(conn, eq_gain_db=3.0, volume_db=0.0)
    assert conn.sent[0][4:8] == b'CFMP'


def test_set_fairlight_master_volume_neg_inf_clamps():
    """-inf volume becomes wire -10000 (silenced)."""
    conn = _FakeConn()
    set_fairlight_master(conn, volume_db=float('-inf'))
    pkt = conn.sent[0]
    # CFMP layout: 1 mask byte then 5x pad, then i16 eq_gain at offset 14
    # (header 8 + 6 → 14). Volume is i32 at offset 8+12 = 20.
    import struct
    volume_wire = struct.unpack_from('>i', pkt, 20)[0]
    assert volume_wire == -10000


def test_set_fairlight_strip_emits_cfsp_with_split_flag():
    conn = _FakeConn()
    set_fairlight_strip(conn, source=1, channel=-1, fader_gain_db=0.0)
    pkt = conn.sent[0]
    assert pkt[4:8] == b'CFSP'


def test_set_fairlight_strip_mix_option_by_name():
    conn = _FakeConn()
    set_fairlight_strip(conn, source=1, channel=-1, mix_option='AudioFollowVideo')
    assert len(conn.sent) == 1
    assert conn.sent[0][4:8] == b'CFSP'


def test_set_fairlight_strip_mix_option_int_passthrough():
    conn = _FakeConn()
    set_fairlight_strip(conn, source=1, channel=-1, mix_option=2)
    assert conn.sent[0][4:8] == b'CFSP'


def test_set_fairlight_strip_unknown_mix_option_raises():
    conn = _FakeConn()
    with pytest.raises(ValueError, match='unknown mix_option'):
        set_fairlight_strip(conn, source=1, channel=-1, mix_option='Bogus')


def test_set_fairlight_eq_band_emits_cebp_with_shape_name():
    conn = _FakeConn()
    set_fairlight_eq_band(conn, source=1, channel=-1, band=2,
                          shape='BandPass', frequency_range='MidLow',
                          gain_db=2.5)
    assert conn.sent[0][4:8] == b'CEBP'


def test_set_fairlight_eq_band_unknown_shape_raises():
    conn = _FakeConn()
    with pytest.raises(ValueError, match='unknown EQ shape'):
        set_fairlight_eq_band(conn, source=1, channel=-1, band=0,
                              shape='Crunch')


def test_set_fairlight_eq_band_unknown_freq_range_raises():
    conn = _FakeConn()
    with pytest.raises(ValueError, match='unknown EQ frequency_range'):
        set_fairlight_eq_band(conn, source=1, channel=-1, band=0,
                              frequency_range='Ultra')


def test_set_fairlight_compressor_emits_cicp():
    conn = _FakeConn()
    set_fairlight_compressor(conn, source=1, channel=-1, threshold_db=-20.0,
                             ratio=4.0)
    assert conn.sent[0][4:8] == b'CICP'


def test_set_fairlight_limiter_emits_cilp():
    conn = _FakeConn()
    set_fairlight_limiter(conn, source=1, channel=-1, threshold_db=-10.0)
    assert conn.sent[0][4:8] == b'CILP'


def test_set_fairlight_expander_emits_cixp_with_mode():
    conn = _FakeConn()
    set_fairlight_expander(conn, source=1, channel=-1, mode='gate',
                            threshold_db=-30.0, range_db=20.0)
    assert conn.sent[0][4:8] == b'CIXP'


def test_enable_fairlight_levels_emits_sfln():
    conn = _FakeConn()
    enable_fairlight_levels(conn, enable=True)
    assert conn.sent[0][4:8] == b'SFLN'


def test_enable_fairlight_levels_disable_emits_sfln():
    conn = _FakeConn()
    enable_fairlight_levels(conn, enable=False)
    assert conn.sent[0][4:8] == b'SFLN'


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def test_fairlight_master_default_when_missing():
    assert fairlight_master({}) == {
        'present': False,
        'volume_db': 0.0,
        'eq_enable': False,
        'eq_gain_db': 0.0,
        'dynamics_makeup_db': 0.0,
        'afv': False,
    }


def test_fairlight_master_reads_wire_db():
    mx = {'fairlight-master-properties': _FAMP(
        volume=-1234, eq_gain=500, dynamics_gain=600, eq_enable=True,
        afv=True,
    )}
    out = fairlight_master(mx)
    assert out['present'] is True
    assert out['volume_db'] == -12.34
    assert out['eq_gain_db'] == 5.0
    assert out['dynamics_makeup_db'] == 6.0
    assert out['eq_enable'] is True
    assert out['afv'] is True


def test_fairlight_strips_empty_when_missing():
    assert fairlight_strips({}) == []


def test_fairlight_strips_orders_cameras_before_mp_before_mic():
    """type=0 cameras first, then type=1 MP audio, then type=2 mic/line."""
    mx = {
        'fairlight-strip-properties': {
            '1.0': _FASP(volume=-1000, state=2),       # camera
            '2001.0': _FASP(volume=-500, state=2),     # MP audio
            '1301.0': _FASP(volume=0, state=2),        # mic
        },
        'fairlight-audio-input': {
            1: _FAIP(type=0, number=1),
            2001: _FAIP(type=1, number=0),
            1301: _FAIP(type=2, number=0, level=1),
        },
        'input-properties': {
            1: _InPr(name=b'Camera 1\x00', short_name=b'CAM1\x00'),
        },
    }
    out = fairlight_strips(mx)
    assert [s['source'] for s in out] == [1, 2001, 1301]
    assert out[0]['display_name'] == 'CAM1'
    assert out[1]['display_name'] == 'MP1'
    assert out[2]['display_name'] == 'Mic'


def test_fairlight_audio_inputs_decodes_metadata():
    mx = {'fairlight-audio-input': {1: _FAIP(type=0, level=4, split=0xff)}}
    out = fairlight_audio_inputs(mx)
    assert out == {1: {'type': 0, 'split': 0xff, 'level': 4}}


def test_fairlight_eq_bands_sorts_by_index_and_handles_master_id_none():
    mx = {
        'atem-master-eq-band-properties': {
            0: _AEBP(band_index=2),
            1: _AEBP(band_index=0),
        },
    }
    out = fairlight_eq_bands(mx, None)
    assert [b['index'] for b in out] == [0, 2]


def test_fairlight_compressor_keys_by_strip_id():
    mx = {'fairlight-compressor-properties': {
        0: _Dyn(index=1, is_split=0x01, subchannel=0, threshold=-1500),
    }}
    out = fairlight_compressor(mx)
    assert '1.0' in out
    assert out['1.0']['threshold_db'] == -15.0
    assert out['1.0']['ratio'] == 2.0  # ratio=200 → /100 = 2.0


def test_fairlight_limiter_does_not_include_ratio():
    mx = {'fairlight-limiter-properties': {
        0: _Dyn(index=2, is_split=0x01, subchannel=0),
    }}
    out = fairlight_limiter(mx)
    assert 'ratio' not in out['2.0']


def test_fairlight_expander_mode_byte_maps_to_gate_or_expander():
    mx_expander = {'fairlight-expander-properties': {
        0: _Dyn(index=3, mode=0),
    }}
    mx_gate = {'fairlight-expander-properties': {
        0: _Dyn(index=4, mode=1),
    }}
    assert fairlight_expander(mx_expander)['3.0']['mode'] == 'expander'
    assert fairlight_expander(mx_gate)['4.0']['mode'] == 'gate'


def test_fairlight_headphones_default_when_missing():
    assert fairlight_headphones({}) == {
        'present': False, 'volume_db': 0.0, 'unmuted': True,
    }


def test_fairlight_headphones_reads_wire_db():
    mx = {'fairlight-headphones': _FMHP(volume=-1500, unmuted=False)}
    out = fairlight_headphones(mx)
    assert out['present'] is True
    assert out['volume_db'] == -15.0
    assert out['unmuted'] is False


# ---------------------------------------------------------------------------
# Latent bug preservation — fairlight_solo
# ---------------------------------------------------------------------------


class _FAMS_realshape:
    """The actual FairlightSoloField shape: solo / channel / is_split_lr /
    subchannel. ``is_split_lr = 0xff`` is the split marker the reader keys
    off for the ``{channel}.{subchannel}`` strip id."""
    solo = True
    channel = 1
    is_split_lr = 0xff
    subchannel = 1


class _FAMS_notsplit:
    """A soloed, non-split source — reported as ``{channel}.0``."""
    solo = True
    channel = 3
    is_split_lr = 0x01
    subchannel = 0


def test_fairlight_solo_default_when_missing():
    """No FAMS node → empty form. This branch works correctly."""
    assert fairlight_solo({}) == {'any_soloed': False, 'strip_id': None}


def test_fairlight_solo_reads_real_fams_shape():
    """Reader fix (2026-06-10): the reader now reads the real FAMS attribute
    names (``solo`` / ``channel`` / ``is_split_lr`` / ``subchannel``), so a
    genuinely-soloed strip is reported instead of the old always-empty form.

    Was ``test_fairlight_solo_latent_bug_attributes_dont_match``, which
    pinned the broken behaviour (reader looked up ``any_soloed`` / ``source``
    / ``is_split``, names the Recv never exposed). _FAMS_realshape: solo,
    channel 1, split (is_split_lr=0xff), subchannel 1 → strip id '1.1'."""
    mx = {'fairlight-solo': _FAMS_realshape()}
    assert fairlight_solo(mx) == {'any_soloed': True, 'strip_id': '1.1'}


def test_fairlight_solo_non_split_strip():
    """A soloed non-split source (is_split_lr != 0xff) reports the '.0'
    suffix — channel 3 → strip id '3.0'."""
    mx = {'fairlight-solo': _FAMS_notsplit()}
    assert fairlight_solo(mx) == {'any_soloed': True, 'strip_id': '3.0'}


def test_fairlight_solo_not_soloed_returns_empty():
    """solo=False → empty form regardless of channel/split fields."""
    class _Off:
        solo = False
        channel = 5
        is_split_lr = 0xff
        subchannel = 1
    assert fairlight_solo({'fairlight-solo': _Off()}) == {
        'any_soloed': False, 'strip_id': None}


# ---------------------------------------------------------------------------
# Master-out dynamics decode — MOCP / AMLP (compact master layout)
# ---------------------------------------------------------------------------

def test_mocp_decodes_master_compressor():
    """``MOCP`` master-out compressor, 24 bytes, all values ×100. Live
    Constellation HD reference: enable on, threshold fa24 = -1500 (-15.00
    dB), ratio 018d = 397 (3.97), attack 0000008c = 140 (1.40 ms), release
    00002454 = 9300 (93.00 ms). Pins the compact master layout (threshold
    s16 @6, ratio u16 @8, attack/hold/release i32 @12/16/20) — distinct
    from the per-strip AICP."""
    raw = bytes.fromhex(
        '01 ff ff 00 ff ff fa 24 01 8d ff ff 00 00 00 8c '
        '00 00 00 00 00 00 24 54'.replace(' ', ''))
    assert len(raw) == 24
    f = FairlightMasterCompressorPropertiesField(raw)
    assert f.enabled is True
    assert f.threshold == -1500
    assert f.ratio == 397
    assert f.attack == 140
    assert f.hold == 0
    assert f.release == 9300

    # Reader maps the ×100 wire ints to operator-friendly floats.
    out = fairlight_master_compressor(
        {'fairlight-master-compressor-properties': f})
    assert out == {
        'enabled': True, 'threshold_db': -15.0, 'ratio': 3.97,
        'attack_ms': 1.4, 'hold_ms': 0.0, 'release_ms': 93.0,
    }
    assert fairlight_master_compressor({}) is None


def test_amlp_decodes_master_limiter():
    """``AMLP`` master-out limiter, 20 bytes, all values ×100. Same compact
    master layout as MOCP minus the ratio field, so attack/hold/release
    shift up by 4. Live reference: enable on, threshold fe0b = -501 (-5.01
    dB), attack 00000047 = 71 (0.71 ms), release 00002454 = 9300 (93.00
    ms)."""
    raw = bytes.fromhex(
        '01 15 00 00 ff ff fe 0b 00 00 00 47 00 00 00 00 '
        '00 00 24 54'.replace(' ', ''))
    assert len(raw) == 20
    f = FairlightMasterLimiterPropertiesField(raw)
    assert f.enabled is True
    assert f.threshold == -501
    assert f.attack == 71
    assert f.hold == 0
    assert f.release == 9300

    out = fairlight_master_limiter(
        {'fairlight-master-limiter-properties': f})
    assert out == {
        'enabled': True, 'threshold_db': -5.01,
        'attack_ms': 0.71, 'hold_ms': 0.0, 'release_ms': 93.0,
    }
    assert fairlight_master_limiter({}) is None


# ---------------------------------------------------------------------------
# Master-out dynamics SET — CMCP / CMLP (RE'd from a Wireshark capture of
# ATEM Software Control; no source/channel, 1-byte mask, ×100 wire)
# ---------------------------------------------------------------------------

def test_set_master_compressor_emits_cmcp_full_mask():
    """CMCP master compressor SET. Full-mask encode of the captured final
    values pins the layout: mask@0, enabled@1, threshold sign-extended i32 @4
    (value low 2 bytes @6-7), ratio u16 @8, attack/hold/release i32 @12/16/20,
    all ×100. The threshold is i32@4 NOT s16@6 — a negative value's sign-ext
    bytes occupy 4-5 (the capture's apparent "padding"); s16@6 made the ATEM
    clamp negatives to 0. Fix confirmed live 2026-06-09 on hardware."""
    conn = _FakeConn()
    set_fairlight_master_compressor(
        conn, enabled=True, threshold_db=-23.55, ratio=6.01,
        attack_ms=11.56, hold_ms=7.04, release_ms=249.76)
    pkt = conn.sent[0]
    assert pkt[4:8] == b'CMCP'
    payload = pkt[8:]
    assert len(payload) == 24
    assert payload[0] == 0x3f                                   # all 6 fields
    assert payload[1] == 0x01                                   # enabled
    assert struct.unpack_from('>i', payload, 4)[0] == -2355     # threshold i32@4
    assert struct.unpack_from('>h', payload, 6)[0] == -2355     # value in low 2B
    assert struct.unpack_from('>H', payload, 8)[0] == 601       # ratio
    assert struct.unpack_from('>i', payload, 12)[0] == 1156     # attack
    assert struct.unpack_from('>i', payload, 16)[0] == 704      # hold
    assert struct.unpack_from('>i', payload, 20)[0] == 24976    # release


def test_set_master_compressor_single_field_matches_capture():
    """One masked field reproduces the capture's mask byte + offset. The ASC
    threshold-only CMCP packet carries the threshold as a sign-extended i32 at
    offset 4 (value in the low 2 bytes @6-7) — the capture's bytes 4-5 are
    FF FF for a negative value, not padding."""
    conn = _FakeConn()
    set_fairlight_master_compressor(conn, threshold_db=-23.5)
    payload = conn.sent[0][8:]
    assert payload[0] == 0x02                                   # threshold bit
    assert struct.unpack_from('>i', payload, 4)[0] == -2350     # i32@4
    # sign-extension occupies bytes 4-5 (would be 0000 under the old s16@6 bug)
    assert payload[4:6] == b'\xff\xff'


def test_set_master_limiter_emits_cmlp_full_mask():
    """CMLP master limiter SET. No ratio, so attack/hold/release sit at
    @8/12/16 (the AMLP echo layout). Threshold is a sign-extended i32 @4
    (NOT s16@6 — same trap as CMCP; a negative otherwise clamps to 0).
    Pinned to the captured final values; fix confirmed live 2026-06-09."""
    conn = _FakeConn()
    set_fairlight_master_limiter(
        conn, enabled=True, threshold_db=-0.99, attack_ms=2.24,
        hold_ms=5.0, release_ms=99.59)
    pkt = conn.sent[0]
    assert pkt[4:8] == b'CMLP'
    payload = pkt[8:]
    assert len(payload) == 20
    assert payload[0] == 0x1f                                   # all 5 fields
    assert payload[1] == 0x01                                   # enabled
    assert struct.unpack_from('>i', payload, 4)[0] == -99       # threshold i32@4
    assert struct.unpack_from('>i', payload, 8)[0] == 224       # attack
    assert struct.unpack_from('>i', payload, 12)[0] == 500      # hold
    assert struct.unpack_from('>i', payload, 16)[0] == 9959     # release


def test_set_master_limiter_single_field_matches_capture():
    """ASC release-only CMLP packet had mask 0x10 with the i32 release at
    offset 16."""
    conn = _FakeConn()
    set_fairlight_master_limiter(conn, release_ms=100.0)
    payload = conn.sent[0][8:]
    assert payload[0] == 0x10                                   # release bit
    assert struct.unpack_from('>i', payload, 16)[0] == 10000


# ---------------------------------------------------------------------------
# Master-out EQ — CFMP enable/gain offsets + CMBP band SET (RE'd from a
# Wireshark capture of ATEM Software Control)
# ---------------------------------------------------------------------------

def test_set_master_eq_enable_gain_offsets_match_capture():
    """CFMP master EQ: enable bool @ byte 1, gain SIGNED i32 @ byte 4 (mask
    bits 0 and 1). The capture shows ASC sends the gain as a sign-extended
    i32 at offset 4 (value in the low 2 bytes @6-7), NOT an s16 @6, and
    enable @1 (not @17). Regression guards for both offsets."""
    conn = _FakeConn()
    set_fairlight_master(conn, eq_enable=True, eq_gain_db=8.7)
    pkt = conn.sent[0]
    assert pkt[4:8] == b'CFMP'
    p = pkt[8:]
    assert p[0] == 0x03                                  # eq_enable | eq_gain
    assert p[1] == 1                                     # eq_enable @1 (not @17)
    assert struct.unpack_from('>i', p, 4)[0] == 870      # eq_gain i32 @4
    assert struct.unpack_from('>h', p, 6)[0] == 870      # value low 2 bytes


def test_set_master_eq_gain_negative_sign_extends():
    """A NEGATIVE master EQ gain must sign-extend across the i32 (bytes 4-5
    = 0xFFFF) or the ATEM reads it as a large positive and clamps to max
    +20.00 dB. -11.45 -> -1145 = 0xFFFFFB87. Regression guard for the
    'gain jumps to max +20' bug (in the ASC capture every gain packet
    satisfies i32@4 == s16@6, with FF FF in bytes 4-5 for negatives)."""
    conn = _FakeConn()
    set_fairlight_master(conn, eq_gain_db=-11.45)
    p = conn.sent[0][8:]
    assert p[0] == 0x02                                  # eq_gain bit only
    assert struct.unpack_from('>i', p, 4)[0] == -1145    # signed i32 @4
    assert p[4] == 0xff and p[5] == 0xff                 # sign-extension
    assert struct.unpack_from('>h', p, 6)[0] == -1145    # value low 2 bytes


def test_set_master_eq_band_emits_cmbp_full():
    """CMBP master EQ band SET. Full encode pins the layout: mask@0,
    band_index@1, enabled@2, filter@3, freq_range@4, frequency u16@10,
    gain i32@12, Q u16@16 — all ×100. No source/channel."""
    conn = _FakeConn()
    set_fairlight_master_eq_band(
        conn, band=1, enabled=True, shape='LowShelf', frequency_range='Low',
        frequency_hz=153, gain_db=13.5, q=2.3)
    pkt = conn.sent[0]
    assert pkt[4:8] == b'CMBP'
    p = pkt[8:]
    assert len(p) == 20
    assert p[0] == 0x3f                                  # all six fields
    assert p[1] == 1                                     # band index
    assert p[2] == 1                                     # enabled
    assert p[3] == 0x01                                  # LowShelf bitfield
    assert p[4] == 0x01                                  # freq range Low
    assert struct.unpack_from('>H', p, 10)[0] == 153     # frequency
    assert struct.unpack_from('>i', p, 12)[0] == 1350    # gain ×100
    assert struct.unpack_from('>H', p, 16)[0] == 230     # Q ×100


def test_set_master_eq_band_single_fields_match_capture():
    """Single-field CMBP encodes reproduce the capture's mask byte, band
    index, and offset: frequency mask 0x08 @10, gain mask 0x10 @12, Q mask
    0x20 @16, filter mask 0x02 @3."""
    conn = _FakeConn()
    set_fairlight_master_eq_band(conn, band=1, frequency_hz=51)
    p = conn.sent[-1][8:]
    assert p[0] == 0x08 and p[1] == 1
    assert struct.unpack_from('>H', p, 10)[0] == 51

    conn = _FakeConn()
    set_fairlight_master_eq_band(conn, band=1, gain_db=-11.0)
    p = conn.sent[-1][8:]
    assert p[0] == 0x10 and p[1] == 1
    assert struct.unpack_from('>i', p, 12)[0] == -1100

    conn = _FakeConn()
    set_fairlight_master_eq_band(conn, band=3, q=0.8)
    p = conn.sent[-1][8:]
    assert p[0] == 0x20 and p[1] == 3
    assert struct.unpack_from('>H', p, 16)[0] == 80

    conn = _FakeConn()
    set_fairlight_master_eq_band(conn, band=1, shape='Notch')
    p = conn.sent[-1][8:]
    assert p[0] == 0x02 and p[1] == 1
    assert p[3] == 0x08                                  # Notch bitfield


# ---------------------------------------------------------------------------
# Per-strip delay — FASP delay field offset (17 -> 18) round-trip
# ---------------------------------------------------------------------------

def _pack_fasp(*, index, delay, decoy_at_17=0xff, gain=0, volume=0,
               pan=0, state=0x02, subchannel=0, is_split=0x00):
    """Build a 52-byte ``FASP`` payload. ``delay`` lands at offset 18;
    ``decoy_at_17`` is written at the *old* (buggy) offset 17 so a parser
    reading offset 17 would return the decoy instead of the real delay."""
    buf = bytearray(52)
    struct.pack_into('>H', buf, 0, index)
    buf[14] = is_split
    buf[15] = subchannel
    buf[17] = decoy_at_17          # old offset — must NOT be read as delay
    buf[18] = delay                # real offset
    struct.pack_into('>h', buf, 22, gain)
    struct.pack_into('>h', buf, 40, pan)
    struct.pack_into('>h', buf, 46, volume)
    buf[49] = state
    return bytes(buf)


def test_fasp_decodes_delay_at_offset_18():
    """Regression guard for the FASP ``delay`` offset 17 -> 18 fix (correct
    for Constellation HD analog strips). The decoy byte at offset 17 differs
    from the real delay at 18, so a parser still reading 17 fails here."""
    raw = _pack_fasp(index=1301, delay=2, decoy_at_17=0xff)
    f = FairlightStripPropertiesField(raw)
    assert f.index == 1301
    assert f.delay == 2            # offset 18, NOT the 0xff decoy at 17
    assert f.strip_id == '1301.0'


def test_strip_delayframes_roundtrips_through_save():
    """Wire FASP -> save XML: a decoded strip with delay=2 on an analog
    source (id >= 1300) emits ``delayFrames="2"`` on its ``<AudioSource>``.
    Pins the offset-18 delay all the way to the profile output."""
    import xml.etree.ElementTree as ET
    from types import SimpleNamespace

    from atemwire.profile.save import _build_fairlight

    strip = FairlightStripPropertiesField(
        _pack_fasp(index=1301, delay=2, decoy_at_17=0xff))
    mx = {
        'fairlight-audio-input': {1301: SimpleNamespace(split=2, level=0)},
        'fairlight-strip-properties': {'1301.0': strip},
    }
    root = ET.Element('Profile')
    _build_fairlight(root, mx)

    src = root.find(
        "FairlightAudioMixer/AudioInputs/AudioInput[@id='1301']/AudioSource")
    assert src is not None
    assert int(src.get('delayFrames')) == 2
