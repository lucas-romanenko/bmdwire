# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""HyperDeck binding messages — the ATEM's own HyperDeck control settings.

The ATEM acts as a HyperDeck client: in Software Control's HyperDecks
palette you add a deck by IP, connect, and assign it a switcher input.
The ATEM stores that binding and broadcasts it to every connected client
via ``RXMS``. Parsing it lets us read the deck's IP and the input it feeds
straight from the ATEM — per-deployment-correct, no hardcoding.

The ATEM emits all 10 slots regardless; slots with network address
0.0.0.0 are unconfigured.
"""

import socket
import struct

from atemwire.state import safe_int
from atemwire.messages._dsl import Recv, Send, u16


class HyperdeckSettingsField(Recv):
    """``RXMS`` — per-slot HyperDeck binding (IP + switcher input).

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      2    u16    HyperDeck slot id (0..9)
    4      4    u32    Network address (IPv4, big-endian); 0 = unconfigured
    8      2    u16    Switcher input the deck feeds
    ====== ==== ====== ===========

    Layout verified live 2026-05-29 against an ATEM with a deck bound at
    192.0.2.10 on input 4:
    ``RXMS[0] = 00 00 | 00 00 | c0 00 02 0a | 00 04 | ...``.
    """
    CODE = 'RXMS'
    PRETTY = 'hyperdeck-settings'
    KEY_FORMAT = struct.Struct('>H')   # index by slot id (u16 @ offset 0)
    HAND_PARSED_OFFSETS = (4,)         # the IPv4 address below is decoded by hand

    index = u16(at=0)
    input = u16(at=8)

    def __init__(self, raw: bytes):
        super().__init__(raw)
        self.network_address = socket.inet_ntoa(raw[4:8])
        self.configured = raw[4:8] != b'\x00\x00\x00\x00'

    def __repr__(self):
        return (f'<hyperdeck-settings id={self.index} '
                f'ip={self.network_address} input={self.input} '
                f'configured={self.configured}>')


class HyperdeckSettingsCommand(Send):
    """``CXMS`` — set a HyperDeck binding (network address + switcher input).

    Reverse-engineered from a Wireshark capture of ATEM Software Control
    adding a HyperDeck (2026-06-09) and validated live: ASC sends one CXMS
    per changed field, and the ATEM echoes the result back via ``RXMS``.

    ====== ==== ====== ===========
    Offset Size Type   Description
    ====== ==== ====== ===========
    0      1    u8     Field mask (bit 0 = network address, bit 1 = input)
    1      1    ?      reserved (ASC leaves it uninitialised; we send 0)
    2      2    u16    HyperDeck slot id (0..9)
    4      4    u32    Network address (IPv4, big-endian)
    8      2    u16    Switcher input the deck feeds
    10     6    ?      auto-roll / frame-delay — not yet mapped; sent as 0
    ====== ==== ====== ===========

    Only the fields whose mask bit is set are applied, so passing one of
    ``network_address`` / ``switcher_input`` leaves the other untouched on
    the switcher (same partial-write contract as the other setters).
    """
    CODE = 'CXMS'

    def __init__(self, slot, network_address=None, switcher_input=None):
        self.slot = int(slot)
        self.network_address = network_address
        self.switcher_input = switcher_input

    def get_command(self) -> bytes:
        mask = 0
        if self.network_address is not None:
            mask |= 0x01
        if self.switcher_input is not None:
            mask |= 0x02
        ip = (socket.inet_aton(self.network_address)
              if self.network_address is not None else b'\x00\x00\x00\x00')
        data = (bytes([mask, 0])
                + struct.pack('>H', self.slot)
                + ip
                + struct.pack('>H', int(self.switcher_input or 0))
                + b'\x00' * 6)
        header = struct.pack('>H 2x 4s', len(data) + 8, self.CODE.encode())
        return header + data


# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


def set_hyperdeck_settings(conn, *, slot, network_address=None,
                           switcher_input=None):
    """Set a HyperDeck binding's network address and/or switcher input (CXMS).

    Pass either or both; only provided fields are written. ``network_address``
    is a dotted-decimal IPv4 string (``'0.0.0.0'`` clears the slot).
    """
    conn.send(HyperdeckSettingsCommand(
        slot=slot, network_address=network_address,
        switcher_input=switcher_input))


def plan_binding_push(mx, deck_ip, switcher_input, *, exclude_slots=()):
    """Decide the CXMS write (if any) that binds ``deck_ip`` to
    ``switcher_input``: returns ``(slot, needs_write)``.

    Reads the current RXMS bindings from ``mx`` (mixerstate). Reuses the
    slot already holding ``deck_ip`` if there is one, else picks the first
    unconfigured slot not in ``exclude_slots`` (callers assigning several
    decks in one pass track slots they just claimed there — the RXMS echo
    hasn't landed yet). ``slot`` is ``None`` when no slot is available;
    ``needs_write`` is False when the binding is already correct.

    This is the ONE slot-selection/drift decision — callers driving a bare
    ``AtemProtocol`` and callers on a pooled ``ATEMConnection`` both plan
    here and send with their own mechanics; the two used to carry drifting
    copies of this logic.
    """
    hd_state = mx.get('hyperdeck-settings') or {}
    slot_ids = sorted(hd_state.keys()) if hd_state else list(range(10))
    binds = {s: hyperdeck_settings(mx, s) for s in slot_ids}
    slot = next((s for s, b in binds.items()
                 if b['configured'] and b['network_address'] == deck_ip), None)
    if slot is None:
        slot = next((s for s, b in binds.items()
                     if not b['configured'] and s not in exclude_slots), None)
    if slot is None:
        return None, False
    cur = binds[slot]
    needs_write = (cur['network_address'] != deck_ip
                   or int(cur['input'] or 0) != int(switcher_input))
    return slot, needs_write


# -----------------------------------------------------------------------------
# Readers
# -----------------------------------------------------------------------------


def hyperdeck_settings(mx, idx):
    """One HyperDeck slot's binding: ``{slot, network_address, input,
    configured}``. ``configured`` is False for an unbound slot (IP 0.0.0.0)."""
    node = (mx.get('hyperdeck-settings') or {}).get(idx)
    if node is None:
        return {'slot': idx, 'network_address': '0.0.0.0',
                'input': 0, 'configured': False}
    return {
        'slot': idx,
        'network_address': getattr(node, 'network_address', '0.0.0.0'),
        'input': safe_int(getattr(node, 'input', 0), 0),
        'configured': bool(getattr(node, 'configured', False)),
    }
