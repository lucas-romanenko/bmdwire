# SPDX-License-Identifier: LGPL-3.0-only
"""
State / connection resolution helpers — accept an ATEM, ATEMConnection,
or AtemProtocol and return the right thing for the caller's purpose
(mixerstate dict for reads, send-capable conn for writes, optional
protocol for raw transfers).

Plus ``_set_attrs`` for adding XML attributes in BMD's formatted style.
(The state-dump settle wait moved to ``atemwire.ready.wait_state_settled``
— it's a readiness concern, not a profile one, and application callers
needed it without reaching into profile internals.)
"""

import xml.etree.ElementTree as ET

from atemwire.profile._xml import _fmt


def _resolve_mixerstate(atem) -> dict:
    """Return the mixerstate dict regardless of whether the caller passed
    an ATEM, an ATEMConnection, or a raw AtemProtocol."""
    if hasattr(atem, 'mixerstate'):
        return atem.mixerstate
    if hasattr(atem, 'raw') and hasattr(atem.raw, 'mixerstate'):
        return atem.raw.mixerstate
    raise TypeError(
        f"from_atem expected ATEM/ATEMConnection/AtemProtocol, got {type(atem).__name__}"
    )


class _ProtocolAdapter:
    """Adapter that makes an ``AtemProtocol`` quack like an
    ``ATEMConnection`` for the atemwire operation API.

    Operation wrappers expect ``conn.send(Command)``; ``AtemProtocol``
    exposes ``send_commands([Command, ...])``. The adapter bridges
    that gap so callers that already hold a connected protocol (e.g.
    an upload job that opens a short-lived
    ``AtemProtocol`` with ``aggressive_drain=True`` and applies a
    macro XML on the same socket after image upload) can pass it to
    ``Profile.apply`` without opening a parallel ``ATEMConnection``.

    The surface mirrors the subset of ``ATEMConnection`` the apply
    functions reach for: ``.send(cmd)``, ``.protocol``,
    ``.is_connected``, ``.mixerstate``.
    """

    def __init__(self, protocol):
        self._protocol = protocol

    def send(self, command):
        self._protocol.send_commands([command])

    @property
    def protocol(self):
        return self._protocol

    @property
    def is_connected(self):
        return bool(getattr(self._protocol, 'connected', False))

    @property
    def mixerstate(self):
        return self._protocol.mixerstate


def _resolve_connection(atem):
    """Return an object suitable for passing to atemwire.operations functions
    (which expect ``conn.send(Command)``). Accepts ATEM, ATEMConnection,
    or AtemProtocol (the latter via the ``_ProtocolAdapter`` shim)."""
    if hasattr(atem, 'raw'):
        return atem.raw  # ATEM facade → ATEMConnection
    if hasattr(atem, 'send'):
        return atem  # ATEMConnection
    # AtemProtocol: has send_commands(list) + mixerstate but no .send.
    if hasattr(atem, 'send_commands') and hasattr(atem, 'mixerstate'):
        return _ProtocolAdapter(atem)
    raise TypeError(
        f"apply expected ATEM/ATEMConnection/AtemProtocol, "
        f"got {type(atem).__name__}"
    )


def _set_attrs(elem: ET.Element, **attrs) -> None:
    """Add formatted attribute strings, in the given keyword order."""
    for k, v in attrs.items():
        elem.set(k, _fmt(v))


def _resolve_protocol_or_none(atem):
    """Best-effort: return the underlying ``AtemProtocol`` for an ``ATEM``
    facade or an ``ATEMConnection`` argument. Returns ``None`` for
    objects that don't expose one (e.g. test fakes that only carry a
    ``mixerstate`` attribute) — the caller is expected to gracefully
    skip protocol-only operations."""
    raw = getattr(atem, 'raw', None)
    if raw is not None:
        protocol = getattr(raw, 'protocol', None)
        if protocol is not None:
            return protocol
    protocol = getattr(atem, 'protocol', None)
    if protocol is not None:
        return protocol
    return None

