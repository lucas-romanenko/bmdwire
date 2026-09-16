# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Lock messages — request / release a datastore lock for bulk transfers.
The ATEM grants exclusive access to a store via the lock protocol; the
file-transfer commands (FTSU / FTSD etc.) require a held lock first.

Wire packets:
    LOCK — outgoing, request or release a full-store lock
    PLCK — outgoing, request a partial (per-slot) lock
    LKOB — incoming, lock-obtained notification
    LKST — incoming, current lock state for a store
"""

from atemwire.messages._dsl import Recv, Send, boolean, u8, u16


# -----------------------------------------------------------------------------
# Outgoing — lock requests
# -----------------------------------------------------------------------------


class LockCommand(Send):
    """``LOCK`` — request or release a full-store lock.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Store index
    2      1    bool   Lock state (True = request, False = release)
    3      1    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'LOCK'
    SIZE = 4

    store = u16    (at=0)
    state = boolean(at=2)

    def __init__(self, store, state):
        super().__init__(store=store, state=state)


class PartialLockCommand(Send):
    """``PLCK`` — request a partial (per-slot) lock for raw-socket transfers.

    The trailing two bytes ``0xff 0x01`` are constants — observed in every
    Software Control PLCK packet. Their meaning isn't documented; they're
    written as literal values to match the reference protocol exactly.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Store index
    2      2    u16    Slot number
    4      1    u8     Literal 0xff
    5      1    u8     Literal 0x01
    6      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'PLCK'
    SIZE = 8

    store     = u16(at=0)
    slot      = u16(at=2)
    _const_ff = u8 (at=4)
    _const_01 = u8 (at=5)

    def __init__(self, store, slot):
        super().__init__(store=store, slot=slot,
                         _const_ff=0xff, _const_01=0x01)


# -----------------------------------------------------------------------------
# Incoming — lock notifications
# -----------------------------------------------------------------------------


class LockObtainedField(Recv):
    """``LKOB`` — datastore lock was successfully obtained.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Store index
    2      2    ?      padding
    ====== ==== ====== ===========
    """
    CODE = 'LKOB'
    PRETTY = 'lock-obtained'

    store = u16(at=0)

    def __repr__(self):
        return f'<lock-obtained store={self.store}>'


class LockStateField(Recv):
    """``LKST`` — current lock state for a store.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    Store index
    2      1    bool   Locked
    3      1    u8     (unknown — preserved as ``u1`` for compat)
    ====== ==== ====== ===========
    """
    CODE = 'LKST'
    PRETTY = 'lock-state'

    store = u16    (at=0)
    state = boolean(at=2)
    u1    = u8     (at=3)

    def __repr__(self):
        state = 'locked' if self.state else 'unlocked'
        return f'<lock-state store={self.store} state={state}>'
