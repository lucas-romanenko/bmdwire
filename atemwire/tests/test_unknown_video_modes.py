# SPDX-License-Identifier: LGPL-3.0-only
"""A video mode code atemwire has no table entry for never stops the state
dump.

``VideoModeField`` raised ``ValueError`` on a code outside its table. The
exception left ``AtemProtocol.loop()`` and the rest of that packet was lost.
A switcher that can run 4K or 8K lists every mode it supports in ``_VMC``
even while it runs HD, each block and each multiview / downconvert bit
parsed as a ``VideoModeField``, and ``_VMC`` travels ahead of ``VidM`` in the
state dump. So one mode newer than the table cost the ``VidM`` behind it:
``video-mode`` never arrived, every dump ended in a re-handshake, and every
connect timed out.

Now an unknown code is kept as an unknown mode (``known`` False, label
``mode-<n>``, no frame size or rate), every reader and the profile and macro
paths take it.
"""
import logging
import struct
import types

import pytest

from atemwire.macrotransfer import decode_macro_bytecode, encode_macro_bytecode
from atemwire.messages.input_video import (
    VIDEO_MODE_NAMES, VIDEO_MODE_XML_NAMES, VideoModeCapabilityField,
    VideoModeCommand, VideoModeField, set_video_mode,
)
from atemwire.messages.system_info import (
    available_video_modes, video_mode, video_resolution,
)
from atemwire.profile import (
    ApplyOptions, MixEffectOptions, Profile, SaveOptions,
    download_media_pool_images,
)
from atemwire.protocol import AtemProtocol
from atemwire.ready import wait_ready
from atemwire.state import build_full_state, display_fps
from atemwire.transport import Packet, UdpProtocol


KNOWN = sorted(VideoModeField._MODE_TABLE)
UNKNOWN = [m for m in range(256) if m not in VideoModeField._MODE_TABLE]

# What a UHD switcher on newer firmware might list: its HD and 4K modes plus
# two codes past the table (30, 31) and one far out (200).
UHD_LIST = [4, 5, 28, 6, 7, 29, 8, 9, 10, 11, 26, 12, 13, 27,
            14, 15, 16, 17, 18, 19, 30, 31, 200]


def _vidm(mode):
    return VideoModeField(struct.pack('>B3x', mode))


def _vmc_payload(blocks, v8=True):
    """``_VMC``: u16 count, 2 pad, then per mode u8 mode, 3 pad, u32
    multiview bits, u32 downconvert bits (+ u8 reconfigure from 8.0)."""
    out = struct.pack('>H2x', len(blocks))
    for mode, multiview, downconvert, reconfigure in blocks:
        out += struct.pack('>B3x I I', mode, multiview, downconvert)
        if v8:
            out += bytes([reconfigure])
    return out + bytes(-len(out) % 4)


def _uhd_blocks():
    # Every 4K block offers multiview modes 30 and 31 too, and code 200
    # sits in the list itself: the bits only reach 31.
    bits = (1 << 12) | (1 << 30) | (1 << 31)
    return [(m, bits, 1 << 31, m >= 14) for m in UHD_LIST]


# --- VidM ---------------------------------------------------------------------


def test_an_unknown_vidm_code_parses_as_an_unknown_mode():
    for mode in UNKNOWN:
        vm = _vidm(mode)
        assert vm.mode == mode
        assert vm.known is False
        assert (vm.resolution, vm.interlaced, vm.rate, vm.widescreen) == (None,) * 4
        assert vm.get_label() == f'mode-{mode}'
        assert vm.get_resolution() is None
        assert vm.get_pixels() is None
        assert repr(vm) == f'<video-mode: mode={mode}: mode-{mode}>'


@pytest.mark.parametrize('mode', KNOWN)
def test_a_known_vidm_code_is_unchanged(mode):
    vm = _vidm(mode)
    assert vm.known is True
    res, inter, rate, ws = VideoModeField._MODE_TABLE[mode]
    assert (vm.resolution, vm.interlaced, vm.rate, vm.widescreen) == (res, inter, rate, ws)
    w, h = vm.get_resolution()
    assert h in (480, 576, 720, 1080, 2160, 4320)
    assert vm.get_pixels() == w * h
    assert not vm.get_label().startswith('mode-')


# --- _VMC ---------------------------------------------------------------------


def test_vmc_with_unknown_codes_keeps_every_mode():
    cap = VideoModeCapabilityField(_vmc_payload(_uhd_blocks()))
    assert [m['modenum'] for m in cap.modes] == UHD_LIST
    by_num = {m['modenum']: m for m in cap.modes}
    for code in (30, 31, 200):
        assert by_num[code]['mode'].known is False
        assert by_num[code]['mode'].get_label() == f'mode-{code}'
    assert by_num[18]['mode'].get_label() == '2160p50'
    # The bitfields: 12 is known, 30 and 31 are not, and none of them raise.
    assert [v.mode for v in by_num[10]['multiview']] == [12, 30, 31]
    assert [v.known for v in by_num[10]['multiview']] == [True, False, False]
    assert [v.mode for v in by_num[10]['downscale']] == [31]
    assert by_num[14]['reconfigure'] is True
    assert by_num[10]['reconfigure'] is False
    assert 'mode-31' in repr(cap)


def test_vmc_before_protocol_8_has_12_byte_blocks():
    # No requires-reconfiguration byte before 8.0 (atem-connection's
    # VideoMixerConfigCommand); a 13-byte stride read the third block at
    # the wrong offset and the last one past the end.
    blocks = [(10, 1 << 6, 1 << 10, False), (12, 1 << 12, 1 << 12, False),
              (13, 1 << 7, 1 << 13, False)]
    cap = VideoModeCapabilityField(_vmc_payload(blocks, v8=False))
    assert [m['modenum'] for m in cap.modes] == [10, 12, 13]
    assert [[v.mode for v in m['multiview']] for m in cap.modes] == [[6], [12], [7]]
    assert [[v.mode for v in m['downscale']] for m in cap.modes] == [[10], [12], [13]]
    assert [m['reconfigure'] for m in cap.modes] == [False] * 3


@pytest.mark.parametrize('cut', [0, 1, 3, 5, 17, 30])
def test_a_vmc_cut_short_parses_what_it_holds(cut):
    raw = _vmc_payload(_uhd_blocks())[:cut]
    cap = VideoModeCapabilityField(raw)
    assert [m['modenum'] for m in cap.modes] == UHD_LIST[:len(cap.modes)]


# --- the state dump, through the real packet loop -----------------------------


def _field(code, payload):
    return struct.pack('>H2x4s', 8 + len(payload), code) + payload


TOP = _field(b'_top', bytes(16))
INCM = _field(b'InCm', b'\x01\x00\x00\x00')
TIME = _field(b'Time', bytes(8))


def _vidm_field(mode):
    return _field(b'VidM', bytes([mode, 0, 0, 0]))


def _packet(*fields):
    pkt = Packet()
    pkt.flags = UdpProtocol.FLAG_RELIABLE
    pkt.session = 0x8001
    pkt.data = b''.join(fields)
    return Packet.from_bytes(pkt.to_bytes())


class _Harness:
    """``AtemProtocol.loop()`` through the real transport's receive path,
    the dump put straight on its queue (as in ``test_incomplete_dump``)."""

    def __init__(self):
        self.p = AtemProtocol('127.0.0.1')
        t = self.t = self.p.transport
        t.state = UdpProtocol.STATE_ESTABLISHED
        t.enable_ack = True
        t.had_traffic = True
        self.calls = []
        self.events = []
        t.close_session = lambda: self.calls.append('close_session')
        t.connect = lambda: self.calls.append('connect')
        for ev in ('connected', 'disconnected'):
            self.p.on(ev, lambda ev=ev: self.events.append(ev))

    def run(self, *packets):
        for pkt in packets:
            self.t.thread_recv_queue.put(pkt)
        for _ in range(10):
            if self.t.thread_recv_queue.empty() and self.t._stashed_packet is None:
                break
            self.p.loop()

    def close(self):
        self.t.sock.close()
        self.t.thread_queue.close()


@pytest.fixture
def harness():
    made = []

    def make():
        h = _Harness()
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def test_a_uhd_capability_list_ahead_of_vidm_completes_the_dump(harness):
    h = harness()
    vmc = _field(b'_VMC', _vmc_payload(_uhd_blocks()))
    h.run(_packet(TOP, vmc, _vidm_field(10), INCM), _packet(TIME))

    assert h.calls == [], 'the dump had its VidM: no re-handshake'
    assert h.events == ['connected']
    assert h.p.initialized is True
    assert h.p.mixerstate['video-mode'].get_label() == '1080p25'
    assert wait_ready(h.p, timeout=0.5) is h.p.mixerstate
    modes = available_video_modes(h.p.mixerstate)
    assert [m['id'] for m in modes] == UHD_LIST
    assert {m['id']: m['label'] for m in modes}[31] == 'mode-31'


def test_an_unknown_running_mode_completes_the_dump(harness):
    h = harness()
    h.run(_packet(TOP, _vidm_field(31), INCM), _packet(TIME))

    assert h.calls == []
    assert h.events == ['connected']
    vm = h.p.mixerstate['video-mode']
    assert (vm.mode, vm.known) == (31, False)
    assert wait_ready(h.p, timeout=0.5) is h.p.mixerstate


def test_transfer_progress_skips_an_unknown_mode(harness):
    # Progress is the share of one frame, and an unknown mode has no frame
    # size: get_pixels() * 4 was a TypeError mid-packet.
    h = harness()
    p = h.p
    p.send_commands = lambda commands: None
    progress = []
    p.on('transfer-progress', lambda *a: progress.append(a))
    chunk = _field(b'FTDa', struct.pack('>HH', 7, 4) + b'abcd')

    for mode, expect in ((31, 0), (10, 1)):
        p.mixerstate['video-mode'] = _vidm(mode)
        p.transfer = types.SimpleNamespace(tid=7, store=0, slot=0)
        p.transfer_packets = 19
        p.transfer_buffer, p.transfer_buffer_bytes = [], 0
        progress.clear()
        h.run(_packet(chunk))
        assert p.transfer_packets == 20
        assert len(progress) == expect, mode


# --- readers and the snapshot -------------------------------------------------


def test_readers_take_an_unknown_mode():
    mx = {'video-mode': _vidm(31),
          'video-mode-capability': VideoModeCapabilityField(
              _vmc_payload(_uhd_blocks()))}
    assert video_mode(mx) == {'format': 'mode-31', 'id': 31}
    assert video_resolution(mx) == (1920, 1080)
    assert video_resolution(mx, default=None) is None
    assert display_fps(mx) == 25
    assert [m['id'] for m in available_video_modes(mx)] == UHD_LIST


def test_build_full_state_takes_an_unknown_mode():
    mx = {'video-mode': _vidm(31),
          'video-mode-capability': VideoModeCapabilityField(
              _vmc_payload(_uhd_blocks()))}
    conn = types.SimpleNamespace(mixerstate=mx, last_run_macro_index=-1)
    state = build_full_state(conn)
    assert state['videoMode'] == {'format': 'mode-31', 'id': 31, 'fps': 25}
    labels = {m['id']: m['label'] for m in state['videoModes']}
    assert labels[31] == 'mode-31' and labels[19] == '2160p59.94'


# --- every table, every code --------------------------------------------------


def test_every_table_covers_the_same_modes():
    assert sorted(VIDEO_MODE_XML_NAMES) == KNOWN
    assert sorted(set(VIDEO_MODE_NAMES.values())) == KNOWN


class _RecordingConn:
    def __init__(self, mixerstate=None):
        self.sent = []
        self.mixerstate = mixerstate or {}

    def send(self, command):
        self.sent.append(command)


def _pack_op(op_code, params):
    return struct.pack('<HH', 4 + len(params), op_code) + params


@pytest.mark.parametrize('mode', KNOWN)
def test_each_mode_round_trips_through_every_table(mode):
    name = VIDEO_MODE_XML_NAMES[mode]
    assert VIDEO_MODE_NAMES[name] == mode
    # CVdM by name and by number
    conn = _RecordingConn()
    set_video_mode(conn, name)
    set_video_mode(conn, mode)
    assert [c.mode for c in conn.sent] == [mode, mode]
    assert VideoModeCommand(mode=mode).get_command()[8] == mode
    # _VMC block and bitfield
    cap = VideoModeCapabilityField(
        _vmc_payload([(mode, 1 << mode, 1 << mode, False)]))
    entry = cap.modes[0]
    assert entry['mode'].mode == mode and entry['mode'].known
    assert [v.mode for v in entry['multiview']] == [mode]
    # macro VideoMode op, both ways
    raw = _pack_op(0x000C, bytes([mode, 0, 0, 0]))
    assert decode_macro_bytecode(raw) == [{'id': 'VideoMode', 'videoMode': name}]
    assert encode_macro_bytecode([{'id': 'VideoMode', 'videoMode': name}]) == raw


def test_macro_video_mode_op_with_an_unknown_mode():
    raw = _pack_op(0x000C, bytes([31, 0, 0, 0]))
    assert decode_macro_bytecode(raw) == [{'id': 'VideoMode', 'videoMode': '31'}]
    for spelling in ('31', 'mode-31'):
        assert encode_macro_bytecode(
            [{'id': 'VideoMode', 'videoMode': spelling}]) == raw


# --- profile save and apply ---------------------------------------------------

_SETTLED = (
    'program-bus-input', 'preview-bus-input', 'transition-settings',
    'aux-output-source', 'key-on-air', 'transition-mix',
    'fairlight-audio-input', 'fairlight-master-properties',
)


def _mx_at(mode):
    mx = {k: {} for k in _SETTLED}
    mx['video-mode'] = _vidm(mode)
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


@pytest.fixture
def no_resync_sleep(monkeypatch):
    import time
    monkeypatch.setattr(time, 'sleep', lambda _s: None)


def _apply_xml(name, live=None):
    profile = Profile.from_xml(
        f'<Profile majorVersion="2" minorVersion="1">'
        f'<VideoMode videoMode="{name}"/></Profile>')
    conn = _RecordingConn(live)
    result = profile.apply(conn, _video_mode_only_apply())
    return _sent_modes(conn), result


def test_save_at_an_unknown_mode_round_trips(no_resync_sleep):
    saved = Profile.from_atem(_RecordingConn(_mx_at(31)), _video_mode_only_save())
    profile = Profile.from_xml(saved.to_xml())
    assert profile.video_mode == 'mode-31'

    conn = _RecordingConn(_mx_at(10))
    profile.apply(conn, _video_mode_only_apply())
    assert _sent_modes(conn) == [31]

    conn = _RecordingConn(_mx_at(31))
    result = profile.apply(conn, _video_mode_only_apply())
    assert _sent_modes(conn) == []
    assert any(s.startswith('video_mode: already at') for s in result.skipped)


def test_apply_onto_a_switcher_at_an_unknown_mode(no_resync_sleep):
    sent, _ = _apply_xml('1080p50', live=_mx_at(31))
    assert sent == [12]


@pytest.mark.parametrize('name', ['mode-', 'mode-x', 'mode-256', 'mode-\u0663', 'banana'])
def test_apply_skips_a_name_it_cannot_read(no_resync_sleep, name):
    sent, result = _apply_xml(name, live=_mx_at(10))
    assert sent == []
    assert any('unrecognized mode' in s for s in result.skipped)


def test_media_pool_images_at_an_unknown_mode_downloads_nothing(caplog):
    # The frame size is unknown, so a download could not be decoded.
    calls = []

    class _Conn:
        is_ready = True
        mixerstate = {
            'video-mode': _vidm(31),
            'mediaplayer-file-info': {
                0: types.SimpleNamespace(is_used=True, name=b'one',
                                         hash=b'\x01' * 16),
            },
        }

        def download_still(self, slot, timeout=None):
            calls.append(slot)
            return b''

    with caplog.at_level(logging.WARNING, logger='atemwire.profile.save'):
        assert download_media_pool_images(_Conn()) == []
    assert calls == []
    assert any('mode-31' in r.getMessage() for r in caplog.records)
