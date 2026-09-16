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
    response.
  - Clip line parsing handles filenames with spaces correctly
    (real-world data: "Intro Loop animation.mp4").
  - Action commands assemble their argument strings as documented.
"""

from typing import List, Optional

import pytest

from hyperdeckwire.client import (
    Clip,
    Hyperdeck,
    HyperdeckError,
    Response,
    _parse_clips_v1_line,
    _parse_disk_line,
)


class _FakeSocket:
    """Minimal socket stand-in.

    ``script`` is a list of byte-strings the server will send on
    successive ``recv`` calls (one chunk per call). The fake also
    captures everything ``sendall`` wrote in ``sent`` so tests can
    assert on the exact wire bytes the client emitted.
    """

    def __init__(self, script: List[bytes]):
        self._script = list(script)
        self._inbox = b''
        self.sent = b''
        self.closed = False
        self._timeout: Optional[float] = None

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
            if not self._script:
                # Mimic real blocking — but for tests, just return empty
                # to signal EOF rather than hang forever.
                return b''
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
