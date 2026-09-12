# SPDX-License-Identifier: LGPL-3.0-only
"""Tests for the ``_ProtocolAdapter`` shim in ``atemwire.profile._common``.

The adapter lets callers pass an ``AtemProtocol`` to ``Profile.apply``
without first opening an ``ATEMConnection``. A bulk-upload job uses
this so a macro XML can be applied on the same short-lived upload
socket after image upload, with no second handshake and no extra
socket.

These tests pin the surface the apply functions actually reach for:
``.send(cmd) -> protocol.send_commands([cmd])``, ``.protocol`` returning
the wrapped protocol, ``.is_connected`` mirroring ``.connected``, and
``.mixerstate`` proxying the dict. They also pin the
``_resolve_connection`` selection logic so an ATEMConnection-like
object keeps taking the simpler path.
"""

import pytest

from atemwire.profile._common import _ProtocolAdapter, _resolve_connection


class _FakeAtemProtocol:
    """Minimal stand-in: ``send_commands`` queue + ``mixerstate`` dict +
    ``connected`` flag. No ``send`` attribute (that's the whole point —
    AtemProtocol doesn't have one)."""

    def __init__(self, connected=True):
        self.connected = connected
        self.mixerstate = {'video-mode': 'fake'}
        self.sent = []

    def send_commands(self, cmds):
        self.sent.extend(cmds)


class _FakeATEMConnection:
    """Has ``send(Command)`` — the ATEMConnection surface."""

    def __init__(self):
        self.sent = []

    def send(self, command):
        self.sent.append(command)


class _FakeATEMFacade:
    """Has ``.raw`` -> ATEMConnection (the public ATEM wrapper shape)."""

    def __init__(self):
        self.raw = _FakeATEMConnection()


def test_adapter_send_translates_to_send_commands():
    p = _FakeAtemProtocol()
    a = _ProtocolAdapter(p)
    a.send('cmd-a')
    a.send('cmd-b')
    assert p.sent == ['cmd-a', 'cmd-b']


def test_adapter_protocol_attribute_returns_wrapped_protocol():
    p = _FakeAtemProtocol()
    a = _ProtocolAdapter(p)
    assert a.protocol is p


def test_adapter_is_connected_mirrors_protocol_connected_flag():
    p = _FakeAtemProtocol(connected=True)
    a = _ProtocolAdapter(p)
    assert a.is_connected is True

    p.connected = False
    assert a.is_connected is False


def test_adapter_mixerstate_proxies_dict():
    p = _FakeAtemProtocol()
    p.mixerstate['foo'] = 'bar'
    a = _ProtocolAdapter(p)
    assert a.mixerstate['foo'] == 'bar'
    # Mutation through the adapter must propagate.
    a.mixerstate['baz'] = 'qux'
    assert p.mixerstate['baz'] == 'qux'


def test_resolve_connection_prefers_raw_for_atem_facade():
    facade = _FakeATEMFacade()
    resolved = _resolve_connection(facade)
    assert resolved is facade.raw


def test_resolve_connection_returns_atem_connection_unchanged():
    conn = _FakeATEMConnection()
    resolved = _resolve_connection(conn)
    assert resolved is conn


def test_resolve_connection_wraps_atem_protocol_in_adapter():
    proto = _FakeAtemProtocol()
    resolved = _resolve_connection(proto)
    assert isinstance(resolved, _ProtocolAdapter)
    assert resolved.protocol is proto

    # And the wrapped object speaks the ATEMConnection surface.
    resolved.send('test-cmd')
    assert proto.sent == ['test-cmd']


def test_resolve_connection_rejects_unrelated_object():
    class _Unrelated:
        pass

    with pytest.raises(TypeError, match='ATEM/ATEMConnection/AtemProtocol'):
        _resolve_connection(_Unrelated())
