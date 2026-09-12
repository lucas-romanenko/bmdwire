# SPDX-License-Identifier: LGPL-3.0-only
"""Test doubles for atemwire — no live ATEM required.

``FakeAtemProtocol`` mimics the surface atemwire.connection.ATEMConnection
actually exercises (connect / blocking loop / send_commands / mixerstate /
on / transport.thread_recv_queue), configurable per-test:

  * connect_delay   — sleep inside connect() before firing 'connected'
  * loop_blocks     — loop() blocks on thread_recv_queue (real behaviour)
  * fail_connect / fail_loop_after / fail_loop_exception — failure modes

Used by atemwire's own tests and by applications that orchestrate the
library (capture pipelines, media-pool watchers, upload workers).
Fixtures that install these doubles live in atemwire/tests/conftest.py.
"""

import time
from queue import Queue


class FakeTransport:
    """Mimics atemwire.transport.UdpProtocol's surface used by ATEMConnection."""

    def __init__(self):
        self.thread_recv_queue: "Queue" = Queue()
        # ATEMConnection._close_protocol calls .sock.close()
        self.sock = _FakeSocket()


class _FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeAtemProtocol:
    """Stand-in for atemwire.protocol.AtemProtocol.

    Implements just enough surface to exercise ATEMConnection.

    The crucial behaviour the real AtemProtocol has — that ``loop()`` blocks
    on ``transport.thread_recv_queue.get()`` until either a packet arrives
    or someone puts ``None`` (disconnect signal) — is replicated here so
    the wake-the-worker path under test actually has something to wake.
    """

    def __init__(self, ip=None, *, connect_delay=0.0, loop_blocks=True,
                 fail_connect=False, fail_loop_after=None,
                 fail_loop_exception=None):
        self.ip = ip
        self.transport = FakeTransport()
        self.mixerstate = {}
        self.connected = False

        self._callbacks = {}
        self._connect_delay = connect_delay
        self._loop_blocks = loop_blocks
        self._fail_connect = fail_connect
        self._fail_loop_after = fail_loop_after  # raise after N loop() calls
        # The exception class to raise. Default RuntimeError gets caught by
        # _pump_loop_once's except Exception. Pass SystemExit (a BaseException
        # subclass) to simulate a worker-killing failure.
        self._fail_loop_exception = fail_loop_exception or RuntimeError
        self._loop_count = 0

        # Test-side instrumentation:
        self.send_log = []          # list of [Command, ...] batches sent
        self.connect_called = 0

    def connect(self):
        self.connect_called += 1
        if self._fail_connect:
            raise RuntimeError("simulated connect() failure")
        # Simulate handshake delay.
        if self._connect_delay > 0:
            time.sleep(self._connect_delay)
        # Real atemwire fires 'connected' AFTER the initial state dump
        # completes (post-InCm). Our wait_ready check is "connected event
        # fired AND 'video-mode' in mixerstate" — populate both.
        self.mixerstate['video-mode'] = _FakeVideoMode()
        self.connected = True
        # Wake any blocked loop().
        self.transport.thread_recv_queue.put('CONNECTED_PACKET')
        # Fire callbacks registered for 'connected'.
        for cb in self._callbacks.get('connected', []):
            try:
                cb()
            except Exception:
                pass

    def loop(self):
        self._loop_count += 1
        if self._fail_loop_after is not None and self._loop_count > self._fail_loop_after:
            raise self._fail_loop_exception("simulated loop() failure")
        if self._loop_blocks:
            # Mirror real atemwire: receive_packet blocks on the queue.
            packet = self.transport.thread_recv_queue.get()
            if packet is None:
                # Disconnect signal.
                self.connected = False
                self.mixerstate = {}
                for cb in self._callbacks.get('disconnected', []):
                    try:
                        cb()
                    except Exception:
                        pass

    def on(self, event, callback):
        self._callbacks.setdefault(event, []).append(callback)
        return len(self._callbacks[event]) - 1

    def send_commands(self, commands):
        if not self.connected:
            raise RuntimeError("send_commands while disconnected")
        self.send_log.append(list(commands))


class _FakeVideoMode:
    def get_resolution(self):
        return (1920, 1080)

    def get_label(self):
        return '1080p50'
