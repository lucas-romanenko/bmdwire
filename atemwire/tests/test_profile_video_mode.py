# SPDX-License-Identifier: LGPL-3.0-only
"""Profile save → apply round-trips the video mode, for every mode.

Save used to write ``VideoModeField.get_label()`` ("1080p59.94", and
"625i50" for both SD aspects) while apply resolved names through
``VIDEO_MODE_NAMES`` ("1080p5994"), so every fractional-rate mode came
back "unrecognized" and the 16:9 SD modes came back as 4:3. Both sides
now use ``VIDEO_MODE_XML_NAMES``; the old spellings are still read.
"""

import struct

import pytest

from atemwire.messages.input_video import (
    VIDEO_MODE_NAMES, VIDEO_MODE_XML_NAMES, VideoModeCommand, VideoModeField,
)
from atemwire.profile import (
    ApplyOptions, MixEffectOptions, Profile, SaveOptions,
)


ALL_MODES = sorted(VideoModeField._MODE_TABLE)

# Everything Profile.from_atem's settle wait looks for, so it returns at once.
_SETTLED = (
    'program-bus-input', 'preview-bus-input', 'transition-settings',
    'aux-output-source', 'key-on-air', 'transition-mix',
    'fairlight-audio-input', 'fairlight-master-properties',
)


class _RecordingConn:
    def __init__(self, mixerstate=None):
        self.sent = []
        self.mixerstate = mixerstate or {}

    def send(self, command):
        self.sent.append(command)


def _mx_at(mode):
    mx = {k: {} for k in _SETTLED}
    mx['video-mode'] = VideoModeField(struct.pack('>B3x', mode))
    return mx


def _video_mode_only_save():
    return SaveOptions(
        mes=[MixEffectOptions.none() for _ in range(4)],
        downstream_keys=False, color_generators=False, fairlight=False,
        camera_control=False, media_pool_metadata=False,
        media_pool_images=False, media_players=False, auxiliaries=False,
        counters=False, settings=False, video_mode=True, hyperdecks=False,
        macros=False,
    )


def _video_mode_only_apply():
    opts = ApplyOptions.macros_only()
    opts.restore_macros = False
    opts.restore_video_mode = True
    return opts


def _sent_modes(conn):
    return [c.mode for c in conn.sent if isinstance(c, VideoModeCommand)]


@pytest.fixture(autouse=True)
def _no_resync_sleep(monkeypatch):
    # apply sleeps 3 s after a mode change to let the switcher resync.
    import time
    monkeypatch.setattr(time, 'sleep', lambda _s: None)


def test_canonical_table_covers_every_mode_once():
    assert sorted(VIDEO_MODE_XML_NAMES) == ALL_MODES
    names = list(VIDEO_MODE_XML_NAMES.values())
    assert len(set(names)) == len(names)
    for mode, name in VIDEO_MODE_XML_NAMES.items():
        assert VIDEO_MODE_NAMES[name] == mode


@pytest.mark.parametrize('mode', ALL_MODES)
def test_saved_mode_applies_as_the_same_mode(mode):
    saved = Profile.from_atem(_RecordingConn(_mx_at(mode)),
                              _video_mode_only_save())
    profile = Profile.from_xml(saved.to_xml())
    assert profile.video_mode == VIDEO_MODE_XML_NAMES[mode]

    # Onto a switcher with no mode reported yet, and onto one at another mode.
    other = 27 if mode != 27 else 12
    for live in ({}, _mx_at(other)):
        conn = _RecordingConn(live)
        result = profile.apply(conn, _video_mode_only_apply())
        assert _sent_modes(conn) == [mode], result.summary()


@pytest.mark.parametrize('mode', ALL_MODES)
def test_already_at_the_saved_mode_sends_nothing(mode):
    saved = Profile.from_atem(_RecordingConn(_mx_at(mode)),
                              _video_mode_only_save())
    conn = _RecordingConn(_mx_at(mode))
    result = saved.apply(conn, _video_mode_only_apply())
    assert _sent_modes(conn) == []
    assert any(s.startswith('video_mode: already at') for s in result.skipped)


@pytest.mark.parametrize('mode', ALL_MODES)
def test_old_label_spellings_still_apply(mode):
    # What atemwire <= 1.2.0 wrote: the display label without its aspect.
    label = VideoModeField(struct.pack('>B3x', mode)).get_label().split(' ')[0]
    profile = Profile.from_xml(
        f'<Profile majorVersion="2" minorVersion="1">'
        f'<VideoMode videoMode="{label}"/></Profile>')
    conn = _RecordingConn()
    profile.apply(conn, _video_mode_only_apply())
    # The old label never recorded the SD aspect: 16:9 SD reads as 4:3.
    expected = {2: 0, 3: 1}.get(mode, mode)
    assert _sent_modes(conn) == [expected]
