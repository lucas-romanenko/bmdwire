# SPDX-License-Identifier: LGPL-3.0-only
"""The upstream send commands restored in 0.15, pinned byte-for-byte.

Every expected payload below was produced by upstream pyatem's own
``Command.get_command()`` at the fork point (commit 8f45831) on the same
arguments, so a passing test means "identical to what upstream would send",
not "verified on a switcher". The two long packets (CRMS, CRSS) are checked
against upstream's ``struct.pack`` format strings instead of a 2 KB hex
literal. ``*XFC`` is the OpenSwitcher TCP-proxy artefact and is not restored.
"""

import datetime
import struct

import pytest

from atemwire.atem import _OPERATIONS
from atemwire.messages import (
    audio_legacy, camera_control, multiviewer, recording, streaming,
    supersource, system_info, transition,
)


def payload(cmd):
    raw = cmd.get_command()
    assert raw[4:8] == cmd.CODE.encode()
    assert struct.unpack('>H', raw[:2])[0] == len(raw)
    return raw[8:]


class _FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)


# ---------------------------------------------------------------------------
# small fixed-layout packets: (command, expected payload hex)
# ---------------------------------------------------------------------------

CASES = [
    (system_info.AutoInputVideoModeCommand(True), '01000000'),
    (audio_legacy.AudioInputCommand(source=1001, balance=-2500, volume=40000, on=True), '070003e901009c40f63c0000'),
    (audio_legacy.AudioInputCommand(source=2, afv=True), '010000020200000000000000'),
    (audio_legacy.AudioInputCommand(source=3, volume=12345), '020000030000303900000000'),
    (audio_legacy.AudioMasterPropertiesCommand(volume=65381, afv=True), '0500ff6500000100'),
    (audio_legacy.AudioMasterPropertiesCommand(afv=False), '0400000000000000'),
    (audio_legacy.AudioMonitorPropertiesCommand(enabled=True, volume=30000, mute=False, solo=True,
                                                solo_source=1002, dim=True, dim_volume=5000), '7f017530000103ea01001388'),
    (audio_legacy.AudioMonitorPropertiesCommand(volume=100), '020000640000000000000000'),
    (audio_legacy.SendAudioLevelsCommand(True), '01000000'),
    (multiviewer.MultiviewInputCommand(index=1, window=7, source=3010), '01070bc2'),
    (multiviewer.MultiviewPropertiesCommand(index=0, layout=3, swap=True), '03000301'),
    (multiviewer.MultiviewPropertiesCommand(index=1, swap=False), '02010000'),
    (supersource.SupersourceBoxPropertiesCommand(index=0, box=2, enabled=True, source=5, x=-1200, y=3400,
                                                 size=500, masked=True, top=100, bottom=200, left=300, right=400),
     '03ff000201000005fb500d4801f40100006400c8012c0190'),
    (supersource.SupersourceBoxPropertiesCommand(index=1, box=0, x=4800), '000401000000000012c00000000000000000000000000000'),
    (supersource.SupersourcePropertiesCommand(index=0, fill_source=3020, key_source=3021, layer=1,
                                              premultiplied=True, clip=500, gain=250, invert=True),
     '7f000bcc0bcd010101f400fa01000000'),
    (supersource.SupersourcePropertiesCommand(index=0, clip=10), '1000000000000000000a000000000000'),
    (transition.TransitionPreviewCommand(index=1, enabled=True), '01010000'),
    (transition.TransitionPositionCommand(index=0, position=10000), '00002710'),
    (recording.RecorderStatusCommand(True), '01000000'),
    (system_info.ClearStartupStateCommand(), '00000000'),
    (system_info.SaveStartupStateCommand(), '00000000'),
    (streaming.StreamingAudioBitrateCommand(128000, 128000), '0001f4000001f400'),
    (streaming.StreamingStatusSetCommand(False), '00000000'),
    (system_info.SetTimeOfDayCommand(datetime.datetime(2026, 9, 11, 12, 0, 0, tzinfo=datetime.timezone.utc)),
     '6aa3ed4000000000'),
    (system_info.SetTimeOfDayCommand(1789128000), '6aa3ed4000000000'),
]


@pytest.mark.parametrize('cmd, expected', CASES, ids=[c.CODE + ':' + h[:8] for c, h in CASES])
def test_payload_matches_upstream(cmd, expected):
    assert payload(cmd).hex() == expected


# ---------------------------------------------------------------------------
# the two long packets, against upstream's struct.pack format strings
# ---------------------------------------------------------------------------

def test_crms_matches_upstream_layout():
    got = payload(recording.RecordingSettingsSetCommand(
        filename='Show 2026-09-11', disk1=0x12345678, disk2=0x9ABCDEF0, record_in_camera=True))
    want = struct.pack('>B 128s xxx II ?xxx', 0x0f, b'Show 2026-09-11', 0x12345678, 0x9ABCDEF0, True)
    assert got == want
    got = payload(recording.RecordingSettingsSetCommand(filename='x'))
    assert got == struct.pack('>B 128s xxx II ?xxx', 0x01, b'x', 0, 0, False)


def test_crss_matches_upstream_layout():
    got = payload(streaming.StreamingServiceSetCommand(
        name='YouTube', url='rtmp://a.rtmp.youtube.com/live2', key='abcd-1234',
        bitrate_min=6000000, bitrate_max=9000000))
    want = struct.pack('>B 64s 512s 512s 3x II', 0x0f, b'YouTube', b'rtmp://a.rtmp.youtube.com/live2',
                       b'abcd-1234', 6000000, 9000000)
    assert got == want
    got = payload(streaming.StreamingServiceSetCommand(key='k'))
    assert got == struct.pack('>B 64s 512s 512s 3x II', 0x04, b'', b'', b'k', 0, 0)


def test_crss_requires_both_bitrates():
    with pytest.raises(ValueError):
        streaming.StreamingServiceSetCommand(bitrate_min=1)


# ---------------------------------------------------------------------------
# camera control: variable payload, per data type
# ---------------------------------------------------------------------------

CCMD = [
    (dict(datatype=0, data=[True]), '020801000000000100000000000000000100000000000000'),
    (dict(datatype=1, data=[-3, 7]), '02080100010000020000000000000000fd07000000000000'),
    (dict(datatype=2, data=[-1000, 1000]), '02080100020000020000000000000000fc1803e800000000'),
    (dict(datatype=3, data=[100000]), '02080100030000000001000000000000000186a000000000'),
    (dict(datatype=4, data=[1]), '020801000400000100000000000000000000000000000001'),
    (dict(datatype=5, data=['ab']), '020801000500000100000000000000006162000000000000'),
    (dict(datatype=128, data=[0.5, -1.0]), '020801008000000000020000000000000400f80000000000'),
    (dict(datatype=None, data=None), '02080100000000000000000000000000'),
    (dict(relative=True, datatype=128, data=[0.25]), '020801018000000000010000000000000200000000000000'),
]


@pytest.mark.parametrize('kw, expected', CCMD, ids=[str(k.get('datatype')) + ('r' if k.get('relative') else '') for k, _ in CCMD])
def test_ccmd_matches_upstream(kw, expected):
    data = kw.get('data')
    snapshot = list(data) if data is not None else None
    cmd = camera_control.CameraControlCommand(destination=2, category=8, parameter=1, **kw)
    assert payload(cmd).hex() == expected
    assert data == snapshot, 'the caller\'s data list must not be mutated (upstream mutated it)'


# ---------------------------------------------------------------------------
# operation wrappers emit the right command; the facade sees them
# ---------------------------------------------------------------------------

def test_wrappers_emit_the_matching_command():
    c = _FakeConn()
    transition.set_transition_preview(c, True, me=1)
    transition.set_transition_position(c, 20000)          # clamped to 10000
    multiviewer.set_multiviewer_input(c, 7, 3010, index=1)
    multiviewer.set_multiviewer_layout(c, 3)
    multiviewer.set_multiviewer_swap(c, True, index=1)
    recording.start_recording(c); recording.stop_recording(c)
    recording.set_recording_settings(c, filename='x')
    streaming.start_streaming(c); streaming.stop_streaming(c)
    streaming.set_streaming_audio_bitrate(c, 128000, 128000)
    streaming.set_streaming_service(c, key='k')
    supersource.set_supersource_box(c, 2, x=100)
    supersource.set_supersource_properties(c, clip=10)
    audio_legacy.set_audio_input(c, 1, volume=5)
    audio_legacy.set_audio_master(c, afv=True)
    audio_legacy.set_audio_monitor(c, mute=True)
    audio_legacy.enable_audio_levels(c)
    camera_control.camera_control(c, 255, 0, 0, datatype=1, data=[1])
    system_info.set_auto_video_mode(c, True)
    system_info.save_startup_state(c); system_info.clear_startup_state(c)
    system_info.set_time_of_day(c, 1789128000)
    codes = [cmd.CODE for cmd in c.sent]
    assert codes == ['CTPr', 'CTPs', 'CMvI', 'CMvP', 'CMvP', 'RcTM', 'RcTM', 'CRMS', 'StrR', 'StrR',
                     'STAB', 'CRSS', 'CSBP', 'CSSc', 'CAMI', 'CAMM', 'CAMm', 'SALN', 'CCmd', 'AiVM',
                     'SRsv', 'SRcl', 'SToD']
    assert payload(c.sent[1]).hex() == '00002710'
    assert payload(c.sent[3]).hex() == '01000300'


def test_facade_exposes_the_restored_operations():
    for name in ('set_transition_preview', 'set_transition_position', 'set_multiviewer_input',
                 'set_multiviewer_layout', 'set_multiviewer_swap', 'start_recording', 'stop_recording',
                 'set_recording_settings', 'start_streaming', 'stop_streaming', 'set_streaming_service',
                 'set_streaming_audio_bitrate', 'set_supersource_box', 'set_supersource_properties',
                 'set_audio_input', 'set_audio_master', 'set_audio_monitor', 'enable_audio_levels',
                 'camera_control', 'set_auto_video_mode', 'save_startup_state', 'clear_startup_state',
                 'set_time_of_day'):
        assert name in _OPERATIONS, name
