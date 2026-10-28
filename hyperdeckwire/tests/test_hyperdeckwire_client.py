# SPDX-License-Identifier: MIT
"""Unit tests for hyperdeckwire.client.

These verify the 9993 protocol I/O without touching real hardware. A
``_FakeSocket`` replays scripted responses byte-by-byte so we exercise
the actual line-framing, multi-line response parsing, and clip-line
tokenizer the production client uses.

What's pinned here:
  - Connection greeting parsing (model + protocol version).
  - Multi-line response with trailing-colon detection +
    blank-line termination.
  - Simple ack (200) vs error (1xx) distinguishing — error codes
    raise ``HyperdeckError`` with the code and text preserved.
  - Asynchronous (5xx) messages are skipped by default during a
    blocking request — they don't get returned as the request's
    response — and kept for ``read_notifications``.
  - Clip line parsing handles filenames with spaces correctly
    (real-world data: "Intro Loop animation.mp4").
  - Action commands assemble their argument strings as documented.
"""

import logging
import socket
from typing import List, Optional

import pytest

import hyperdeckwire.client as client_mod

from hyperdeckwire.client import (
    Clip,
    Hyperdeck,
    HyperdeckError,
    Response,
    _parse_clips_v1_line,
    _parse_disk_line,
)


# A script item: nothing arrives while the client waits. The recv times
# out, and the test's clock (see ``clock``) moves on by the socket's
# timeout, so the client's deadline passes without any real waiting.
SILENCE = None


class _FakeSocket:
    """Minimal socket stand-in.

    ``script`` is a list of byte-strings the server will send on
    successive ``recv`` calls (one chunk per call), or ``SILENCE``. The
    fake also captures everything ``sendall`` wrote in ``sent`` so tests
    can assert on the exact wire bytes the client emitted.

    The script is what the deck sends LATER, in answer to what the client
    does. What has already arrived is ``_inbox`` (``arrive()`` adds to
    it): with ``settimeout(0)`` (a non-blocking read) ``recv`` returns
    only that, and raises ``BlockingIOError`` when it is empty.
    """

    def __init__(self, script: List[bytes], clock=None):
        self._script = list(script)
        self._inbox = b''
        self.sent = b''
        self.closed = False
        self._timeout: Optional[float] = None
        self._clock = clock

    def arrive(self, data: bytes):
        """Data the deck has already sent, waiting in the socket."""
        self._inbox += data

    # --- socket surface --------------------------------------------------

    def settimeout(self, t):
        self._timeout = t

    def sendall(self, data):
        if self.closed:
            raise OSError('closed')
        self.sent += data

    def recv(self, n):
        if self.closed:
            return b''
        if not self._inbox:
            if self._timeout == 0:
                raise BlockingIOError('nothing ready')
            if not self._script:
                # Mimic real blocking — but for tests, just return empty
                # to signal EOF rather than hang forever.
                return b''
            if self._script[0] is SILENCE:
                self._script.pop(0)
                self._clock.now += self._timeout
                raise socket.timeout('timed out')
            self._inbox = self._script.pop(0)
        chunk, self._inbox = self._inbox[:n], self._inbox[n:]
        return chunk

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


GREETING = (
    b'500 connection info:\r\n'
    b'protocol version: 1.13\r\n'
    b'model: HyperDeck Studio HD Mini\r\n'
    b'\r\n'
)


def _client_with_script(extra_script):
    """Build a connected Hyperdeck whose socket emits the greeting
    followed by ``extra_script``."""
    sock = _FakeSocket([GREETING] + list(extra_script))
    factory = lambda *_a, **_kw: sock
    hd = Hyperdeck('1.2.3.4', socket_factory=factory)
    hd.connect()
    return hd, sock


# ---------------------------------------------------------------------------
# Greeting
# ---------------------------------------------------------------------------

def test_connect_parses_greeting():
    hd, _ = _client_with_script([])
    assert hd.model == 'HyperDeck Studio HD Mini'
    assert hd.protocol_version == '1.13'


# ---------------------------------------------------------------------------
# Response framing
# ---------------------------------------------------------------------------

def test_simple_ack_response_is_ok_no_lines():
    hd, sock = _client_with_script([b'200 ok\r\n'])
    resp = hd.request('stop')
    assert resp.code == 200
    assert resp.text == 'ok'
    assert resp.lines == []
    assert resp.is_ok()
    # Wire bytes: just the command + LF.
    assert sock.sent == b'stop\n'


def test_multi_line_response_terminates_on_blank_line():
    hd, _ = _client_with_script([
        b'204 device info:\r\n'
        b'protocol version: 1.13\r\n'
        b'model: HyperDeck Studio HD Mini\r\n'
        b'unique id: 7c2e0d18dc78\r\n'
        b'slot count: 3\r\n'
        b'software version: 8.1.1\r\n'
        b'\r\n'
    ])
    info = hd.device_info()
    assert info['model'] == 'HyperDeck Studio HD Mini'
    assert info['protocol version'] == '1.13'
    assert info['unique id'] == '7c2e0d18dc78'
    assert info['slot count'] == '3'
    assert info['software version'] == '8.1.1'


def test_error_response_raises_hyperdeck_error_with_code():
    hd, _ = _client_with_script([b'111 remote control disabled\r\n'])
    with pytest.raises(HyperdeckError) as exc_info:
        hd.stop()
    assert exc_info.value.code == 111
    assert exc_info.value.text == 'remote control disabled'


def test_async_message_skipped_so_real_response_returns():
    """An async 5xx arriving before our actual response must be
    drained transparently — the request() caller should receive the
    real response, not the async one."""
    hd, _ = _client_with_script([
        # Async transport-state change arrives unsolicited:
        b'508 transport info:\r\n'
        b'status: stopped\r\n'
        b'\r\n'
        # Then the real response to our command:
        b'200 ok\r\n'
    ])
    resp = hd.request('stop')
    assert resp.code == 200
    assert resp.text == 'ok'


def test_async_message_returned_when_skip_async_false():
    """skip_async=False makes _read_response return the async too —
    useful for a future notifications consumer."""
    sock = _FakeSocket([GREETING, b'508 transport info:\r\nstatus: play\r\n\r\n'])
    factory = lambda *_a, **_kw: sock
    hd = Hyperdeck('1.2.3.4', socket_factory=factory)
    hd.connect()
    resp = hd._read_response(skip_async=False)
    assert resp.is_async()
    assert resp.code == 508


# ---------------------------------------------------------------------------
# Clip parsers (real-world inputs from bench tests)
# ---------------------------------------------------------------------------

def test_disk_line_parser_handles_filename_with_spaces():
    """Verified real-world data — filenames with spaces survived FTP
    upload and disk_list round-trip. The parser pulls the three
    trailing tokens (file format, video format, duration) and treats
    everything else as the name."""
    clip = _parse_disk_line(
        '1: Intro Loop animation.mp4 H.264 1080p25 00:01:03:17')
    assert clip == Clip(
        clip_id=1,
        name='Intro Loop animation.mp4',
        duration='00:01:03:17',
        file_format='H.264',
        video_format='1080p25',
    )


def test_clips_get_on_an_empty_timeline_is_empty():
    """A Studio HD Mini on 9.0.2 answers clips get on an empty timeline
    with 107 timeline empty, not an empty 205 (2026-10-27)."""
    hd, _ = _client_with_script([b'107 timeline empty\r\n'])
    assert hd.clips_get() == []


def test_clips_get_other_refusals_still_raise():
    hd, _ = _client_with_script([b'100 syntax error\r\n'])
    with pytest.raises(HyperdeckError) as exc_info:
        hd.clips_get()
    assert exc_info.value.code == 100


def test_clips_v1_line_parser_handles_filename_with_spaces():
    clip = _parse_clips_v1_line(
        '2: Intro Loop animation2.mp4 00:01:03:17 00:01:03:17')
    assert clip == Clip(
        clip_id=2,
        name='Intro Loop animation2.mp4',
        duration='00:01:03:17',
        start='00:01:03:17',
    )


def test_disk_line_parser_rejects_malformed_line():
    with pytest.raises(ValueError):
        _parse_disk_line('not-a-clip-line')


# ---------------------------------------------------------------------------
# High-level commands — wire-format assertions
# ---------------------------------------------------------------------------

def test_disk_list_filters_header_lines_and_returns_clips():
    hd, _ = _client_with_script([
        b'206 disk list:\r\n'
        b'slot id: 1\r\n'
        b'1: Intro Loop animation.mp4 H.264 1080p25 00:01:03:17\r\n'
        b'2: Other Clip.mov ProRes 1080p25 00:00:30:00\r\n'
        b'\r\n'
    ])
    clips = hd.disk_list()
    assert len(clips) == 2
    assert clips[0].clip_id == 1
    assert clips[0].name == 'Intro Loop animation.mp4'
    assert clips[1].clip_id == 2
    assert clips[1].name == 'Other Clip.mov'


def test_play_with_loop_and_single_clip_builds_correct_command():
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.play(loop=True, single_clip=True)
    sent_after_greeting = sock.sent
    assert sent_after_greeting == b'play: loop: true single clip: true\n'


def test_play_with_no_args_sends_plain_play():
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.play()
    assert sock.sent == b'play\n'


def test_play_with_false_loop_and_single_clip_sends_them_false():
    # Before 1.8.0 a False was left out, so a loop couldn't be turned off.
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.play(loop=False, single_clip=False)
    assert sock.sent == b'play: loop: false single clip: false\n'


def test_play_leaves_out_what_it_is_not_given():
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.play(single_clip=True, speed=50)
    assert sock.sent == b'play: single clip: true speed: 50\n'


def test_pause_sends_play_speed_zero():
    # HyperDeck has no literal pause — it's play at speed 0 (holds the frame).
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.pause()
    assert sock.sent == b'play: speed: 0\n'


def test_clips_add_preserves_spaces_in_name():
    """The protocol parses the parameter value as 'everything after
    name: until end-of-line', so filenames with spaces go on the wire
    verbatim — no escaping."""
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.clips_add('Intro Loop animation2.mp4')
    assert sock.sent == b'clips add: name: Intro Loop animation2.mp4\n'


def test_clips_add_with_before_clip_id_inserts():
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.clips_add('newer.mp4', before_clip_id=3)
    assert sock.sent == b'clips add: clip id: 3 name: newer.mp4\n'


def test_goto_clip_sends_clip_id_param():
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.goto_clip(5)
    assert sock.sent == b'goto: clip id: 5\n'


@pytest.mark.parametrize('offset, line', [
    (1, b'goto: clip id: +1\n'),
    (-1, b'goto: clip id: -1\n'),
    (3, b'goto: clip id: +3\n'),
], ids=['next', 'prev', 'three-on'])
def test_goto_clip_by_sends_a_signed_offset(offset, line):
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.goto_clip_by(offset)
    assert sock.sent == line


def test_goto_clip_by_zero_is_refused_unsent():
    hd, sock = _client_with_script([b'200 ok\r\n'])
    with pytest.raises(ValueError, match='moves nowhere'):
        hd.goto_clip_by(0)
    assert sock.sent == b''


def test_goto_clip_by_past_the_end_raises_109():
    # A Studio HD Mini on 9.0.2 with one clip on the timeline (2026-10-27).
    hd, _ = _client_with_script([b'109 out of range\r\n'])
    with pytest.raises(HyperdeckError) as exc_info:
        hd.goto_clip_by(1)
    assert exc_info.value.code == 109


@pytest.mark.parametrize('timecode', ['00:01:30:00', '23:59:59:29', '00:00:10;02'],
                         ids=['plain', 'last', 'drop-frame'])
def test_goto_timecode_sends_it(timecode):
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.goto_timecode(timecode)
    assert sock.sent == f'goto: timecode: {timecode}\n'.encode()


@pytest.mark.parametrize('timecode', [
    '1:00:00:00', '24:00:00:00', '00:60:00:00', '00:00:60:00', '00;00:00:00',
    '00:00:00:0', '00:00:00:000', ' 00:00:00:00', '00:00:00:00\n', '',
    '٠٠:٠٠:٠٠:٠٠', 100,
], ids=['one-digit-hour', 'hour-24', 'minute-60', 'second-60', 'semicolon-early',
        'one-digit-frame', 'three-digit-frame', 'space', 'line-break', 'empty',
        'arabic-digits', 'int'])
def test_goto_timecode_refuses_anything_else_unsent(timecode):
    hd, sock = _client_with_script([b'200 ok\r\n'])
    with pytest.raises(ValueError, match='not a timecode'):
        hd.goto_timecode(timecode)
    assert sock.sent == b''


@pytest.mark.parametrize('name, line', [
    (None, b'record\n'),
    ('', b'record\n'),
    ('Interview 2', b'record: name: Interview 2\n'),
], ids=['none', 'empty', 'named'])
def test_record_sends_its_name_if_given(name, line):
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.record(name)
    assert sock.sent == line


def test_record_with_no_input_raises_110():
    # A Studio HD Mini on 9.0.2 with nothing on its input (2026-10-27).
    hd, _ = _client_with_script([b'110 no input\r\n'])
    with pytest.raises(HyperdeckError) as exc_info:
        hd.record()
    assert exc_info.value.code == 110


def test_remote_enable_true_sets_remote_to_true():
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.remote_enable(True)
    assert sock.sent == b'remote: enable: true\n'


def test_set_configuration_single_param_builds_command():
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.set_configuration(file_format='H.264High')
    assert sock.sent == b'configuration: file format: H.264High\n'


def test_set_configuration_stacks_multiple_params_in_one_command():
    """The protocol accepts multiple params on a single ``configuration:``
    line — the deck applies them as a batch. snake_case kwargs are
    translated to space-separated protocol names."""
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.set_configuration(file_format='QuickTimeProResHQ',
                         default_standard='2160p25',
                         video_input='SDI')
    assert sock.sent == (
        b'configuration: file format: QuickTimeProResHQ '
        b'default standard: 2160p25 video input: SDI\n'
    )


def test_set_configuration_translates_bool_to_protocol_strings():
    """Python True/False -> protocol 'true'/'false'."""
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.set_configuration(record_cache=True, append_timestamp=False)
    assert sock.sent == (
        b'configuration: record cache: true append timestamp: false\n'
    )


def test_set_configuration_no_args_is_noop():
    """An empty kwargs call sends nothing — the protocol would reject
    a bare ``configuration:`` line anyway."""
    hd, sock = _client_with_script([])
    hd.set_configuration()
    assert sock.sent == b''  # nothing went on the wire beyond the greeting drain


def test_set_configuration_raises_on_protocol_error():
    hd, _ = _client_with_script([b'102 invalid value\r\n'])
    with pytest.raises(HyperdeckError) as exc_info:
        hd.set_configuration(file_format='NotARealFormat')
    assert exc_info.value.code == 102


def test_set_configuration_accepts_213_deck_rebooting_as_success():
    """213 is a 2xx code — Hyperdeck signals "command accepted, but I'm
    about to reboot." Caller has to handle the connection drop on the
    next call; we just don't raise here."""
    hd, _ = _client_with_script([b'213 deck rebooting\r\n'])
    # Does not raise.
    hd.set_configuration(file_format='QuickTimeProResHQ')


def test_clips_count_returns_int():
    hd, _ = _client_with_script([
        b'214 clips count:\r\n'
        b'clip count: 7\r\n'
        b'\r\n'
    ])
    assert hd.clips_count() == 7


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_context_manager_connects_and_closes():
    sock = _FakeSocket([GREETING])
    factory = lambda *_a, **_kw: sock
    with Hyperdeck('1.2.3.4', socket_factory=factory) as hd:
        assert hd.model == 'HyperDeck Studio HD Mini'
    # On exit: 'quit' line was sent, socket closed.
    assert b'quit\n' in sock.sent
    assert sock.closed


def test_close_is_idempotent():
    sock = _FakeSocket([GREETING])
    factory = lambda *_a, **_kw: sock
    hd = Hyperdeck('1.2.3.4', socket_factory=factory)
    hd.connect()
    hd.close()
    hd.close()  # second close must not raise


# ---------------------------------------------------------------------------
# Response.params helper
# ---------------------------------------------------------------------------

def test_response_params_parses_key_value_pairs():
    resp = Response(code=204, text='device info', lines=[
        'protocol version: 1.13',
        'model: HyperDeck Studio HD Mini',
        '  unique id: abcdef  ',  # tolerate leading/trailing whitespace
    ])
    params = resp.params()
    assert params == {
        'protocol version': '1.13',
        'model': 'HyperDeck Studio HD Mini',
        'unique id': 'abcdef',
    }


def test_response_params_skips_lines_without_colon():
    resp = Response(code=200, text='something', lines=['no-colon-line'])
    assert resp.params() == {}


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def test_notify_reads_which_are_on():
    hd, sock = _client_with_script([
        b'209 notify:\r\n'
        b'remote: false\r\n'
        b'transport: true\r\n'
        b'slot: false\r\n'
        b'configuration: false\r\n'
        b'\r\n'
    ])
    assert hd.notify() == {'remote': 'false', 'transport': 'true',
                           'slot': 'false', 'configuration': 'false'}
    assert sock.sent == b'notify\n'


def test_set_notify_stacks_its_kinds_in_one_line():
    hd, sock = _client_with_script([b'200 ok\r\n'])
    hd.set_notify(transport=True, slot=True, display_timecode=False)
    assert sock.sent == b'notify: transport: true slot: true display timecode: false\n'


def test_set_notify_no_args_is_noop():
    hd, sock = _client_with_script([])
    hd.set_notify()
    assert sock.sent == b''


def test_set_notify_raises_on_protocol_error():
    hd, _ = _client_with_script([b'101 unsupported parameter\r\n'])
    with pytest.raises(HyperdeckError) as exc_info:
        hd.set_notify(timeline_position=True)
    assert exc_info.value.code == 101


TRANSPORT_NOTE = b'508 transport info:\r\nstatus: play\r\n\r\n'
SLOT_NOTE = b'502 slot info:\r\nslot id: 1\r\nstatus: mounted\r\n\r\n'


def test_notifications_among_answers_are_kept_in_order():
    hd, sock = _client_with_script([TRANSPORT_NOTE + b'200 ok\r\n', SLOT_NOTE + b'200 ok\r\n'])
    hd.stop()
    hd.slot_select(1)
    notes = hd.read_notifications()
    assert [(n.code, n.text, n.params()) for n in notes] == [
        (508, 'transport info', {'status': 'play'}),
        (502, 'slot info', {'slot id': '1', 'status': 'mounted'}),
    ]
    assert hd.read_notifications() == [], 'read once, forgotten'


def test_notifications_that_came_before_a_command_are_kept():
    hd, [sock] = _deck([[GREETING, b'200 ok\r\n']])
    sock.arrive(TRANSPORT_NOTE)
    hd.stop()
    assert [n.code for n in hd.read_notifications()] == [508]


def test_read_notifications_takes_what_has_arrived():
    hd, [sock] = _deck([[GREETING]])
    sock.arrive(TRANSPORT_NOTE + SLOT_NOTE)
    assert [n.code for n in hd.read_notifications()] == [508, 502]


def test_read_notifications_waits_for_one(clock):
    hd, [sock] = _deck([[GREETING, SILENCE, TRANSPORT_NOTE]], clock)
    assert [n.code for n in hd.read_notifications(timeout=2.0)] == [508]


def test_waiting_out_read_notifications_leaves_the_connection(clock):
    hd, [sock] = _deck([[GREETING, SILENCE, b'200 ok\r\n']], clock)
    assert hd.read_notifications(timeout=0.5) == []
    assert hd._sock is sock
    hd.ping()
    assert sock.sent == b'ping\n'


def test_read_notifications_drops_a_stray_answer(caplog):
    hd, [sock] = _deck([[GREETING]])
    sock.arrive(b'200 ok\r\n' + TRANSPORT_NOTE)
    with caplog.at_level(logging.WARNING, logger='hyperdeckwire.client'):
        assert [n.code for n in hd.read_notifications()] == [508]
    assert 'no command was waiting for' in caplog.text


def test_read_notifications_leaves_a_partial_one():
    hd, [sock] = _deck([[GREETING]])
    sock.arrive(b'508 transport info:\r\nstatus: re')
    assert hd.read_notifications() == []
    sock.arrive(b'cord\r\n\r\n')
    assert [n.params() for n in hd.read_notifications()] == [{'status': 'record'}]


def test_a_full_backlog_keeps_the_newest(monkeypatch):
    monkeypatch.setattr(client_mod, 'NOTIFICATION_BACKLOG', 3)
    hd, [sock] = _deck([[GREETING]])
    for status in (b'play', b'stopped', b'preview', b'record'):
        sock.arrive(b'508 transport info:\r\nstatus: ' + status + b'\r\n\r\n')
    assert [n.params()['status'] for n in hd.read_notifications()] == [
        'stopped', 'preview', 'record']


def test_connect_starts_the_notifications_afresh():
    hd, socks = _deck([[GREETING, b'200 ok\r\n'], [GREETING]])
    socks[0].arrive(TRANSPORT_NOTE)
    hd.ping()                                    # sets the notification aside
    hd.connect()
    assert hd.read_notifications() == []


def test_read_notifications_when_not_connected():
    hd = Hyperdeck('1.2.3.4')
    with pytest.raises(OSError, match='not connected'):
        hd.read_notifications()


# ---------------------------------------------------------------------------
# Answers stay with their commands
# ---------------------------------------------------------------------------

class _Clock:
    """Stands in for time.monotonic in the client: SILENCE moves it on."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(client_mod.time, 'monotonic', c.monotonic)
    return c


def _deck(scripts, clock=None, read_timeout=0.3):
    """A Hyperdeck whose every connect() gets the next scripted socket."""
    socks = [_FakeSocket(script, clock=clock) for script in scripts]
    pending = list(socks)
    hd = Hyperdeck('1.2.3.4', read_timeout=read_timeout,
                   socket_factory=lambda *_a, **_kw: pending.pop(0))
    hd.connect()
    return hd, socks


TRANSPORT_PLAY = b'208 transport info:\r\nstatus: play\r\n\r\n'


# --- #18: a timeout ends the connection -----------------------------------

def test_a_timeout_closes_the_connection(clock):
    # The deck answers transport info only after read_timeout, then ping.
    hd, [sock] = _deck([[GREETING, SILENCE, TRANSPORT_PLAY, b'200 ok\r\n']], clock)
    with pytest.raises(TimeoutError):
        hd.transport_info()
    assert hd._sock is None, (
        'the late answer would be taken as the next command\'s: the '
        'connection must end'
    )
    assert sock.closed
    with pytest.raises(OSError, match='not connected'):
        hd.request('ping')


def test_after_a_timeout_a_reconnect_gets_its_own_answers(clock):
    hd, _ = _deck([[GREETING, SILENCE, TRANSPORT_PLAY],
                   [GREETING, b'200 ok\r\n', TRANSPORT_PLAY]], clock)
    with pytest.raises(TimeoutError):
        hd.transport_info()
    hd.connect()
    resp = hd.request('ping')
    assert (resp.code, resp.text) == (200, 'ok')
    assert hd.transport_info() == {'status': 'play'}


# --- #19: a response nobody asked for is dropped ---------------------------

def test_a_stray_answer_in_the_buffer_is_dropped(caplog):
    # The stray 200 ok came in the same read as the greeting.
    hd, [sock] = _deck([[GREETING + b'200 ok\r\n', TRANSPORT_PLAY]])
    with caplog.at_level(logging.WARNING, logger='hyperdeckwire.client'):
        assert hd.transport_info() == {'status': 'play'}
    assert sock.sent == b'transport info\n'
    assert 'no command was waiting for' in caplog.text


def test_a_stray_answer_waiting_in_the_socket_is_dropped():
    hd, [sock] = _deck([[GREETING, TRANSPORT_PLAY]])
    sock.arrive(b'200 ok\r\n')
    assert hd.transport_info() == {'status': 'play'}
    assert sock.sent == b'transport info\n'


def test_the_drain_does_not_take_the_real_answer():
    # Nothing has arrived: the non-blocking read finds nothing, and the
    # answer, which the deck sends only after the command, is the reply.
    hd, [sock] = _deck([[GREETING, TRANSPORT_PLAY]])
    assert hd.transport_info() == {'status': 'play'}


def test_a_partial_response_is_left_for_the_read():
    # Half an asynchronous message has arrived; its rest comes with the
    # answer. The drain leaves it, and the read skips it as before.
    hd, [sock] = _deck([[GREETING, b'cord\r\n\r\n200 ok\r\n']])
    sock.arrive(b'508 transport info:\r\nstatus: re')
    resp = hd.request('ping')
    assert (resp.code, resp.text) == (200, 'ok')
    assert sock.sent == b'ping\n'


# --- #20: a line break in a value is refused -------------------------------

@pytest.mark.parametrize('call', [
    lambda hd: hd.clips_add('a.mov\nplay'),
    lambda hd: hd.clips_add('a.mov', before_clip_id='1\nclips clear'),
    lambda hd: hd.set_configuration(record_prefix='x\r'),
    lambda hd: hd.set_configuration(record_prefix='show\nformat: prepare: H.264High'),
    lambda hd: hd.goto_clip('1\nstop'),
    lambda hd: hd.record('take\nstop'),
    lambda hd: hd.request('ping\nstop'),
], ids=['clips_add', 'clips_add-before', 'record_prefix-cr', 'record_prefix-lf',
        'goto_clip', 'record', 'request'])
def test_a_line_break_is_refused_before_sending(call):
    hd, sock = _client_with_script([b'200 ok\r\n', b'200 ok\r\n'])
    with pytest.raises(ValueError, match='line break'):
        call(hd)
    assert sock.sent == b''
    assert hd._sock is sock                      # still connected


# --- #21: a refused connection fails connect() -----------------------------

@pytest.mark.parametrize('greeting, code', [
    (b'120 connection failed\r\n', 120),         # BMD's text (Dec 2024)
    (b'120 connection rejected\r\n', 120),
    (b'122 authentication required\r\n', 122),
], ids=['120-failed', '120-rejected', '122'])
def test_a_1xx_greeting_fails_connect(greeting, code):
    sock = _FakeSocket([greeting])
    hd = Hyperdeck('1.2.3.4', socket_factory=lambda *_a, **_kw: sock)
    with pytest.raises(HyperdeckError) as excinfo:
        hd.connect()
    assert excinfo.value.code == code
    assert hd._sock is None
    assert sock.closed
    assert hd.model == ''


def test_an_unexpected_greeting_fails_connect():
    sock = _FakeSocket([b'200 ok\r\n'])
    hd = Hyperdeck('1.2.3.4', socket_factory=lambda *_a, **_kw: sock)
    with pytest.raises(OSError, match='unexpected greeting'):
        hd.connect()
    assert hd._sock is None
