# SPDX-License-Identifier: LGPL-3.0-only
"""Migration tests for the ``fade_to_black`` feature.

Phase 3 commit 3: FtB setters + readers moved from
atemwire.operations / atemwire.state into atemwire.messages.fade_to_black.

  Bucket A (1): fade_to_black trigger.
  Bucket B (2): set_ftb_rate (reads mixerstate.video-mode for fps),
                set_ftb_disabled (semantic flip — disabled → enable).
  Bucket A (5): ftb_active, ftb_in_transition, ftb_frames_remaining,
                ftb_rate, ftb_disabled — small mixerstate reads, kept
                as named functions so the snapshot assembler doesn't
                inline three lines of mx.get().

This commit also lifted ``display_fps`` from state.py to _state.py
(rate-resolution machinery needed by many later feature migrations).

The +8 header offset (Send.get_command prepends 8 bytes) applies
below.
"""



from atemwire.messages.fade_to_black import (
    fade_to_black, ftb_active, ftb_disabled, ftb_frames_remaining,
    ftb_in_transition, ftb_rate, set_ftb_disabled, set_ftb_rate
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


class _FtbState:
    def __init__(self, done=False, transitioning=False, frames_remaining=0):
        self.done = done
        self.transitioning = transitioning
        self.frames_remaining = frames_remaining


class _FtbConfig:
    def __init__(self, rate=25):
        self.rate = rate


class _FtbEnabled:
    def __init__(self, disabled=False):
        self.disabled = disabled


# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------

def test_fade_to_black_triggers_ftba():
    """fade_to_black emits the FtbA packet for the given M/E."""
    conn = _FakeConn()
    fade_to_black(conn, me=1)
    assert conn.sent[0][4:8] == b'FtbA'
    assert conn.sent[0][8] == 1


def test_set_ftb_rate_uses_display_fps_for_parsing():
    """rate '1:00' against a 60fps mixerstate → display fps 30 (halved
    per ASC convention) → 30 frames packed into FtbC.

    FtbC layout: mask=1 at offset 0, M/E index at offset 1, frames as
    u8 at offset 2."""
    conn = _FakeConn(mixerstate={'video-mode': _VideoMode(rate=60)})
    set_ftb_rate(conn, rate_str='1:00')
    payload = conn.sent[0][8:]
    assert payload[0] == 1   # mask (always 1)
    assert payload[1] == 0   # M/E index
    assert payload[2] == 30  # frames (u8)


def test_set_ftb_rate_defaults_to_25_when_video_mode_missing():
    """No video-mode field → display_fps falls back to 25."""
    conn = _FakeConn()
    set_ftb_rate(conn, rate_str='1:00')
    payload = conn.sent[0][8:]
    assert payload[2] == 25


def test_set_ftb_disabled_inverts_to_enable():
    """Frontend passes disabled=True → FEna packet carries enable=False."""
    conn = _FakeConn()
    set_ftb_disabled(conn, disabled=True)
    payload = conn.sent[0][8:]
    # FEna enable byte is at offset 1 (per the wire-format class).
    assert payload[1] == 0   # enable=False


def test_set_ftb_disabled_inverts_to_enable_when_false():
    conn = _FakeConn()
    set_ftb_disabled(conn, disabled=False)
    payload = conn.sent[0][8:]
    assert payload[1] == 1   # enable=True


def test_set_ftb_disabled_pins_me1_for_any_me():
    """FEna sends are pinned to ME1 (index 0) regardless of ``me``.

    The disable packet carries the M/E index at the byte where the enable
    packet carries the 0x01 enable marker, so a me>0 disable would be
    byte-identical to an ME1 enable — a latent misfire on multi-M/E.
    Until a multi-M/E capture settles the per-M/E layout there must be
    NO me>0 send path."""
    for me in (0, 1, 2, 3):
        conn = _FakeConn()
        set_ftb_disabled(conn, disabled=True, me=me)
        payload = conn.sent[0][8:]
        # Disable layout [00 <mE> 00 xx]: index byte must always be 0.
        assert payload == b'\x00\x00\x00\x00'

        conn = _FakeConn()
        set_ftb_disabled(conn, disabled=False, me=me)
        payload = conn.sent[0][8:]
        # Enable layout [00 01 xx xx]: carries no index at all.
        assert payload[:2] == b'\x00\x01'


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def test_ftb_active_reads_done():
    mx = {'fade-to-black-state': {0: _FtbState(done=True)}}
    assert ftb_active(mx, 0) is True
    assert ftb_active(mx, me=1) is False  # missing entry → False


def test_ftb_in_transition_reads_transitioning():
    mx = {'fade-to-black-state': {0: _FtbState(transitioning=True)}}
    assert ftb_in_transition(mx, 0) is True


def test_ftb_frames_remaining():
    mx = {'fade-to-black-state': {0: _FtbState(frames_remaining=12)}}
    assert ftb_frames_remaining(mx, 0) == 12


def test_ftb_rate_default_25():
    """No FtbP entry → default 25."""
    assert ftb_rate({}) == 25


def test_ftb_rate_reads_from_mixerstate():
    mx = {'fade-to-black': {0: _FtbConfig(rate=60)}}
    assert ftb_rate(mx, 0) == 60


def test_ftb_disabled_singleton_no_me_index():
    """FEna is stored bare in mixerstate (no M/E index in the wire)."""
    mx = {'fade-to-black-enabled': _FtbEnabled(disabled=True)}
    assert ftb_disabled(mx) is True
    assert ftb_disabled({}) is False
