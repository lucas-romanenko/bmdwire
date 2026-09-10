# SPDX-License-Identifier: LGPL-3.0-only
"""ATEMConnection.download_still — the native interleaved still download
(ASC-style step 2, 2026-07-02).

Contract under test:
  * a matching download-done event returns the frame bytes; progress
    fractions reach the callback; both handlers are unregistered after;
  * events for OTHER slots never satisfy the wait;
  * a timeout aborts the protocol's transfer state (so the shared
    connection's transfer lane can't stay wedged for later callers) and
    raises TimeoutError;
  * a dead connection raises ConnectionDeadError before touching the wire;
  * protocol.abort_transfers resets the transfer state machine and
    releases held store locks.
"""
import threading
import types

import pytest

from atemwire.connection import ATEMConnection, ConnectionDeadError
from atemwire.protocol import AtemProtocol


class _FakeProto:
    def __init__(self):
        self.callbacks = {}
        self.idx = 0
        self.downloads = []
        self.aborted = 0

    def on(self, event, cb):
        self.callbacks.setdefault(event, {})[self.idx] = cb
        self.idx += 1
        return self.idx - 1

    def off(self, event, cb_id):
        del self.callbacks[event][cb_id]

    def _raise(self, event, *args):
        for cb in list(self.callbacks.get(event, {}).values()):
            cb(*args)

    def download(self, store, slot):
        self.downloads.append((store, slot))

    def abort_transfers(self):
        self.aborted += 1


def _conn(proto, connected=True):
    ns = types.SimpleNamespace()
    ns.is_connected = connected
    ns.ip_address = '10.0.0.1'
    ns._protocol = proto
    ns._transfer_serial_lock = threading.Lock()
    # download_still/download_macro delegate to the shared helper — bind it
    # so the unbound wrapper calls work against this stand-in.
    ns._download_transfer = ATEMConnection._download_transfer.__get__(ns)
    return ns


def test_returns_frame_and_unregisters_handlers():
    class _P(_FakeProto):
        def download(self, store, slot):
            super().download(store, slot)
            self._raise('transfer-progress', 0, slot, 0.5)
            self._raise('download-done', 0, slot, b'FRAME')

    p = _P()
    fractions = []
    data = ATEMConnection.download_still(
        _conn(p), 3, timeout=1.0, progress_callback=fractions.append)

    assert data == b'FRAME'
    assert p.downloads == [(0, 3)]
    assert fractions == [0.5]
    assert p.callbacks['download-done'] == {}      # one-shot: unregistered
    assert p.callbacks['transfer-progress'] == {}
    assert p.aborted == 0


def test_other_slots_event_does_not_satisfy():
    class _P(_FakeProto):
        def download(self, store, slot):
            self._raise('download-done', 0, slot + 1, b'WRONG')

    with pytest.raises(TimeoutError):
        ATEMConnection.download_still(_conn(_P()), 3, timeout=0.05)


def test_timeout_aborts_transfer_state():
    p = _FakeProto()                               # never answers
    with pytest.raises(TimeoutError, match='slot 3'):
        ATEMConnection.download_still(_conn(p), 3, timeout=0.05)
    assert p.aborted == 1                          # lane un-wedged for the next caller
    assert p.callbacks['download-done'] == {}


def test_timeout_names_foreign_lock_holder():
    """A timeout where the store lock was NEVER granted is the signature of
    another client holding the ATEM's media lock (the most common cause of
    'images won't load') — the error must say so instead of a bare timeout."""
    p = _FakeProto()
    p.locks = {}                                   # PLCK sent, LKOB never came
    with pytest.raises(TimeoutError, match='another client is holding it'):
        ATEMConnection.download_still(_conn(p), 3, timeout=0.05)

    p2 = _FakeProto()
    p2.locks = {0: True}                           # lock granted; data stalled
    with pytest.raises(TimeoutError) as excinfo:
        ATEMConnection.download_still(_conn(p2), 3, timeout=0.05)
    assert 'another client' not in str(excinfo.value)


def test_dead_connection_raises_before_wire():
    p = _FakeProto()
    with pytest.raises(ConnectionDeadError):
        ATEMConnection.download_still(_conn(p, connected=False), 0, timeout=0.05)
    assert p.downloads == []


def test_protocol_abort_transfers_resets_and_unlocks():
    p = AtemProtocol(ip='127.0.0.1')
    try:
        p.locks[0] = True
        p.transfer_requested = True
        p.transfer = object()
        p.transfer_queue = {0: [object()]}
        p.transfer_buffer = [b'partial']
        p.transfer_buffer_bytes = 7

        p.abort_transfers()

        assert p.transfer_queue == {}
        assert p.transfer is None
        assert p.transfer_requested is False
        assert p.transfer_buffer == []
        assert p.transfer_buffer_bytes == 0
        assert p.locks[0] is False                 # unlock sent, belief reset
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass


def test_download_macro_uses_macro_store():
    """download_macro targets store 0xFFFF (no still-store lock involved)
    and returns the raw bytes for decode_macro_bytecode; an empty macro
    slot yields b'' (FTDC with no FTDa)."""
    class _P(_FakeProto):
        def download(self, store, slot):
            super().download(store, slot)
            self._raise('download-done', store, slot, b'')   # empty macro

    p = _P()
    data = ATEMConnection.download_macro(_conn(p), 5, timeout=1.0)
    assert data == b''
    assert p.downloads == [(0xFFFF, 5)]
    assert p.callbacks['download-done'] == {}


# ---------------------------------------------------------------------------
# ATEMConnection.clear_still — the LOCKED clear (2026-07-29)
#
# Some ATEMs (1 M/E Production Studio 4K) silently ignore a bare CSTL from a
# session that does not hold the still-store lock (a Constellation HD honours
# it, so it's model-dependent). clear_still rides the
# transfer machinery's lock discipline: LOCK → LKOB → CSTL → per-frame
# release. Contract: returns once the CSTL is dispatched under the lock;
# a timeout (lock never granted) withdraws the queued task so a stale clear
# can't fire later against a slot an operator may have since reused.
# ---------------------------------------------------------------------------

from atemwire.messages import (
    ClearStillCommand,
    LockCommand,
    PartialLockCommand,
)


class _ClearProto(_FakeProto):
    def __init__(self):
        super().__init__()
        self.clears = []
        self.dequeued = []

    def queue_clear(self, store, index):
        self.clears.append((store, index))
        task = types.SimpleNamespace(store=store, slot=index, clear=True)
        self._task = task
        return task

    def dequeue_clear(self, task):
        self.dequeued.append(task)
        return True


def test_clear_still_returns_on_dispatch_and_unregisters():
    class _P(_ClearProto):
        def queue_clear(self, store, index):
            task = super().queue_clear(store, index)
            self._raise('clear-dispatched', store, index)
            return task

    p = _P()
    ATEMConnection.clear_still(_conn(p), 4, timeout=1.0)
    assert p.clears == [(0, 4)]
    assert p.dequeued == []                        # dispatched — not withdrawn
    assert p.callbacks['clear-dispatched'] == {}   # one-shot: unregistered


def test_clear_still_other_slot_dispatch_does_not_satisfy():
    class _P(_ClearProto):
        def queue_clear(self, store, index):
            task = super().queue_clear(store, index)
            self._raise('clear-dispatched', store, index + 1)
            return task

    with pytest.raises(TimeoutError):
        ATEMConnection.clear_still(_conn(_P()), 4, timeout=0.05)


def test_clear_still_timeout_withdraws_the_queued_task():
    p = _ClearProto()                              # lock never granted
    with pytest.raises(TimeoutError, match='media lock'):
        ATEMConnection.clear_still(_conn(p), 4, timeout=0.05)
    assert p.dequeued == [p._task]                 # stale clear withdrawn
    assert p.callbacks['clear-dispatched'] == {}


def test_clear_still_dead_connection_raises_before_wire():
    p = _ClearProto()
    with pytest.raises(ConnectionDeadError):
        ATEMConnection.clear_still(_conn(p, connected=False), 4, timeout=0.05)
    assert p.clears == []


# --- the real protocol machinery -------------------------------------------


def test_queue_clear_requests_full_lock_not_plck():
    """A clear task must take the FULL store lock (LOCK): CSTL was verified
    honoured under a LOCK grant; a PLCK per-slot grant is unverified for
    clears, and a silently-dropped clear is the failure mode this exists
    to prevent."""
    p = AtemProtocol(ip='127.0.0.1')
    sent = []
    p.send_commands = lambda cmds: sent.extend(cmds)
    try:
        task = p.queue_clear(0, 6)
        assert p.transfer_queue[0] == [task]       # queued until LKOB
        assert len(sent) == 1
        assert isinstance(sent[0], LockCommand)
        assert not isinstance(sent[0], PartialLockCommand)
        assert sent[0].store == 0 and sent[0].state is True
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass


def test_queue_clear_dispatches_cstl_under_lock_then_releases():
    """With the lock held (LKOB arrived), the trigger pops the task, sends
    the CSTL, raises clear-dispatched, and releases per-frame — the CSTL
    is on the wire BEFORE the unlock (in-order delivery: the switcher
    processes it as lock holder)."""
    p = AtemProtocol(ip='127.0.0.1')
    sent = []
    p.send_commands = lambda cmds: sent.extend(cmds)
    dispatched = []
    p.on('clear-dispatched', lambda store, slot: dispatched.append((store, slot)))
    try:
        task = p.queue_clear(0, 6)
        del sent[:]                                # drop the LOCK request
        p.locks[0] = True                          # simulate LKOB
        p._transfer_trigger(0)

        assert p.transfer_queue[0] == []           # task popped
        assert [type(c) for c in sent] == [ClearStillCommand, LockCommand]
        assert sent[0].slot == 6
        assert sent[1].state is False              # per-frame release
        assert dispatched == [(0, 6)]
        assert p.locks.get(0) is False             # belief reset
        assert 0 in p._lock_release_pending        # echo-gated re-requests
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass


def test_dequeue_clear_removes_pending_task_only():
    p = AtemProtocol(ip='127.0.0.1')
    p.send_commands = lambda cmds: None
    try:
        task = p.queue_clear(0, 6)
        assert p.dequeue_clear(task) is True
        assert p.transfer_queue[0] == []
        assert p.dequeue_clear(task) is False      # already gone: no-op
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass


def test_atem_facade_clear_still_is_the_locked_override():
    """The ATEM facade must shadow the bare messages op with an explicit
    member routing to ATEMConnection.clear_still — deleting the override
    would silently fall back to __getattr__'s bare CSTL, which some
    models ignore."""
    from atemwire.atem import ATEM
    assert 'clear_still' in ATEM.__dict__
