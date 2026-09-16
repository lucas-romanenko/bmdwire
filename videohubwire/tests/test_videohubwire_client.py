# SPDX-License-Identifier: MIT
"""Unit tests for videohubwire.client.

Protocol I/O against canned bytes — no hardware. A ``_FakeSocket``
replays scripted recv chunks and captures sendall writes, exercising
the real block framing, preamble parsing, push-drain, and ACK/NAK
handling the production client uses.

Pinned here:
  - Preamble parsing: device info, labels with spaces, routing, locks,
    both with and without the END PRELUDE: marker.
  - state() snapshot shape: outputs carry source + lock; label
    fallbacks for unlabeled ports; counts from device info.
  - route() emits the exact wire bytes, ACK resolves (with optimistic
    routing apply), NAK raises, interleaved push blocks are applied
    while waiting.
  - state() drains pushed routing updates from other clients.
  - Bounds validation against the hub's own port counts.
  - A closed socket raises instead of looping.
"""

import socket
from typing import List

import pytest

from videohubwire.client import Videohub, VideohubError


PREAMBLE = (
    b'PROTOCOL PREAMBLE:\n'
    b'Version: 2.7\n'
    b'\n'
    b'VIDEOHUB DEVICE:\n'
    b'Device present: true\n'
    b'Model name: Blackmagic Smart Videohub 40 x 40\n'
    b'Friendly name: Test Hub\n'
    b'Unique ID: 0123456789AB\n'
    b'Video inputs: 4\n'
    b'Video processing units: 0\n'
    b'Video outputs: 4\n'
    b'Video monitoring outputs: 0\n'
    b'Serial ports: 0\n'
    b'\n'
    b'INPUT LABELS:\n'
    b'0 Camera 1\n'
    b'1 Camera 2\n'
    b'2 Replay Out\n'
    b'3 \n'
    b'\n'
    b'OUTPUT LABELS:\n'
    b'0 Monitor A\n'
    b'1 Switcher In 1\n'
    b'2 Stream Enc\n'
    b'3 \n'
    b'\n'
    b'VIDEO OUTPUT LOCKS:\n'
    b'0 U\n'
    b'1 L\n'
    b'2 O\n'
    b'3 U\n'
    b'\n'
    b'VIDEO OUTPUT ROUTING:\n'
    b'0 0\n'
    b'1 2\n'
    b'2 1\n'
    b'3 3\n'
    b'\n'
)

END_PRELUDE = b'END PRELUDE:\n\n'


class _FakeSocket:
    """Replays ``script`` chunks on successive recv calls; raises
    socket.timeout when the script runs dry; captures sendall bytes."""

    def __init__(self, script: List[bytes]):
        self._script = list(script)
        self.sent = b''
        self.closed = False

    def settimeout(self, t):
        pass

    def sendall(self, data):
        if self.closed:
            raise OSError('closed')
        self.sent += data

    def recv(self, n):
        if not self._script:
            raise socket.timeout()
        return self._script.pop(0)

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


def _connect(script: List[bytes]) -> Videohub:
    fake = _FakeSocket(script)
    vh = Videohub('192.0.2.31', socket_factory=lambda addr, timeout: fake)
    vh.connect()
    vh._fake = fake  # test-side handle for wire assertions
    return vh


# ---------------------------------------------------------------------------
# Preamble / state
# ---------------------------------------------------------------------------

def test_preamble_with_end_prelude_marker():
    vh = _connect([PREAMBLE + END_PRELUDE])
    assert vh.protocol_version == '2.7'
    assert vh.device['model name'] == 'Blackmagic Smart Videohub 40 x 40'
    assert vh.video_inputs == 4
    assert vh.video_outputs == 4


def test_preamble_without_end_prelude_marker():
    # Older firmware never sends the marker — quiet-wire completion.
    vh = _connect([PREAMBLE])
    assert vh.routing == {0: 0, 1: 2, 2: 1, 3: 3}


def test_labels_with_spaces_and_fallbacks():
    vh = _connect([PREAMBLE + END_PRELUDE])
    snap = vh.state()
    assert snap['inputs'][2]['label'] == 'Replay Out'
    # Port 3 has an empty label on the wire — falls back to a name.
    assert snap['inputs'][3]['label'] == 'Input 4'
    assert snap['outputs'][3]['label'] == 'Output 4'


def test_state_snapshot_shape():
    vh = _connect([PREAMBLE + END_PRELUDE])
    snap = vh.state()
    assert snap['device']['friendly_name'] == 'Test Hub'
    assert snap['device']['video_inputs'] == 4
    assert [o['source'] for o in snap['outputs']] == [0, 2, 1, 3]
    assert [o['lock'] for o in snap['outputs']] == ['U', 'L', 'O', 'U']


def test_state_drains_pushed_updates():
    push = b'VIDEO OUTPUT ROUTING:\n1 3\n\n'
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh._fake._script.append(push)  # another client re-routes
    snap = vh.state()
    assert snap['outputs'][1]['source'] == 3


def test_preamble_garbage_raises():
    fake = _FakeSocket([b'220 Welcome to some FTP server\r\n'])
    vh = Videohub('192.0.2.31', read_timeout=0.2,
                  socket_factory=lambda addr, timeout: fake)
    with pytest.raises(VideohubError):
        vh.connect()


def test_closed_connection_raises():
    fake = _FakeSocket([b''])  # recv returning b'' = peer closed
    vh = Videohub('192.0.2.31', read_timeout=0.2,
                  socket_factory=lambda addr, timeout: fake)
    with pytest.raises(VideohubError):
        vh.connect()


# ---------------------------------------------------------------------------
# route()
# ---------------------------------------------------------------------------

def test_route_sends_wire_bytes_and_acks():
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh._fake._script.append(b'ACK\n\n')
    vh.route(1, 3)
    assert vh._fake.sent == b'VIDEO OUTPUT ROUTING:\n1 3\n\n'
    assert vh.routing[1] == 3  # optimistic apply


def test_route_nak_raises():
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh._fake._script.append(b'NAK\n\n')
    with pytest.raises(VideohubError, match='refused'):
        vh.route(1, 3)


def test_route_applies_interleaved_push_before_ack():
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh._fake._script.append(b'VIDEO OUTPUT ROUTING:\n3 0\n\nACK\n\n')
    vh.route(1, 3)
    assert vh.routing[3] == 0   # push applied while waiting
    assert vh.routing[1] == 3   # our route acknowledged


def test_route_bounds_checked_against_hub_size():
    vh = _connect([PREAMBLE + END_PRELUDE])
    with pytest.raises(VideohubError, match='outside'):
        vh.route(99, 0)
    with pytest.raises(VideohubError, match='outside'):
        vh.route(0, 99)
    with pytest.raises(VideohubError):
        vh.route(-1, 0)


def test_route_timeout_without_response():
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh.read_timeout = 0.2
    with pytest.raises(VideohubError, match='timed out'):
        vh.route(1, 3)


# ---------------------------------------------------------------------------
# Label editing
# ---------------------------------------------------------------------------

def test_set_input_label_sends_wire_bytes_and_acks():
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh._fake._script.append(b'ACK\n\n')
    vh.set_input_label(2, 'Replay B')
    assert vh._fake.sent == b'INPUT LABELS:\n2 Replay B\n\n'
    assert vh.input_labels[2] == 'Replay B'


def test_set_output_label_sends_wire_bytes_and_acks():
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh._fake._script.append(b'ACK\n\n')
    vh.set_output_label(0, 'Wall Monitor 1')
    assert vh._fake.sent == b'OUTPUT LABELS:\n0 Wall Monitor 1\n\n'
    assert vh.output_labels[0] == 'Wall Monitor 1'


def test_set_label_nak_raises_and_keeps_old_label():
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh._fake._script.append(b'NAK\n\n')
    with pytest.raises(VideohubError, match='refused'):
        vh.set_input_label(0, 'Nope')
    assert vh.input_labels[0] == 'Camera 1'


def test_set_label_strips_newlines_that_would_break_framing():
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh._fake._script.append(b'ACK\n\n')
    vh.set_input_label(0, 'CAM\n1\r\nEVIL')
    assert vh._fake.sent == b'INPUT LABELS:\n0 CAM 1 EVIL\n\n'


def test_set_label_length_and_bounds():
    vh = _connect([PREAMBLE + END_PRELUDE])
    with pytest.raises(VideohubError, match='too long'):
        vh.set_input_label(0, 'x' * 65)
    with pytest.raises(VideohubError, match='outside'):
        vh.set_input_label(99, 'x')
    with pytest.raises(VideohubError, match='outside'):
        vh.set_output_label(-1, 'x')


def test_set_label_applies_interleaved_push_before_ack():
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh._fake._script.append(b'VIDEO OUTPUT ROUTING:\n3 1\n\nACK\n\n')
    vh.set_input_label(1, 'Camera 2B')
    assert vh.routing[3] == 1
    assert vh.input_labels[1] == 'Camera 2B'


# ---------------------------------------------------------------------------
# Connect / reconnect / close
# ---------------------------------------------------------------------------

import videohubwire.client as client_mod  # noqa: E402  (appended section)


def test_connect_retries_once_after_transient_oserror(monkeypatch):
    monkeypatch.setattr(client_mod, 'CONNECT_RETRY_DELAY_S', 0)
    attempts = []
    fake = _FakeSocket([PREAMBLE + END_PRELUDE])

    def factory(addr, timeout):
        attempts.append(addr)
        if len(attempts) == 1:
            raise ConnectionRefusedError()
        return fake

    vh = Videohub('192.0.2.31', socket_factory=factory)
    vh.connect()
    assert attempts == [('192.0.2.31', 9990)] * 2
    assert vh.video_inputs == 4


def test_connect_second_failure_propagates(monkeypatch):
    monkeypatch.setattr(client_mod, 'CONNECT_RETRY_DELAY_S', 0)

    def factory(addr, timeout):
        raise OSError('unreachable')

    vh = Videohub('192.0.2.31', socket_factory=factory)
    with pytest.raises(OSError, match='unreachable'):
        vh.connect()
    assert vh._sock is None


def test_reconnect_closes_previous_socket():
    first = _FakeSocket([PREAMBLE + END_PRELUDE])
    second = _FakeSocket([PREAMBLE + END_PRELUDE])
    socks = [first, second]
    vh = Videohub('192.0.2.31', socket_factory=lambda addr, timeout: socks.pop(0))
    vh.connect()
    vh.connect()
    assert first.closed and not second.closed
    vh.close()
    assert second.closed


def test_context_manager_closes_and_close_is_idempotent():
    fake = _FakeSocket([PREAMBLE + END_PRELUDE])
    with Videohub('192.0.2.31', socket_factory=lambda addr, timeout: fake) as vh:
        assert vh.video_outputs == 4
    assert fake.closed
    vh.close()
    vh.close()
    assert vh._sock is None


def test_preamble_failure_closes_socket():
    fake = _FakeSocket([b'garbage\r\n'])
    vh = Videohub('192.0.2.31', read_timeout=0.2,
                  socket_factory=lambda addr, timeout: fake)
    with pytest.raises(VideohubError):
        vh.connect()
    assert fake.closed and vh._sock is None


def test_hub_with_no_routing_block_and_no_marker_fails_to_connect():
    """Quiet-wire completion needs a routing block; without the END
    PRELUDE marker a hub that never sends one is reported as no preamble."""
    pre = PREAMBLE.split(b'VIDEO OUTPUT ROUTING:')[0]
    fake = _FakeSocket([pre])
    vh = Videohub('192.0.2.31', read_timeout=0.2,
                  socket_factory=lambda addr, timeout: fake)
    with pytest.raises(VideohubError, match='no state preamble'):
        vh.connect()


# ---------------------------------------------------------------------------
# ping() and parsing edge cases
# ---------------------------------------------------------------------------

def test_ping_acks_and_applies_interleaved_push():
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh._fake._script.append(b'INPUT LABELS:\n0 Renamed\n\nACK\n\n')
    vh.ping()
    assert vh._fake.sent == b'PING:\n\n'
    assert vh.input_labels[0] == 'Renamed'


def test_ping_timeout_raises():
    vh = _connect([PREAMBLE + END_PRELUDE])
    vh.read_timeout = 0.2
    with pytest.raises(VideohubError, match='PING'):
        vh.ping()


def test_malformed_indexed_line_is_skipped_not_fatal(caplog):
    bad = b'VIDEO OUTPUT ROUTING:\nnot-a-number 3\n2 x\n\n'
    vh = _connect([PREAMBLE + bad + END_PRELUDE])
    assert vh.routing == {0: 0, 1: 2, 2: 1, 3: 3}
    assert 'skipping malformed line' in caplog.text


def test_counts_fall_back_to_label_indices_when_device_omits_them():
    pre = PREAMBLE.replace(b'Video inputs: 4\n', b'').replace(b'Video outputs: 4\n', b'')
    vh = _connect([pre + END_PRELUDE])
    snap = vh.state()
    assert snap['device']['video_inputs'] == 4
    assert snap['device']['video_outputs'] == 4


def test_unknown_blocks_are_ignored():
    extra = b'CONFIGURATION:\nTake Mode: true\n\nSERIAL PORT LABELS:\n0 Deck\n\n'
    vh = _connect([PREAMBLE + extra + END_PRELUDE])
    assert vh.video_inputs == 4
    assert vh.routing == {0: 0, 1: 2, 2: 1, 3: 3}
