# SPDX-License-Identifier: LGPL-3.0-only
"""Counting a switcher's free sessions by headroom (atemwire.sessions),
against a fake switcher with a session table of a set size."""
import json
import random
import socket
import struct
import threading
import time

import pytest

from atemwire import sessions
from atemwire.sessions import count_sessions, format_result, main

MODEL = 'ATEM 1 M/E Constellation HD'


def _header(flags, length, session, ack=0, seq=0):
    return struct.pack('>HHHHHH', (flags << 11) | length, session, ack, 0, 0, seq)


class TableSwitcher(threading.Thread):
    """A switcher with ``slots`` sessions. A hello gets 0x02 while one is
    free and 0x03 ("no room") when not; the ACK of the 0x02 gets a reliable
    packet under the assigned id carrying ``_pin``; a goodbye (0x04) frees
    the slot. ``taken`` slots start held by someone else. ``answer=False``
    answers nothing once ``answer_first`` hellos have been accepted."""

    def __init__(self, slots, taken=0, *, answer_first=None):
        super().__init__(daemon=True)
        self.slots = slots
        self.taken = taken
        self.answer_first = answer_first
        self.ip = f'127.{random.randint(1, 254)}.{random.randint(1, 254)}.{random.randint(2, 254)}'
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.ip, 9910))
        self.sock.settimeout(0.05)
        self.table = {}        # peer -> assigned session id
        self.pending = set()   # peers answered 0x02, not yet ACKed
        self.next_id = 0x8001
        self.refusals = 0
        self.accepted = 0
        self.goodbyes = 0
        self.most_held = 0
        self.stop = threading.Event()

    def used(self):
        return self.taken + len(self.table) + len(self.pending)

    def run(self):
        while not self.stop.is_set():
            try:
                data, peer = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 12:
                continue
            flags = data[0] >> 3
            if flags & 0x02 and len(data) >= 13:
                op = data[12]
                if op == 0x01:
                    self._hello(peer)
                elif op == 0x04:
                    if self.table.pop(peer, None) is not None:
                        self.goodbyes += 1
            elif flags & 0x10 and peer in self.pending:
                self.pending.discard(peer)
                sid = self.next_id
                self.next_id += 1
                self.table[peer] = sid
                pin = MODEL.encode().ljust(40, b'\0') + bytes([0x1f, 0, 0, 0])
                field = struct.pack('!H2x4s', 8 + len(pin), b'_pin') + pin
                self.sock.sendto(_header(0x01, 12 + len(field), sid, seq=1) + field, peer)
            self.most_held = max(self.most_held, len(self.table))

    def _hello(self, peer):
        if peer in self.table or peer in self.pending:
            return
        if self.answer_first is not None and self.accepted >= self.answer_first:
            return
        code = 0x02 if self.used() < self.slots else 0x03
        if code == 0x02:
            self.pending.add(peer)
            self.accepted += 1
        else:
            self.refusals += 1
        self.sock.sendto(_header(0x02, 20, 0x1337) + bytes([code, 0, 0, 0, 0, 0, 0, 0]), peer)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self.join(1)
        self.sock.close()


def _settled(sw, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and sw.table:
        time.sleep(0.01)


def test_counts_the_free_sessions_and_logs_every_one_off():
    with TableSwitcher(slots=8, taken=5) as sw:
        r = count_sessions(sw.ip)
        _settled(sw)
        assert r['free'] == 3
        assert r['full'] and r['stopped'] == 'no room'
        assert r['model'] == MODEL
        assert r['session_ids'] == [0x8001, 0x8002, 0x8003]
        assert r['accepts'] == ['0200000000000000'] * 3
        assert sw.refusals == 1
        assert sw.goodbyes == 3 and not sw.table
        assert sw.most_held == 3


def test_in_use_is_max_minus_free_once_the_max_is_known(monkeypatch):
    monkeypatch.setitem(sessions.MAX_SESSIONS, MODEL, 8)
    with TableSwitcher(slots=8, taken=5) as sw:
        r = count_sessions(sw.ip)
    assert (r['free'], r['max'], r['in_use']) == (3, 8, 5)
    assert 'in use   5   = max 8 − free 3' in format_result(r)


def test_in_use_is_unknown_until_the_model_is_measured():
    with TableSwitcher(slots=4, taken=1) as sw:
        r = count_sessions(sw.ip)
    assert r['free'] == 3 and r['max'] is None and r['in_use'] is None
    assert 'not measured yet' in format_result(r)


def test_an_idle_switcher_gives_its_max():
    with TableSwitcher(slots=6) as sw:
        r = count_sessions(sw.ip)
    assert r['free'] == 6 and r['full']


def test_a_full_switcher_has_none_free_at_once():
    with TableSwitcher(slots=4, taken=4) as sw:
        t0 = time.monotonic()
        r = count_sessions(sw.ip)
        assert time.monotonic() - t0 < 1.0
    assert r['free'] == 0 and r['full'] and r['session_ids'] == []
    assert 'free     0' in format_result(r)


def test_stops_at_the_limit_and_says_at_least():
    with TableSwitcher(slots=10) as sw:
        r = count_sessions(sw.ip, limit=4)
        _settled(sw)
        assert sw.goodbyes == 4 and not sw.table
    assert r['free'] == 4 and not r['full'] and r['stopped'] == 'limit'
    assert 'at least 4' in format_result(r)


def test_a_switcher_that_stops_answering_is_not_a_full_one():
    with TableSwitcher(slots=8, answer_first=2) as sw:
        r = count_sessions(sw.ip, answer_timeout=0.5)
        _settled(sw)
        assert sw.goodbyes == 2 and not sw.table
    assert r['free'] == 2 and not r['full'] and r['stopped'] == 'no answer'
    assert r['in_use'] is None
    assert 'not complete' in format_result(r)


def test_nothing_at_the_address():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    try:
        r = count_sessions('127.0.0.1', port=port, answer_timeout=0.3)
    finally:
        sock.close()
    assert r['free'] == 0 and r['stopped'] == 'no answer'
    assert 'no answer from the switcher' in format_result(r)


def test_the_command(capsys, monkeypatch):
    monkeypatch.setitem(sessions.MAX_SESSIONS, MODEL, 8)
    with TableSwitcher(slots=8, taken=6) as sw:
        assert main([sw.ip]) == 0
        out = capsys.readouterr()
        assert 'logs each one off' in out.err
        assert out.out.splitlines()[0] == f'{MODEL} ({sw.ip})'
        assert 'in use   6' in out.out
        assert main([sw.ip, '--json']) == 0
        r = json.loads(capsys.readouterr().out)
    assert r['free'] == 2 and r['in_use'] == 6


def test_the_command_refuses_a_zero_limit():
    with pytest.raises(SystemExit):
        main(['192.0.2.1', '--limit', '0'])
