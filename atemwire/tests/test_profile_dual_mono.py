# SPDX-License-Identifier: LGPL-3.0-only
"""Dual-mono Fairlight inputs in profile save and apply, pinned to ATEM
Software Control's own export (bmdwire#9).

``profile_fairlight_dual_mono_asc.xml`` is the ``<FairlightAudioMixer>`` of a
Save Switcher State written by ASC 9.3.2 for a 1 M/E Constellation HD
(firmware 9.2.1) on 2026-10-13, otherwise unedited. Input 1 is in Dual Mono
with its halves set apart (left: fader -10 dB, pan -50, EQ band 0 +3 dB;
right: -20 dB, +50, -3 dB), input 2 is stereo, 1301 is the XLR.

ASC writes a dual-mono input as ``configuration="DualMono"`` with one
``<AudioSource>`` per half, left first. The id is the strip's wire address
read as a signed i64 (six 0xFF bytes, the split byte, the channel byte):
-256 the left half (split 0xff, channel 0), -255 the right (channel 1),
-65280 a stereo strip (split 0x01), and -256 again for the XLR's one strip.
A macro recorded on the same switcher names the halves the same way, as
``sourceId`` 18446744073709551360 and 18446744073709551361 (unsigned).

Before this, apply sent every strip to channel -1, the stereo address, so the
right half landed on top of the left on a strip a split input doesn't have;
and save wrote ``Stereo`` and -65280 for both halves, in arrival order.
"""
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from atemwire.profile._enums import (
    EQ_FREQ_RANGE_NAMES, EQ_SHAPE_NAMES, MIX_OPTION_TO_INT,
    audio_source_channel, audio_source_id,
)
from atemwire.profile.apply import _apply_fairlight
from atemwire.profile.options import ApplyResult
from atemwire.profile.save import _build_fairlight

FIXTURE = Path(__file__).with_name('profile_fairlight_dual_mono_asc.xml')

# (AudioSource id, setter channel), as captured.
CAPTURED_IDS = [(-65280, -1), (-256, 0), (-255, 1)]


class _RecConn:
    """Fake connection recording the Command objects sent."""
    def __init__(self):
        self.sent = []
        self.mixerstate = {}

    def send(self, cmd):
        self.sent.append(cmd)

    def code(self, code):
        return [c for c in self.sent if getattr(c, 'CODE', None) == code]


def _fixture():
    return ET.parse(FIXTURE).getroot()


def _input(root, src):
    return root.find(f".//AudioInput[@id='{src}']")


def _canon(elem):
    """An element as a string, whitespace between tags dropped."""
    elem = ET.fromstring(ET.tostring(elem))
    for e in elem.iter():
        e.text = e.tail = None
    return ET.tostring(elem, encoding='unicode')


def _hundredths(s):
    return -10000 if s == '-inf' else int(round(float(s) * 100))


def _mixerstate_from(root, faip_split):
    """The FAIP, FASP and AEBP records a switcher in the fixture's state
    would report for the inputs in ``faip_split`` (source -> FAIP byte 10).
    Strips go in last-first, so save can't lean on arrival order."""
    shape = {v: k for k, v in EQ_SHAPE_NAMES.items()}
    freq_range = {v: k for k, v in EQ_FREQ_RANGE_NAMES.items()}
    mx = {'fairlight-audio-input': {}, 'fairlight-strip-properties': {},
          'atem-eq-band-properties': {}}
    for src, split_byte in faip_split.items():
        ai = _input(root, src)
        mx['fairlight-audio-input'][src] = SimpleNamespace(
            split=split_byte, level=0)
        sources = list(enumerate(ai.findall('AudioSource')))
        for sub, s in reversed(sources):
            split = audio_source_channel(s.get('id')) != -1
            strip_id = f'{src}.{sub if split else 0}'
            eq = s.find('Equalizer')
            mx['fairlight-strip-properties'][strip_id] = SimpleNamespace(
                is_split=0xff if split else 0x01, subchannel=sub,
                volume=_hundredths(s.get('faderGain')),
                gain=_hundredths(s.get('inputGain')),
                pan=_hundredths(s.get('pan')),
                state=MIX_OPTION_TO_INT[s.get('mixOption')],
                delay=int(s.get('delayFrames', 0)),
                eq_enable=eq.get('enabled') == 'True',
                eq_gain=_hundredths(eq.get('gain')),
                dynamics_gain=_hundredths(
                    s.find('DynamicsProcessor').get('makeupGain')))
            mx['atem-eq-band-properties'][strip_id] = {
                int(b.get('index')): SimpleNamespace(
                    band_enabled=b.get('enabled') == 'True',
                    band_filter=shape[b.get('shape')],
                    band_freq_range=freq_range[b.get('frequencyRange')],
                    band_frequency=int(b.get('frequency')),
                    band_gain=_hundredths(b.get('gain')),
                    band_q=int(round(float(b.get('qFactor')) * 100)))
                for b in eq.findall('EqualizerBand')}
    return mx


# FAIP byte 10 for a dual-mono input wasn't captured. Save tells Dual Mono
# from the two split strips, so any value but 1 (Mono) must do.
@pytest.fixture(params=[2, 4], ids=['faip2', 'faip4'])
def saved(request):
    mx = _mixerstate_from(_fixture(), {1: request.param, 2: 2, 1301: 1})
    root = ET.Element('Profile')
    _build_fairlight(root, mx)
    return root


# --- What ASC wrote ----------------------------------------------------------

def test_asc_writes_a_dual_mono_input_as_two_sources_left_first():
    root = _fixture()
    ai = _input(root, 1)
    assert ai.get('configuration') == 'DualMono'
    left, right = ai.findall('AudioSource')
    assert (left.get('id'), left.get('faderGain'), left.get('pan')) == (
        '-256', '-10', '-50')
    assert (right.get('id'), right.get('faderGain'), right.get('pan')) == (
        '-255', '-20', '50')
    band = "Equalizer/EqualizerBand[@index='0']"
    assert (left.find(band).get('gain'), right.find(band).get('gain')) == (
        '3', '-3')

    stereo = _input(root, 2)
    assert stereo.get('configuration') == 'Stereo'
    assert [s.get('id') for s in stereo.findall('AudioSource')] == ['-65280']
    xlr = _input(root, 1301)
    assert xlr.get('configuration') == 'Mono'
    assert [s.get('id') for s in xlr.findall('AudioSource')] == ['-256']


# --- The id <-> channel pair -------------------------------------------------

@pytest.mark.parametrize('source_id, channel', CAPTURED_IDS)
def test_audio_source_id_is_the_strip_address(source_id, channel):
    assert audio_source_id(channel) == source_id
    assert audio_source_channel(str(source_id)) == channel
    # The unsigned form macro ops carry as sourceId.
    assert audio_source_channel(str(source_id & 0xFFFFFFFFFFFFFFFF)) == channel


@pytest.mark.parametrize('value', [None, '', 'x', '0', '-1', '-768'])
def test_an_unknown_id_is_taken_as_stereo(value):
    assert audio_source_channel(value) == -1


# --- Apply -------------------------------------------------------------------

def test_apply_sends_each_strip_to_the_address_asc_wrote():
    root = _fixture()
    conn = _RecConn()
    result = ApplyResult()
    _apply_fairlight(conn, root, result)
    assert result.errors == []
    # CFSP payload bytes 8-15 (after the 8-byte header) are the address.
    sent = [(c.source, struct.unpack('>q', c.get_command()[16:24])[0])
            for c in conn.code('CFSP') if c.volume is not None]
    assert sent == [(int(ai.get('id')), int(s.get('id')))
                    for ai in root.iter('AudioInput')
                    for s in ai.findall('AudioSource')]


def test_apply_gives_each_half_its_own_values():
    conn = _RecConn()
    _apply_fairlight(conn, _fixture(), ApplyResult())

    strips = {c.channel: c for c in conn.code('CFSP')
              if c.source == 1 and c.volume is not None}
    assert sorted(strips) == [0, 1]
    assert (strips[0].volume, strips[0].balance) == (-1000, -5000)
    assert (strips[1].volume, strips[1].balance) == (-2000, 5000)

    bands = {c.channel: c for c in conn.code('CEBP')
             if c.source == 1 and c.band == 0}
    assert (bands[0].frequency, bands[0].gain) == (35, 300)
    assert (bands[1].frequency, bands[1].gain) == (46, -300)

    for code in ('CICP', 'CILP', 'CIXP'):
        assert sorted(c.channel for c in conn.code(code) if c.source == 1) \
            == [0, 1]
    by_source = {}
    for c in conn.sent:
        by_source.setdefault(getattr(c, 'source', None), set()).add(
            getattr(c, 'channel', None))
    assert by_source[2] == {-1}          # stereo, as before
    assert by_source[1301] == {0}        # the XLR, at ASC's -256


# --- Save --------------------------------------------------------------------

@pytest.mark.parametrize('src', [1, 2, 1301])
def test_save_writes_what_asc_wrote(saved, src):
    assert _canon(_input(saved, src)) == _canon(_input(_fixture(), src))


def test_save_keeps_the_xlr_at_minus_256_if_its_strip_reads_stereo():
    # The XLR's FASP split byte wasn't captured; either way it saves as ASC
    # wrote it.
    root_fx = _fixture()
    mx = _mixerstate_from(root_fx, {1301: 1})
    mx['fairlight-strip-properties']['1301.0'].is_split = 0x01
    root = ET.Element('Profile')
    _build_fairlight(root, mx)
    assert _canon(_input(root, 1301)) == _canon(_input(root_fx, 1301))


def test_save_then_apply_gives_each_half_its_values_back(saved):
    conn = _RecConn()
    _apply_fairlight(conn, saved, ApplyResult())
    strips = {(c.source, c.channel): (c.volume, c.balance)
              for c in conn.code('CFSP') if c.volume is not None}
    assert strips == {(1, 0): (-1000, -5000), (1, 1): (-2000, 5000),
                      (2, -1): (-10000, 0), (1301, 0): (-10000, 0)}
