# SPDX-License-Identifier: LGPL-3.0-only
"""Tests for the master-out EQ band (AMBP) parser + per-band keying.

Regression guards for the 2026-06 master-EQ-qFactor fix. Before the fix,
``AMBP`` had no Recv class: the protocol dispatch stored each packet's raw
bytes under the 4-char code ``'AMBP'`` via the non-unique ``else`` branch,
so every band overwrote the previous one (only the last survived), and
``profile.save._build_eq_bands(source_id=None)`` fell back to integer
defaults forever.

The fix added ``AtemMasterEqBandPropertiesField`` (CODE ``AMBP``, PRETTY
``atem-master-eq-band-properties``, ``KEY_FORMAT = '>B'`` keying on
``band_index``) and removed ``'AMBP'`` from ``_UNMAPPED_PRETTY``. These
tests pin:

  1. the wire decode (against a live band-5 reference packet),
  2. that six AMBP packets accumulate as a 6-entry per-band dict in
     ``mixerstate['atem-master-eq-band-properties']`` (NOT one overwritten
     slot under the raw ``'AMBP'`` key),
  3. that ``_build_eq_bands(source_id=None)`` now emits the six LIVE
     ``band_q`` values instead of the integer defaults.

No hardware: ``AtemProtocol(ip=...)`` creates an unbound UDP socket but
starts no thread and sends nothing; we drive ``save_field_data`` directly.
"""

import struct
import xml.etree.ElementTree as ET

import pytest

from atemwire.messages.audio_legacy import AtemMasterEqBandPropertiesField
from atemwire.profile.save import _build_eq_bands
from atemwire.protocol import AtemProtocol

# Master-EQ XML key after the fix (was absent — data was stranded under the
# raw 'AMBP' code).
_MASTER_KEY = 'atem-master-eq-band-properties'

# AMBP wire layout = AEBP band fields minus the 16-byte per-strip prefix.
_AMBP_FMT = '>B ? B B x B 4x H i H 2x'   # 20 bytes; band_index @0, band_q @16

# Live band-5 reference packet (LowPass, freq 12900, q 71 -> 0.71). The
# trailing 2-byte pad happens to carry ASCII 'LP' in this capture; it's
# skipped by the format and must not affect the decode.
_BAND5_REF = bytes.fromhex('05 00 33 02 08 08 00 00 00 00'
                           '32 64 00 00 00 00 00 47 4c 50'.replace(' ', ''))

# Save-side defaults that the override loop must NOT fall back to once live
# bands are present (q column of the _build_eq_bands defaults table).
_DEFAULT_Q = [0.71, 1, 2, 2, 1, 0.71]


def _pack_ambp(band_index, band_q, *, enabled=True, possible_filters=0x3f,
               band_filter=0x01, freq_range=2, frequency=200, gain=0):
    """Build one synthetic AMBP packet."""
    return struct.pack(_AMBP_FMT, band_index, enabled, possible_filters,
                       band_filter, freq_range, frequency, gain, band_q)


@pytest.fixture
def proto():
    """Unconnected AtemProtocol — drives save_field_data with no socket I/O."""
    p = AtemProtocol(ip='0.0.0.0')
    try:
        yield p
    finally:
        p.transport.sock.close()


def test_ambp_decodes_band5_reference():
    """The verified band-5 wire bytes decode to the expected band fields."""
    f = AtemMasterEqBandPropertiesField(_BAND5_REF)
    assert f.band_index == 5
    assert f.band_frequency == 12900
    assert f.band_gain == 0
    assert f.band_q == 71            # reader / save divide by 100 -> 0.71


def test_six_ambp_packets_persist_as_per_band_dict(proto):
    """Six AMBP packets (band_index 0..5) accumulate into a 6-entry dict
    keyed by band_index — the per-band-keying regression guard."""
    band_qs = [65, 110, 230, 250, 130, 95]
    for bi, bq in enumerate(band_qs):
        proto.save_field_data(b'AMBP', _pack_ambp(bi, bq, frequency=100 + bi))

    bands = proto.mixerstate.get(_MASTER_KEY)
    assert isinstance(bands, dict)
    # All six survive — pre-fix this was a single overwritten raw-bytes blob.
    assert sorted(bands) == [0, 1, 2, 3, 4, 5]
    assert len(bands) == 6
    for bi, bq in enumerate(band_qs):
        assert bands[bi].band_index == bi
        assert bands[bi].band_q == bq
    # Routed through the pretty key, not stranded under the raw 4-char code.
    assert 'AMBP' not in proto.mixerstate


def test_build_eq_bands_master_reads_live_band_q():
    """_build_eq_bands(source_id=None) emits the six live band_q values
    (band_q / 100), not the integer defaults."""
    band_qs = [65, 110, 230, 250, 130, 95]      # -> 0.65,1.1,2.3,2.5,1.3,0.95
    freqs = [40, 80, 160, 800, 7000, 12900]
    mx = {
        _MASTER_KEY: {
            bi: AtemMasterEqBandPropertiesField(
                _pack_ambp(bi, bq, frequency=fq))
            for bi, (bq, fq) in enumerate(zip(band_qs, freqs))
        }
    }

    parent = ET.Element('MasterOutEqualizer')
    _build_eq_bands(parent, mx, source_id=None)

    emitted = parent.findall('EqualizerBand')
    assert len(emitted) == 6
    by_index = {int(e.get('index')): e for e in emitted}

    got_q = [float(by_index[i].get('qFactor')) for i in range(6)]
    assert got_q == [bq / 100.0 for bq in band_qs]
    # Prove it's live data, not the fallback defaults.
    assert got_q != _DEFAULT_Q
    # Frequencies are live too (bonus — same override loop).
    got_freq = [int(by_index[i].get('frequency')) for i in range(6)]
    assert got_freq == freqs
