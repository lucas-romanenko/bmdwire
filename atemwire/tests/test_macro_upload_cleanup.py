# SPDX-License-Identifier: LGPL-3.0-only
"""A failed macro upload cleans up after itself, and a slow one is given
the time it needs.

The switcher makes the slot a nameless macro as a macro upload starts (it
pre-erases). An upload that timed out or was refused left that behind
("Macro 100" on a Constellation HD after the WebATEM #63 bench), and its
task stayed in the transfer queue. The timeout was the whole upload's;
it is now seconds without progress.
"""
import threading
import time
import types

import pytest

from atemwire._transfer import TransferTask
from atemwire.macrotransfer import _io
from atemwire.macrotransfer._io import MACRO_STORE, upload_macro_bytecode
from atemwire.messages.macros import MacroActionCommand
from atemwire.protocol import AtemProtocol


class _Proto:
    """What upload_macro_bytecode uses of an AtemProtocol; ``script`` runs
    on its own thread once the upload is queued, as the switcher would."""

    def __init__(self, used=False, script=None):
        self.connected = True
        self.mixerstate = {'macro-properties': {
            5: types.SimpleNamespace(is_used=used)}}
        self.transfer_queue = {}
        self.transport = types.SimpleNamespace(_acked_through=1)
        self.handlers = {}
        self.sent = []
        self.cancelled = []
        self.script = script

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)
        return (event, fn)

    def off(self, event, hid):
        self.handlers[hid[0]].remove(hid[1])

    def raise_(self, event, *args):
        for fn in list(self.handlers.get(event, [])):
            fn(*args)

    def upload(self, store, slot, data, **kw):
        task = TransferTask(store, slot, upload=True)
        self.transfer_queue.setdefault(store, []).append(task)
        if self.script:
            threading.Thread(target=self.script, args=(self, task), daemon=True).start()

    def cancel_transfer(self, task):
        self.cancelled.append(task)
        self.transfer_queue[task.store].remove(task)
        return True

    def send_commands(self, cmds):
        self.sent.extend(cmds)


def deletes(p):
    return [c.index for c in p.sent
            if isinstance(c, MacroActionCommand) and c.action == MacroActionCommand.ACTION_DELETE]


def test_a_timed_out_upload_into_an_empty_slot_empties_it_again():
    p = _Proto(used=False)                      # nothing ever comes back
    with pytest.raises(TimeoutError):
        upload_macro_bytecode(p, 5, 'm', '', b'\x00' * 64, timeout=0.2)
    assert deletes(p) == [5], 'the nameless macro it left is deleted'
    assert p.cancelled and p.transfer_queue[MACRO_STORE] == [], 'its task is off the queue'


def test_a_refused_upload_into_an_empty_slot_empties_it_again():
    def refuse(p, task):
        time.sleep(0.05)
        p.raise_('file-transfer-error', types.SimpleNamespace(status=2, transfer=1))
    p = _Proto(used=False, script=refuse)
    with pytest.raises(RuntimeError, match='rejected'):
        upload_macro_bytecode(p, 5, 'm', '', b'\x00' * 64, timeout=2)
    assert deletes(p) == [5]


def test_a_slot_that_held_a_macro_is_not_deleted_and_the_error_says_it_is_gone():
    p = _Proto(used=True)
    with pytest.raises(TimeoutError, match='held a macro'):
        upload_macro_bytecode(p, 5, 'm', '', b'\x00' * 64, timeout=0.2)
    assert deletes(p) == []


def test_a_slot_the_state_does_not_know_is_left_alone():
    p = _Proto()
    p.mixerstate = {}
    with pytest.raises(TimeoutError):
        upload_macro_bytecode(p, 5, 'm', '', b'\x00' * 64, timeout=0.2)
    assert deletes(p) == []


def test_a_successful_upload_deletes_nothing():
    def finish(p, task):
        time.sleep(0.05)
        p.raise_('upload-done', MACRO_STORE, 5)
    p = _Proto(script=finish)
    upload_macro_bytecode(p, 5, 'm', '', b'\x00' * 64, timeout=2)
    assert deletes(p) == [] and p.cancelled == []


def test_a_slow_upload_that_keeps_moving_is_not_cut_off():
    # 1.14.7: 10 s for the whole upload, however it was going. Now the
    # limit is time without progress: here 0.3 s, and an upload that takes
    # 1 s while its ACKs keep coming finishes.
    def slow(p, task):
        for _ in range(10):
            time.sleep(0.1)
            p.transport._acked_through += 1     # the switcher takes another packet
        p.raise_('upload-done', MACRO_STORE, 5)
    p = _Proto(script=slow)
    t = time.monotonic()
    upload_macro_bytecode(p, 5, 'm', '', b'\x00' * 64, timeout=0.3)
    assert time.monotonic() - t >= 0.9


# --- AtemProtocol.cancel_transfer: ours only -------------------------------

@pytest.fixture
def proto(monkeypatch):
    p = AtemProtocol.__new__(AtemProtocol)
    p.transfer_queue = {}
    p.transfer = None
    p.transfer_requested = False
    p._try_again_due = None
    p.transfer_buffer = []
    p.transfer_buffer_bytes = 0
    p.log = types.SimpleNamespace(info=lambda *a: None)
    p.continued = []
    monkeypatch.setattr(p, '_release_then_continue',
                        lambda store, request_next=True: p.continued.append(store), raising=False)
    return p


def test_a_waiting_transfer_is_taken_off_the_queue_and_the_rest_stay(proto):
    a, b, c = (TransferTask(MACRO_STORE, i, upload=True) for i in range(3))
    proto.transfer_queue[MACRO_STORE] = [a, b, c]
    proto.transfer = a
    assert proto.cancel_transfer(b) is True
    assert proto.transfer_queue[MACRO_STORE] == [a, c]
    assert proto.transfer is a and proto.continued == []


def test_the_running_transfer_is_dropped_and_the_next_goes(proto):
    a, b = (TransferTask(MACRO_STORE, i, upload=True) for i in range(2))
    proto.transfer_queue[MACRO_STORE] = [a, b]
    proto.transfer = a
    proto.transfer_requested = True
    proto.transfer_buffer = [b'x']
    assert proto.cancel_transfer(a) is True
    assert proto.transfer_queue[MACRO_STORE] == [b]
    assert proto.transfer_buffer == [] and not proto.transfer_requested
    assert proto.continued == [MACRO_STORE]


def test_a_transfer_gone_already_is_left(proto):
    a = TransferTask(MACRO_STORE, 0, upload=True)
    assert proto.cancel_transfer(a) is False
