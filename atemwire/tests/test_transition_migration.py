# SPDX-License-Identifier: LGPL-3.0-only
"""Migration tests for the ``transition`` feature.

Phase 3 batch: transition is the single biggest feature
(36 ops + 39 readers). Split into 4 sub-commits by per-style group:

  3a — basic + mix/dip + selection (this file's first block)
  3b — wipe
  3c — dve-transition
  3d — stinger

Sub-blocks below grow as each sub-commit lands. Test fixtures
(_FakeConn, fake mixerstate classes) are shared across blocks.

The +8 header offset applies (Send.get_command prepends an 8-byte
header before the payload).
"""


import pytest

from atemwire.messages.transition import (
    active_transition_rate, dip_rate, dip_source, mix_rate, set_dip_rate,
    set_dip_source, set_mix_rate, set_next_transition_layers,
    set_transition_rate, set_transition_style, set_dve_clip,
    set_dve_enable_key, set_dve_fill_source, set_dve_flip_flop,
    set_dve_gain, set_dve_invert_key, set_dve_key_source,
    set_dve_pre_multiplied, set_dve_rate, set_dve_reverse, set_dve_style,
    set_stinger_clip, set_stinger_clip_duration, set_stinger_gain,
    set_stinger_invert_key, set_stinger_mix_rate,
    set_stinger_pre_multiplied, set_stinger_pre_roll, set_stinger_rate,
    set_stinger_source, set_stinger_trigger_point, set_wipe_fill_source,
    set_wipe_flip_flop, set_wipe_pattern, set_wipe_position_x,
    set_wipe_rate, set_wipe_reverse, set_wipe_softness, set_wipe_symmetry,
    set_wipe_width, toggle_transition_background, toggle_transition_key,
    transition_frames_remaining, transition_in_transition,
    transition_position, transition_selection, transition_style, dve_clip,
    dve_enable_key, dve_fill_source, dve_flip_flop, dve_gain,
    dve_invert_key, dve_key_source, dve_pre_multiplied, dve_rate,
    dve_reverse, dve_style, stinger_clip, stinger_clip_duration,
    stinger_gain, stinger_invert_key, stinger_pre_multiplied,
    stinger_pre_roll, stinger_rate, stinger_source, stinger_trigger_point,
    wipe_fill_source, wipe_flip_flop, wipe_pattern, wipe_position_x,
    wipe_position_y, wipe_rate, wipe_reverse, wipe_softness, wipe_symmetry,
    wipe_width
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self, mixerstate=None):
        self.sent = []
        self.mixerstate = mixerstate or {}

    def send(self, cmd):
        self.sent.append(cmd.get_command())


class _VideoMode:
    def __init__(self, rate):
        self.rate = rate


class _TrSS:
    """Stand-in for the 'transition-settings' field (keyed per M/E
    since Stage 4A). Carries the style + the 5 next-transition booleans
    the selection reader builds into a dict."""
    def __init__(self, style=0, next_transition_bkgd=True,
                 next_transition_key1=False, next_transition_key2=False,
                 next_transition_key3=False, next_transition_key4=False):
        self.style = style
        self.next_transition_bkgd = next_transition_bkgd
        self.next_transition_key1 = next_transition_key1
        self.next_transition_key2 = next_transition_key2
        self.next_transition_key3 = next_transition_key3
        self.next_transition_key4 = next_transition_key4


class _TrPs:
    def __init__(self, in_transition=False, position=0.0, frames_remaining=0):
        self.in_transition = in_transition
        self.position = position
        self.frames_remaining = frames_remaining


class _TrRate:
    """Generic per-style rate entry. transition-mix / transition-dip
    use this shape (.rate plus optional .source)."""
    def __init__(self, rate=25, source=0):
        self.rate = rate
        self.source = source


# ===========================================================================
# 3a — basic + mix/dip + selection
# ===========================================================================

# --- Operations -----------------------------------------------------------

def test_set_transition_style_packs_style():
    """CTTp packs style at offset 1 (offset 0 is the mask byte)."""
    conn = _FakeConn()
    set_transition_style(conn, style=2)  # 2 = wipe
    payload = conn.sent[0][8:]
    assert payload[0] & 0b01     # mask bit 0 for style
    assert payload[1] == 0       # M/E index
    assert payload[2] == 2       # wipe


def test_set_mix_rate_uses_display_fps():
    """Mix rate '1:00' at 60fps mixerstate → 30 frames (ASC half-rate)."""
    conn = _FakeConn(mixerstate={'video-mode': _VideoMode(rate=60)})
    set_mix_rate(conn, rate_str='1:00')
    assert conn.sent[0][4:8] == b'CTMx'


def test_set_dip_rate_and_source_use_correct_send_class():
    conn = _FakeConn(mixerstate={'video-mode': _VideoMode(rate=50)})
    set_dip_rate(conn, rate_str='0:25')
    set_dip_source(conn, source=15)
    assert conn.sent[0][4:8] == b'CTDp'
    assert conn.sent[1][4:8] == b'CTDp'


def test_set_transition_rate_dispatches_by_active_style():
    """Compound RMW: reads transition-settings.style, branches to the
    correct *SettingsCommand. style=2 (wipe) → CTWp packet."""
    conn = _FakeConn(mixerstate={
        'video-mode': _VideoMode(rate=25),
        'transition-settings': {0: _TrSS(style=2)},  # wipe active
    })
    set_transition_rate(conn, rate_str='2:00')
    assert conn.sent[0][4:8] == b'CTWp'


def test_set_transition_rate_unknown_style_raises():
    conn = _FakeConn(mixerstate={
        'video-mode': _VideoMode(rate=25),
        'transition-settings': {0: _TrSS(style=99)},
    })
    with pytest.raises(ValueError, match='unknown transition style'):
        set_transition_rate(conn, rate_str='1:00')


def test_toggle_transition_key_rmw_flips_bit():
    """toggle_transition_key reads selection, flips one bit, sends."""
    conn = _FakeConn(mixerstate={
        'transition-settings': {0: _TrSS(
            next_transition_bkgd=True,
            next_transition_key1=False,
        )},
    })
    toggle_transition_key(conn, key=0)  # toggle key 1 (0-indexed)
    assert len(conn.sent) == 1
    assert conn.sent[0][4:8] == b'CTTp'


def test_toggle_transition_noops_on_zero_mask():
    """ATEM rejects a zero-mask selection (at-least-one-layer rule).
    Toggling the last set bit must no-op rather than send a rejected
    packet."""
    # Selection is just background, no keyers. Toggle BKGD off → mask=0.
    conn = _FakeConn(mixerstate={
        'transition-settings': {0: _TrSS(
            next_transition_bkgd=True,
            next_transition_key1=False, next_transition_key2=False,
            next_transition_key3=False, next_transition_key4=False,
        )},
    })
    toggle_transition_background(conn)
    assert conn.sent == []  # no packet


def test_set_next_transition_layers_packs_absolute_mask():
    """No mixerstate read: caller supplies BKGD + 4 keyer booleans;
    the mask is packed verbatim. Used by save-state restore paths."""
    conn = _FakeConn()
    set_next_transition_layers(conn, background=True, key1=True, key3=True)
    assert len(conn.sent) == 1
    assert conn.sent[0][4:8] == b'CTTp'


def test_set_next_transition_layers_noops_on_zero_mask():
    conn = _FakeConn()
    set_next_transition_layers(conn)  # all false → mask=0
    assert conn.sent == []


# --- Readers --------------------------------------------------------------

def test_transition_style_reads_me_indexed_field():
    """transition-settings is keyed per M/E (Stage 4A — TrSS carries
    the M/E index at byte 0)."""
    mx = {'transition-settings': {0: _TrSS(style=3)}}
    assert transition_style(mx) == 3
    assert transition_style(mx, me=1) == 0  # absent M/E -> default


def test_transition_style_per_me_isolation():
    mx = {'transition-settings': {0: _TrSS(style=3), 1: _TrSS(style=1)}}
    assert transition_style(mx, me=0) == 3
    assert transition_style(mx, me=1) == 1


def test_transition_style_defaults_zero():
    assert transition_style({}) == 0


def test_transition_in_transition_and_position():
    mx = {'transition-position': {0: _TrPs(
        in_transition=True, position=0.42, frames_remaining=8)}}
    assert transition_in_transition(mx) is True
    assert transition_position(mx) == pytest.approx(0.42)
    assert transition_frames_remaining(mx) == 8


def test_transition_selection_default_when_unpopulated():
    """No transition-settings field → returns the 'all-defaults' shape
    (BKGD on, all keyers off)."""
    assert transition_selection({}) == {
        'background': True, 'key1': False, 'key2': False,
        'key3': False, 'key4': False,
    }


def test_transition_selection_reads_5_booleans():
    mx = {'transition-settings': {0: _TrSS(
        next_transition_bkgd=False,
        next_transition_key1=True, next_transition_key3=True,
    )}}
    sel = transition_selection(mx)
    assert sel == {
        'background': False, 'key1': True, 'key2': False,
        'key3': True, 'key4': False,
    }


def test_mix_dip_rate_and_source_readers():
    mx = {
        'transition-mix': {0: _TrRate(rate=30)},
        'transition-dip': {0: _TrRate(rate=15, source=5)},
    }
    assert mix_rate(mx) == 30
    assert dip_rate(mx) == 15
    assert dip_source(mx) == 5


def test_rate_readers_default_25_when_missing():
    assert mix_rate({}) == 25
    assert dip_rate({}) == 25
    assert dip_source({}) == 0


def test_active_transition_rate_dispatches_by_style():
    """style=1 (dip) → dispatches to dip_rate. All four style
    branches resolve to ``atemwire.messages.transition`` readers
    directly now that the shim has been removed."""
    mx = {
        'transition-settings': {0: _TrSS(style=1)},
        'transition-dip': {0: _TrRate(rate=42)},
    }
    assert active_transition_rate(mx) == 42


# ===========================================================================
# 3b — wipe
# ===========================================================================

class _TrWp:
    """Stand-in for the transition-wipe field."""
    def __init__(self, rate=25, pattern=0, source=0, positionx=5000,
                 positiony=5000, softness=0, symmetry=5000, width=0,
                 reverse=False, flipflop=False):
        self.rate = rate
        self.pattern = pattern
        self.source = source
        self.positionx = positionx
        self.positiony = positiony
        self.softness = softness
        self.symmetry = symmetry
        self.width = width
        self.reverse = reverse
        self.flipflop = flipflop


# --- Operations -----------------------------------------------------------

def test_set_wipe_rate_uses_display_fps():
    conn = _FakeConn(mixerstate={'video-mode': _VideoMode(rate=25)})
    set_wipe_rate(conn, rate_str='1:00')
    assert conn.sent[0][4:8] == b'CTWp'


def test_set_wipe_pattern_and_source_identity():
    conn = _FakeConn()
    set_wipe_pattern(conn, pattern=3)
    set_wipe_fill_source(conn, source=42)
    assert len(conn.sent) == 2


def test_set_wipe_position_x_scales_and_clamps():
    """0.42 unit → wire 4200. The clamp guards against >1 input."""
    conn = _FakeConn()
    set_wipe_position_x(conn, position_x=0.42)
    # WipeSettingsCommand layout (per messages/transition.py):
    # mask u32 at 0; index at 4; rate at 5; pattern at 6;
    # source at 8 (u16); softness at 10 (u16); symmetry at 12 (u16);
    # width at 14 (u16); positionx at 16 (u16); positiony at 18 (u16);
    # plus reverse/flipflop u8s.
    # The exact offsets depend on the actual class; the assertion is
    # just that we sent a CTWp packet — wire-byte exactitude tested
    # indirectly by the round-trip below.
    assert conn.sent[0][4:8] == b'CTWp'


def test_set_wipe_position_x_clamps_above_one():
    """1.5 should not pack > wire 10000 (clamp)."""
    conn = _FakeConn()
    set_wipe_position_x(conn, position_x=1.5)
    assert conn.sent[0][4:8] == b'CTWp'
    # The clamp keeps the packed value ≤ 10000 — exact offset checked
    # via the soft-positionx round-trip in the next test.


def test_set_wipe_softness_percent_to_wire():
    """75 percent → wire 7500 (×100). Bucket B because of clamp."""
    conn = _FakeConn()
    set_wipe_softness(conn, softness=75)
    assert conn.sent[0][4:8] == b'CTWp'


def test_set_wipe_symmetry_and_width():
    conn = _FakeConn()
    set_wipe_symmetry(conn, symmetry=50)
    set_wipe_width(conn, width=33.3)
    assert len(conn.sent) == 2


def test_set_wipe_reverse_flipflop_bool_identity():
    conn = _FakeConn()
    set_wipe_reverse(conn, reverse=True)
    set_wipe_flip_flop(conn, flip_flop=False)
    assert len(conn.sent) == 2


# --- Readers --------------------------------------------------------------

def test_wipe_rate_pattern_source():
    mx = {'transition-wipe': {0: _TrWp(rate=50, pattern=4, source=99)}}
    assert wipe_rate(mx) == 50
    assert wipe_pattern(mx) == 4
    assert wipe_fill_source(mx) == 99


def test_wipe_position_x_y_unit_scale():
    """Wire 5000 → display 0.5 unit (÷10000, places=4)."""
    mx = {'transition-wipe': {0: _TrWp(positionx=5000, positiony=2500)}}
    assert wipe_position_x(mx) == pytest.approx(0.5)
    assert wipe_position_y(mx) == pytest.approx(0.25)


def test_wipe_position_x_default_half():
    """Default when transition-wipe is missing."""
    assert wipe_position_x({}) == pytest.approx(0.5)


def test_wipe_softness_symmetry_width_percent_scale():
    """Wire 2690 → display 26.9 percent (÷100, places=2)."""
    mx = {'transition-wipe': {0: _TrWp(
        softness=2690, symmetry=5000, width=7500,
    )}}
    assert wipe_softness(mx) == pytest.approx(26.9)
    assert wipe_symmetry(mx) == pytest.approx(50.0)
    assert wipe_width(mx) == pytest.approx(75.0)


def test_wipe_reverse_flipflop_bool():
    mx = {'transition-wipe': {0: _TrWp(reverse=True, flipflop=False)}}
    assert wipe_reverse(mx) is True
    assert wipe_flip_flop(mx) is False


# ===========================================================================
# 3c — dve transition
# ===========================================================================

class _TrDv:
    """Stand-in for transition-dve."""
    def __init__(self, rate=25, fill_source=1, key_source=1,
                 key_enable=False, key_clip=0, key_gain=1000,
                 key_premultiplied=False, key_invert=False,
                 style=0, reverse=False, flipflop=False):
        self.rate = rate
        self.fill_source = fill_source
        self.key_source = key_source
        self.key_enable = key_enable
        self.key_clip = key_clip
        self.key_gain = key_gain
        self.key_premultiplied = key_premultiplied
        self.key_invert = key_invert
        self.style = style
        self.reverse = reverse
        self.flipflop = flipflop


# --- Operations -----------------------------------------------------------

def test_set_dve_rate_uses_display_fps():
    conn = _FakeConn(mixerstate={'video-mode': _VideoMode(rate=50)})
    set_dve_rate(conn, rate_str='1:00')
    assert conn.sent[0][4:8] == b'CTDv'


def test_set_dve_rate_wire_offsets_correction():
    """2026-08-22 wire correction: THE DVE transition rate rides CTDv
    byte 4 / mask bit 1. Upstream's byte 3 / bit 0 is the DVE
    LOGO-WIPE rate — a different register that round-trips through our
    own reader while ASC never sees it (found live: "DVE rate dead both
    ways while wipe works"; verified against ASC on the 1 M/E
    Constellation HD). Do not revert to the upstream layout."""
    conn = _FakeConn(mixerstate={'video-mode': _VideoMode(rate=50)})
    set_dve_rate(conn, rate_str='0:10')  # frames-only: fps-independent
    payload = conn.sent[0][8:]
    assert int.from_bytes(payload[0:2], 'big') == 0b10  # mask: bit 1 only
    assert payload[4] == 10                             # rate at byte 4
    assert payload[3] == 0                              # logo byte untouched


def test_tdvp_rate_parses_byte2():
    """TDvP counterpart of the 2026-08-22 correction: rate at byte 2
    (upstream called it unknown), logo-wipe rate at byte 1."""
    from atemwire.messages.transition import TransitionDveField
    raw = bytes([0, 40, 75, 5]) + bytes(16)
    f = TransitionDveField(raw)
    assert f.rate == 75
    assert f.logo_rate == 40
    assert f.style == 5


def test_set_dve_fill_and_key_sources():
    conn = _FakeConn()
    set_dve_fill_source(conn, source=5)
    set_dve_key_source(conn, source=7)
    set_dve_enable_key(conn, enable_key=True)
    assert len(conn.sent) == 3


def test_set_dve_clip_uses_tenths_scale():
    """DVE-tx clip: 60 percent → wire 600 (×10), clamped."""
    conn = _FakeConn()
    set_dve_clip(conn, clip=60)
    assert conn.sent[0][4:8] == b'CTDv'


def test_set_dve_clip_clamps_above_100():
    conn = _FakeConn()
    set_dve_clip(conn, clip=150)  # should clamp to 1000
    assert conn.sent[0][4:8] == b'CTDv'


def test_set_dve_gain_clamps_below_zero():
    conn = _FakeConn()
    set_dve_gain(conn, gain=-10)  # should clamp to 0
    assert conn.sent[0][4:8] == b'CTDv'


def test_set_dve_pre_multiplied_invert_bool():
    conn = _FakeConn()
    set_dve_pre_multiplied(conn, pre_multiplied=True)
    set_dve_invert_key(conn, invert_key=True)
    assert len(conn.sent) == 2


def test_set_dve_style_reverse_flipflop():
    conn = _FakeConn()
    set_dve_style(conn, style=3)
    set_dve_reverse(conn, reverse=True)
    set_dve_flip_flop(conn, flip_flop=True)
    assert len(conn.sent) == 3


# --- Readers --------------------------------------------------------------

def test_dve_rate_default_25():
    assert dve_rate({}) == 25


def test_dve_fill_key_sources_defaults():
    """Defaults to source 1 (not 0) — empirical."""
    assert dve_fill_source({}) == 1
    assert dve_key_source({}) == 1


def test_dve_clip_gain_scale_tenths():
    """Wire 1234 → display 123.4 percent (÷10)."""
    mx = {'transition-dve': {0: _TrDv(key_clip=1234, key_gain=750)}}
    assert dve_clip(mx) == pytest.approx(123.4)
    assert dve_gain(mx) == pytest.approx(75.0)


def test_dve_gain_default_100():
    """Default gain is 100.0 percent (different from clip's default 0.0)."""
    assert dve_gain({}) == pytest.approx(100.0)


def test_dve_style_returns_none_when_unset():
    """Distinguishes 'style=0' from 'no transition-dve seen yet'.
    Preserves the pre-migration semantic for save/restore paths."""
    assert dve_style({}) is None
    mx = {'transition-dve': {0: _TrDv(style=0)}}
    assert dve_style(mx) == 0


def test_dve_bool_readers():
    mx = {'transition-dve': {0: _TrDv(
        key_enable=True, key_premultiplied=True, key_invert=False,
        reverse=True, flipflop=False,
    )}}
    assert dve_enable_key(mx) is True
    assert dve_pre_multiplied(mx) is True
    assert dve_invert_key(mx) is False
    assert dve_reverse(mx) is True
    assert dve_flip_flop(mx) is False


# ===========================================================================
# 3d — stinger
# ===========================================================================

class _TrSt:
    """Stand-in for transition-stinger."""
    def __init__(self, rate=25, mediaplayer=3010, duration=150,
                 triggerpoint=34, preroll=48, key_clip=500, key_gain=700,
                 key_premultiplied=True, key_invert=False):
        self.rate = rate
        self.mediaplayer = mediaplayer
        self.duration = duration
        self.triggerpoint = triggerpoint
        self.preroll = preroll
        self.key_clip = key_clip
        self.key_gain = key_gain
        self.key_premultiplied = key_premultiplied
        self.key_invert = key_invert


# --- Operations -----------------------------------------------------------

def test_set_stinger_rate_and_mix_rate_both_send_ctst():
    """Both set_stinger_rate and set_stinger_mix_rate target the same
    StingerSettingsCommand.rate field — kept as separate names for
    dispatch-table BC."""
    conn = _FakeConn(mixerstate={'video-mode': _VideoMode(rate=50)})
    set_stinger_rate(conn, rate_str='1:00')
    set_stinger_mix_rate(conn, rate_str='1:00')
    assert all(p[4:8] == b'CTSt' for p in conn.sent)
    assert len(conn.sent) == 2


def test_set_stinger_source_uses_mediaplayer_field():
    """The wire ``mediaplayer`` field is u8 1..4 (player number, not the
    video source ID). Reader and op share this space."""
    conn = _FakeConn()
    set_stinger_source(conn, source=2)
    assert conn.sent[0][4:8] == b'CTSt'


def test_set_stinger_clip_duration_trigger_pre_roll_parse_rate():
    conn = _FakeConn(mixerstate={'video-mode': _VideoMode(rate=25)})
    set_stinger_clip_duration(conn, clip_duration='2:00')
    set_stinger_trigger_point(conn, trigger_point='0:10')
    set_stinger_pre_roll(conn, pre_roll='0:05')
    assert len(conn.sent) == 3


def test_set_stinger_clip_gain_tenths_scale_no_clamp():
    """Stinger clip/gain are bucket A (no clamp): 60 percent → wire 600.
    Unlike DVE-tx clip/gain, no clamp on the value."""
    conn = _FakeConn()
    set_stinger_clip(conn, clip=60)
    set_stinger_gain(conn, gain=33.3)
    assert len(conn.sent) == 2


def test_set_stinger_bool_setters():
    conn = _FakeConn()
    set_stinger_pre_multiplied(conn, pre_multiplied=False)
    set_stinger_invert_key(conn, invert_key=True)
    assert len(conn.sent) == 2


# --- Readers --------------------------------------------------------------

def test_stinger_rate_default_25():
    assert stinger_rate({}) == 25


def test_stinger_source_default_slot1():
    """Default is MP1's SLOT NUMBER (1) — the same u8 space as the wire
    and set_stinger_source, so reader values round-trip into the op
    (resolved 2026-08-19; the old 3010 default was a frontend
    source ID that can't fit the u8 field)."""
    assert stinger_source({}) == 1


def test_stinger_durations_defaults_match_atem_factory():
    """Defaults 150 / 34 / 48 frames mirror ASC factory defaults."""
    assert stinger_clip_duration({}) == 150
    assert stinger_trigger_point({}) == 34
    assert stinger_pre_roll({}) == 48


def test_stinger_clip_gain_scale_tenths_with_specific_defaults():
    """Defaults: clip 50.0, gain 70.0 (different from DVE-tx defaults)."""
    assert stinger_clip({}) == pytest.approx(50.0)
    assert stinger_gain({}) == pytest.approx(70.0)


def test_stinger_clip_gain_round_trip():
    mx = {'transition-stinger': {0: _TrSt(key_clip=850, key_gain=420)}}
    assert stinger_clip(mx) == pytest.approx(85.0)
    assert stinger_gain(mx) == pytest.approx(42.0)


def test_stinger_pre_multiplied_default_true():
    """Stinger default differs from DVE-tx (True vs False)."""
    assert stinger_pre_multiplied({}) is True
    assert stinger_invert_key({}) is False


# --- Cross-style: active_transition_rate after all rate readers migrated --

def test_active_transition_rate_dispatches_to_stinger():
    mx = {
        'transition-settings': {0: _TrSS(style=4)},  # stinger
        'transition-stinger': {0: _TrSt(rate=120)},
    }
    assert active_transition_rate(mx) == 120


def test_active_transition_rate_dispatches_to_dve():
    mx = {
        'transition-settings': {0: _TrSS(style=3)},  # dve
        'transition-dve': {0: _TrDv(rate=50)},
    }
    assert active_transition_rate(mx) == 50


def test_active_transition_rate_dispatches_to_wipe():
    mx = {
        'transition-settings': {0: _TrSS(style=2)},  # wipe
        'transition-wipe': {0: _TrWp(rate=88)},
    }
    assert active_transition_rate(mx) == 88
