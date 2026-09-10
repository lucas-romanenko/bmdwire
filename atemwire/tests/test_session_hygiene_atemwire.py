# SPDX-License-Identifier: LGPL-3.0-only
"""Session-hygiene guarantees in atemwire core (2026-07-06 review).

Pins these fixes:

  - receive_packet must RETURN (not swallow) pre-handshake disconnect
        sentinels, so pumping callers (wait_ready, worker, uploader) can
        enforce their deadlines — a connect to an unreachable ATEM used to
        be un-timeout-able and un-killable.
  - one transient send error must not kill the UDP thread (zombie
        connection with is_connected True); and a worker whose transport
        thread DID die must fail loudly so the pool evicts it.
  - _raise iterates a snapshot (handlers off() themselves from the
        woken caller thread mid-iteration), and FTDC's _transfer_trigger
        runs even if event delivery raises — otherwise the media-store
        lock stayed held on a SUCCESSFUL download.
  - straggler FTDa/FTDE after abort_transfers must not blow up
        save_field_data (the packet is ACKed; co-bundled fields would be
        lost) and a foreign FTDE tid must not fail our transfer.
  - session death resets the transfer lane so the auto-reconnected
        session doesn't inherit ghost transfer_requested / stale locks.
  - wait_ready unregisters its 'connected' handler on every exit.
  - pool double-release is clamped, never negative.
  - SocketQueue.close() frees the socketpair FDs; the protocol close
        helpers call it.
"""

import errno
import struct
import threading
import time
import types

import pytest

from atemwire._socketqueue import SocketQueue
from atemwire.connection import ATEMConnection
from atemwire.pool import ATEMInstanceManager
from atemwire.protocol import AtemProtocol, TransferTask
from atemwire.ready import wait_ready
from atemwire.transport import UdpProtocol


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _bare_protocol():
    p = AtemProtocol(ip='127.0.0.1')
    sent = []
    p.send_commands = lambda cmds: sent.append(cmds)
    return p, sent


def _close_bare(p):
    try:
        p.transport.sock.close()
    except Exception:
        pass
    try:
        p.transport.thread_queue.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# pre-traffic disconnect sentinels surface to the caller
# ---------------------------------------------------------------------------

def test_receive_packet_returns_none_pre_traffic():
    """Pre-fix, a None on the recv queue before the first handshake
    response was swallowed (`continue`) and receive_packet blocked forever
    in the next get() — loop() never returned, deadlines never ran."""
    t = UdpProtocol('127.0.0.1')
    try:
        t.thread_recv_queue.put(None)
        result = {}

        def _call():
            result['value'] = t.receive_packet()

        th = threading.Thread(target=_call, daemon=True)
        th.start()
        th.join(timeout=2.0)
        assert not th.is_alive(), (
            "receive_packet still blocked on a pre-traffic None sentinel")
        assert result['value'] is None
    finally:
        t.sock.close()
        t.thread_queue.close()


def test_wait_ready_deadline_enforced_against_silent_host():
    """The uploader-fleet wedge: wait_ready against a host that never
    answers must raise TimeoutError. Pre-fix it blocked forever because
    every 5s timeout sentinel was swallowed inside one loop() call."""
    p, _ = _bare_protocol()
    stop = threading.Event()

    def _feeder():
        # Stand-in for the UDP thread's 5s timeout ticks.
        while not stop.is_set():
            p.transport.thread_recv_queue.put(None)
            time.sleep(0.02)

    feeder = threading.Thread(target=_feeder, daemon=True)
    feeder.start()

    outcome = {}

    def _wait():
        try:
            wait_ready(p, timeout=0.3, pump_interval=0.0)
            outcome['result'] = 'ready'
        except TimeoutError:
            outcome['result'] = 'timeout'

    th = threading.Thread(target=_wait, daemon=True)
    th.start()
    th.join(timeout=3.0)
    stop.set()
    try:
        assert not th.is_alive(), "wait_ready never returned (pre-handshake hang)"
        assert outcome['result'] == 'timeout'
    finally:
        _close_bare(p)


# ---------------------------------------------------------------------------
# transport thread survives transient send errors; worker detects a
# dead transport thread
# ---------------------------------------------------------------------------

def test_transient_send_error_keeps_udp_thread_alive():
    t = UdpProtocol('127.0.0.1')
    try:
        calls = {'n': 0}

        def _raising_send(item):
            calls['n'] += 1
            raise OSError(errno.ENETUNREACH, 'network unreachable')

        t._send_packet_low = _raising_send
        t.thread.start()
        t.thread_queue.put('pkt-1')
        deadline = time.time() + 2.0
        while calls['n'] < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert calls['n'] >= 1
        time.sleep(0.05)
        assert t.thread.is_alive(), (
            "UDP thread died on a transient send error (zombie connection)")

        # A non-transient error still terminates the thread with the
        # disconnect sentinel.
        def _fatal_send(item):
            raise OSError(errno.EBADF, 'bad file descriptor')

        t._send_packet_low = _fatal_send
        t.thread_queue.put('pkt-2')
        t.thread.join(timeout=2.0)
        assert not t.thread.is_alive()
        assert t.thread_recv_queue.get(timeout=1.0) is None
    finally:
        t.sock.close()
        t.thread_queue.close()


def test_worker_fails_when_transport_thread_dies(
    reset_pool, fake_protocol_factory,
):
    """A worker whose transport thread died must exit (unexpected-exit
    path) so is_connected flips False and the pool can evict — instead of
    parking forever on the empty queue while claiming to be connected."""
    fake_protocol_factory()
    conn = ATEMConnection('atem_hygiene_sh3')
    assert conn.connect('1.2.3.4')
    try:
        # Simulate a started-then-died UDP thread on the fake transport.
        conn._protocol.transport.thread = types.SimpleNamespace(
            ident=1, is_alive=lambda: False)
        # Wake the worker out of its blocking loop() with a benign packet.
        conn._protocol.transport.thread_recv_queue.put('WAKE')

        deadline = time.time() + 2.0
        while conn.is_connected and time.time() < deadline:
            time.sleep(0.01)
        assert conn.is_connected is False, (
            "worker kept claiming is_connected with a dead transport thread")
    finally:
        conn.disconnect()


def test_transport_thread_died_helper_edges():
    conn = ATEMConnection('atem_hygiene_helper')
    # No protocol at all.
    assert conn._transport_thread_died() is False
    # Transport without a thread attribute (fakes, TCP/USB transports).
    conn._protocol = types.SimpleNamespace(transport=types.SimpleNamespace())
    assert conn._transport_thread_died() is False
    # Thread created but never started (ident is None).
    conn._protocol = types.SimpleNamespace(transport=types.SimpleNamespace(
        thread=types.SimpleNamespace(ident=None, is_alive=lambda: False)))
    assert conn._transport_thread_died() is False
    # Started and alive.
    conn._protocol = types.SimpleNamespace(transport=types.SimpleNamespace(
        thread=types.SimpleNamespace(ident=7, is_alive=lambda: True)))
    assert conn._transport_thread_died() is False
    # Started and dead.
    conn._protocol = types.SimpleNamespace(transport=types.SimpleNamespace(
        thread=types.SimpleNamespace(ident=7, is_alive=lambda: False)))
    assert conn._transport_thread_died() is True


# ---------------------------------------------------------------------------
# _raise snapshot + FTDC trigger-always-runs
# ---------------------------------------------------------------------------

def test_raise_survives_handler_removing_handlers_mid_iteration():
    p, _ = _bare_protocol()
    try:
        called = []
        ids = {}

        def cb1():
            called.append('cb1')
            # The archetype race: a handler (or the thread it wakes)
            # unregisters DURING iteration.
            p.off('evt', ids['cb2'])

        def cb2():
            called.append('cb2')

        ids['cb1'] = p.on('evt', cb1)
        ids['cb2'] = p.on('evt', cb2)

        p._raise('evt')          # pre-fix: RuntimeError (dict size changed)

        assert 'cb1' in called   # snapshot semantics: cb2 may or may not
        assert 'evt' in p.callbacks and ids['cb2'] not in p.callbacks['evt']
    finally:
        _close_bare(p)


def test_raise_swallows_handler_exception():
    p, _ = _bare_protocol()
    try:
        seen = []
        p.on('evt', lambda: (_ for _ in ()).throw(RuntimeError('boom')))
        p.on('evt', lambda: seen.append('second'))
        p._raise('evt')          # must not propagate
        assert seen == ['second']
    finally:
        _close_bare(p)


def test_ftdc_releases_lock_even_if_download_done_handler_raises():
    """The stranded-lock scenario: the download SUCCEEDS, the handler (or
    a racing off()) blows up event delivery — the queue head is already
    popped, so _transfer_trigger MUST still run and, on an empty queue,
    send the unlock. Pre-fix locks[0] stayed True with nothing in flight."""
    from atemwire.messages.lock import LockCommand

    p, sent = _bare_protocol()
    try:
        t1 = TransferTask(0, 3)
        p.transfer_queue = {0: [t1]}
        p.transfer = t1
        p.transfer.tid = 44
        p.transfer_requested = True
        p.locks[0] = True

        def _exploding_handler(store, slot, data):
            raise RuntimeError('handler died mid-delivery')

        p.on('download-done', _exploding_handler)

        # FTDC for tid 44 (u16 tid + u1 + u2).
        p.save_field_data(b'FTDC', (44).to_bytes(2, 'big') + bytes([1, 2]))

        assert p.transfer_queue[0] == []
        unlocks = [c for batch in sent for c in batch
                   if isinstance(c, LockCommand)]
        assert len(unlocks) == 1, (
            "empty-queue unlock never sent — media lock stranded")
    finally:
        _close_bare(p)


# ---------------------------------------------------------------------------
# straggler / foreign transfer packets
# ---------------------------------------------------------------------------

def test_ftda_with_no_transfer_is_ignored():
    p, _ = _bare_protocol()
    try:
        p.transfer = None
        raw = struct.pack('>HH', 7, 4) + b'DATA'
        p.save_field_data(b'FTDa', raw)   # pre-fix: AttributeError
    finally:
        _close_bare(p)


def test_ftde_with_no_transfer_is_ignored():
    p, _ = _bare_protocol()
    try:
        p.transfer = None
        p.transfer_requested = True       # must be left alone
        p.save_field_data(b'FTDE', (9).to_bytes(2, 'big') + bytes([1, 0]))
        assert p.transfer_requested is True
    finally:
        _close_bare(p)


def test_ftde_foreign_tid_does_not_fail_our_transfer():
    p, _ = _bare_protocol()
    try:
        t1 = TransferTask(0, 3)
        p.transfer_queue = {0: [t1]}
        p.transfer = t1
        p.transfer.tid = 41
        p.transfer_requested = True
        p.locks[0] = True

        # Fatal-status FTDE for a DIFFERENT transfer id.
        p.save_field_data(b'FTDE', (99).to_bytes(2, 'big') + bytes([2, 0]))

        assert p.transfer is t1            # ours untouched
        assert p.transfer_queue[0] == [t1]
        assert p.transfer_requested is True
        assert p.locks[0] is True
    finally:
        _close_bare(p)


# ---------------------------------------------------------------------------
# session death resets the transfer lane
# ---------------------------------------------------------------------------

def test_disconnect_resets_transfer_lane():
    p, _ = _bare_protocol()
    try:
        t1 = TransferTask(0, 3)
        p.connected = True
        p.transfer_queue = {0: [t1]}
        p.transfer = t1
        p.transfer_requested = True
        p.transfer_buffer = [b'partial']
        p.transfer_buffer_bytes = 7
        p.locks[0] = True

        p.transport.receive_packet = lambda: None   # disconnect sentinel
        p.loop()

        assert p.connected is False
        assert p.transfer is None
        assert p.transfer_requested is False
        assert p.transfer_queue == {}
        assert p.transfer_buffer == []
        assert p.transfer_buffer_bytes == 0
        assert p.locks == {}
    finally:
        _close_bare(p)


def test_disconnect_when_not_connected_does_not_reset_lane_needlessly():
    """Pre-connect ticks (now surfaced to the caller) must be harmless."""
    p, _ = _bare_protocol()
    try:
        p.connected = False
        p.transport.receive_packet = lambda: None
        p.loop()                                    # no exception, no event
        assert p.connected is False
    finally:
        _close_bare(p)


# ---------------------------------------------------------------------------
# wait_ready handler hygiene
# ---------------------------------------------------------------------------

class _FakeReadyProtocol:
    def __init__(self, become_ready):
        self.connected = False
        self.mixerstate = {}
        self._become_ready = become_ready
        self._handlers = {}
        self._next = 0

    def on(self, event, cb):
        self._next += 1
        self._handlers[self._next] = (event, cb)
        return self._next

    def off(self, event, handler_id):
        del self._handlers[handler_id]

    def loop(self):
        if self._become_ready:
            self.connected = True
            self.mixerstate['video-mode'] = object()


def test_wait_ready_unregisters_handler_on_success():
    p = _FakeReadyProtocol(become_ready=True)
    wait_ready(p, timeout=1.0, pump_interval=0.0)
    assert p._handlers == {}


def test_wait_ready_unregisters_handler_on_timeout():
    p = _FakeReadyProtocol(become_ready=False)
    with pytest.raises(TimeoutError):
        wait_ready(p, timeout=0.05, pump_interval=0.0)
    assert p._handlers == {}


# ---------------------------------------------------------------------------
# pool double-release clamp
# ---------------------------------------------------------------------------

def test_double_release_is_clamped(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
    caplog,
):
    fake_protocol_factory()
    inst = ATEMInstanceManager.get_instance('1.2.3.4')
    assert inst['ref_count'] == 1
    # Simulate the double-release state directly (avoids racing the grace
    # timer): the entry exists, ACTIVE, but its refcount is already 0.
    inst['ref_count'] = 0
    ATEMInstanceManager.release_instance('1.2.3.4')   # the double release
    assert inst['ref_count'] == 0, "refcount went negative on double release"
    assert inst.get('disconnect_timer') is None, (
        "double release armed a grace timer")
    assert any('double release' in r.message.lower() for r in caplog.records)
    # Restore for clean fixture teardown.
    inst['ref_count'] = 1
    ATEMInstanceManager.release_instance('1.2.3.4')


# ---------------------------------------------------------------------------
# SocketQueue.close
# ---------------------------------------------------------------------------

def test_socketqueue_close_frees_fds_and_is_idempotent():
    sq = SocketQueue()
    assert sq.fileno() >= 0
    sq.close()
    sq.close()   # idempotent
    assert sq._getsocket.fileno() == -1
    assert sq._putsocket.fileno() == -1

