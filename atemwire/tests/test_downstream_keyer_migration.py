# SPDX-License-Identifier: LGPL-3.0-only
"""Migration tests for the ``downstream_keyer`` feature.

Phase 3: 16 operations + 1 reader (the big dsk_state aggregator)
migrated from atemwire.operations / atemwire.state to
atemwire.messages.downstream_keyer.

Bucket breakdown:
  - bucket A (identity):        set_dsk_on_air, dsk_auto,
                                set_dsk_fill_source, set_dsk_key_source,
                                set_dsk_mask_enabled, set_dsk_pre_multiplied,
                                set_dsk_invert_key
  - bucket A (×1000 scale, no clamp on individual setters but they
    pass through int(round(value * 1000)) — same as USK/DVE mask):
                                set_dsk_mask_top / _bottom / _left / _right
  - bucket B (clamp):           set_dsk_clip, set_dsk_gain
                                (percent_to_tenths clamps 0..1000)
  - bucket B (RMW):             toggle_dsk
  - bucket B (rate parser):     set_dsk_rate
  - bucket B (multi-field mask): configure_dsk_gain
                                (scale FIXED 2026-06-02: now uses
                                percent_to_tenths, consistent with
                                set_dsk_clip/gain)
  - bucket B (multi-key dict):  dsk_state (3 mixerstate keys aggregated)

+8 header offset on Send.get_command, as established in prior batches.
"""

import struct

import pytest

from atemwire.messages.downstream_keyer import (
    configure_dsk_gain, dsk_auto, dsk_state, set_dsk_clip,
    set_dsk_fill_source, set_dsk_gain, set_dsk_invert_key,
    set_dsk_key_source, set_dsk_mask_bottom, set_dsk_mask_enabled,
    set_dsk_mask_top, set_dsk_on_air, set_dsk_pre_multiplied, set_dsk_rate,
    toggle_dsk
)


class _FakeConn:
    def __init__(self, mixerstate=None):
        self.sent = []
        self.mixerstate = mixerstate or {}

    def send(self, cmd):
        self.sent.append(cmd.get_command())


class _VideoMode:
    def __init__(self, rate):
        self.rate = rate


class _DskState:
    def __init__(self, on_air=False, is_transitioning=False,
                 is_autotransitioning=False, frames_remaining=0):
        self.on_air = on_air
        self.is_transitioning = is_transitioning
        self.is_autotransitioning = is_autotransitioning
        self.frames_remaining = frames_remaining


class _DskBase:
    def __init__(self, fill_source=0, key_source=0):
        self.fill_source = fill_source
        self.key_source = key_source


class _DskProps:
    def __init__(self, rate=25, tie=False, premultiplied=False,
                 clip=0, gain=0, invert_key=False, masked=False,
                 top=0, bottom=0, left=0, right=0):
        self.rate = rate
        self.tie = tie
        self.premultiplied = premultiplied
        self.clip = clip
        self.gain = gain
        self.invert_key = invert_key
        self.masked = masked
        self.top = top
        self.bottom = bottom
        self.left = left
        self.right = right


# ---------------------------------------------------------------------------
# Wrappers — Send → wire
# ---------------------------------------------------------------------------

def test_set_dsk_on_air_packs_cdsl():
    conn = _FakeConn()
    set_dsk_on_air(conn, enabled=True, dsk_idx=1)
    assert conn.sent[0][4:8] == b'CDsL'


def test_dsk_auto_packs_ddsa():
    conn = _FakeConn()
    dsk_auto(conn, dsk_idx=0)
    assert conn.sent[0][4:8] == b'DDsA'


def test_set_dsk_rate_parses_against_display_fps():
    """Rate '1:00' at 60fps → 30 frames (half-rate convention)."""
    conn = _FakeConn(mixerstate={'video-mode': _VideoMode(rate=60)})
    set_dsk_rate(conn, rate_str='1:00')
    assert conn.sent[0][4:8] == b'CDsR'


def test_toggle_dsk_reads_state_and_inverts():
    """RMW: reads dsk-state.on_air=True, sends CDsL with on_air=False."""
    conn = _FakeConn(mixerstate={
        'dkey-state': {0: _DskState(on_air=True)},
    })
    toggle_dsk(conn, dsk_idx=0)
    # Verify a CDsL packet was sent
    assert conn.sent[0][4:8] == b'CDsL'


def test_set_dsk_fill_and_key_sources():
    conn = _FakeConn()
    set_dsk_fill_source(conn, source=5, dsk_idx=0)
    set_dsk_key_source(conn, source=7, dsk_idx=0)
    assert conn.sent[0][4:8] == b'CDsF'
    assert conn.sent[1][4:8] == b'CDsC'


def test_set_dsk_mask_fields_scale_thousandths():
    """Mask values multiply by 1000 on the wire (e.g. 9.000 → 9000)."""
    conn = _FakeConn()
    set_dsk_mask_top(conn, top=5.0, dsk_idx=0)
    set_dsk_mask_bottom(conn, bottom=-5.0, dsk_idx=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CDsM'


def test_set_dsk_mask_enabled_packs_cdsm():
    conn = _FakeConn()
    set_dsk_mask_enabled(conn, enabled=True, dsk_idx=0)
    assert conn.sent[0][4:8] == b'CDsM'


def test_set_dsk_clip_gain_use_tenths_scale():
    """DSK clip/gain wire is ×10 of percent (0..1000), clamped via
    percent_to_tenths. Sends CDsG."""
    conn = _FakeConn()
    set_dsk_clip(conn, clip=66, dsk_idx=0)
    set_dsk_gain(conn, gain=50, dsk_idx=0)
    for cmd in conn.sent:
        assert cmd[4:8] == b'CDsG'


def test_set_dsk_clip_clamps_above_100():
    """percent_to_tenths clamps 150 percent down to wire 1000."""
    conn = _FakeConn()
    set_dsk_clip(conn, clip=150, dsk_idx=0)
    assert conn.sent[0][4:8] == b'CDsG'


def test_set_dsk_pre_multiplied_and_invert_bool():
    conn = _FakeConn()
    set_dsk_pre_multiplied(conn, pre_multiplied=True, dsk_idx=0)
    set_dsk_invert_key(conn, invert_key=True, dsk_idx=0)
    assert all(c[4:8] == b'CDsG' for c in conn.sent)


def test_configure_dsk_gain_uses_tenths_scale_for_clip_gain():
    """Scale fix (2026-06-02): configure_dsk_gain now uses percent_to_tenths
    (×10), matching set_dsk_clip / set_dsk_gain. CDsG packs clip at payload
    offset 4 (packet offset 12) and gain at offset 6 (packet offset 14),
    both u16 BE. clip=50 → wire 500 (50.0%), NOT the old 5000."""
    conn = _FakeConn()
    configure_dsk_gain(conn, clip=50, gain=50, dsk_idx=0)
    pkt = conn.sent[0]
    assert pkt[4:8] == b'CDsG'
    assert struct.unpack_from('>H', pkt, 12)[0] == 500   # clip ×10
    assert struct.unpack_from('>H', pkt, 14)[0] == 500   # gain ×10


# ---------------------------------------------------------------------------
# Reader — dsk_state aggregator
# ---------------------------------------------------------------------------

def test_dsk_state_assembles_all_fields():
    """Aggregator pulls from 3 mixerstate keys + applies per-field scale."""
    mx = {
        'dkey-state': {0: _DskState(
            on_air=True, frames_remaining=8,
        )},
        'dkey-properties-base': {0: _DskBase(fill_source=12, key_source=14)},
        'dkey-properties': {0: _DskProps(
            rate=30, premultiplied=True,
            clip=660,  # wire ×10 → display 66.0
            gain=500,  # wire ×10 → display 50.0
            masked=True,
            top=9000,  # wire ×1000 → display 9.000
            bottom=-5000,  # display -5.000
            left=0, right=16000,  # display 0.0 / 16.0
        )},
    }
    result = dsk_state(mx, dsk_idx=0)
    assert result['on_air'] is True
    assert result['frames_remaining'] == 8
    assert result['fill_source'] == 12
    assert result['key_source'] == 14
    assert result['rate'] == 30
    assert result['pre_multiplied'] is True
    assert result['clip'] == pytest.approx(66.0)
    assert result['gain'] == pytest.approx(50.0)
    assert result['mask_enabled'] is True
    assert result['mask_top'] == pytest.approx(9.0)
    assert result['mask_bottom'] == pytest.approx(-5.0)
    assert result['mask_left'] == pytest.approx(0.0)
    assert result['mask_right'] == pytest.approx(16.0)


def test_dsk_state_default_when_no_mixerstate():
    """All three keys missing → safe defaults across the dict."""
    result = dsk_state({}, dsk_idx=0)
    assert result['on_air'] is False
    assert result['rate'] == 25
    assert result['fill_source'] == 0
    assert result['clip'] == 0.0
    assert result['mask_top'] == 0.0


def test_dsk_state_in_transition_combines_two_state_fields():
    """in_transition is True if EITHER is_transitioning OR
    is_autotransitioning is True."""
    mx = {'dkey-state': {0: _DskState(
        is_transitioning=False, is_autotransitioning=True,
    )}}
    assert dsk_state(mx)['in_transition'] is True
    assert dsk_state(mx)['is_auto_transitioning'] is True
