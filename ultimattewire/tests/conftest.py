# SPDX-License-Identifier: MIT
"""Shared fakes and synthetic fixtures for the ultimattewire suite.

Nothing here touches a network. Helpers are exposed as fixtures rather
than imported by module path so the same files run against a source
checkout and against an installed wheel (which ships without tests).
"""

import socket
import types
from typing import List

import pytest

import ultimattewire
import ultimattewire._protocol as protocol

HOST = "192.0.2.21"

# Synthetic 9998 prelude: the unit's shape (CAPS section headers ending in a
# colon, ``key: value`` lines, a FILE LIST of preset names, ``END PRELUDE:``)
# with made-up values.
SAMPLE_PRELUDE = (
    "Label: Keyer A\n"
    "Software Release: 2.0\n"
    "\n"
    "CONTROL:\n"
    "Matte Density: 5000\n"
    "Transition Rate: 30\n"
    "Offset Black Balance: 2500\n"
    "Not A Param: 12\n"
    "FILE LIST:\n"
    "preset-a\n"
    "preset-b\n"
    "\n"
    "GPI SETTINGS:\n"
    "\n"
    "END PRELUDE:\n"
)


class FakeSocket:
    """Scripted stand-in for a 9996/9998 socket.

    ``chunks`` is the byte stream the unit sends, one item per ``recv``;
    an exhausted script is EOF. A ``socket.timeout`` instance in the
    script is raised by ``recv`` instead of returned.
    """

    def __init__(self, chunks: List):
        self._chunks = [c for c in chunks if c != b""]
        self.sent = b""
        self.closed = False
        self.timeouts: list = []

    def settimeout(self, t):
        self.timeouts.append(t)

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self._chunks:
            return b""
        head = self._chunks[0]
        if isinstance(head, Exception):
            self._chunks.pop(0)
            raise head
        take, rest = head[:n], head[n:]
        if rest:
            self._chunks[0] = rest
        else:
            self._chunks.pop(0)
        return take

    def close(self):
        self.closed = True


def pytest_report_header(config):
    return f"ultimattewire imported from {ultimattewire.__file__}"


@pytest.fixture
def host():
    return HOST


@pytest.fixture
def sample_prelude():
    return SAMPLE_PRELUDE


@pytest.fixture
def fake_socket_cls():
    return FakeSocket


@pytest.fixture
def fake_binary_socket(monkeypatch):
    """Route ``_protocol``'s ``socket.create_connection`` to a FakeSocket
    without touching the global socket module. Returns a factory that
    installs a script and hands back the socket for assertions."""
    def _install(chunks):
        sock = FakeSocket(chunks)
        fake_module = types.SimpleNamespace(
            create_connection=lambda addr, timeout=None: sock,
            timeout=socket.timeout,
        )
        monkeypatch.setattr(protocol, "socket", fake_module)
        monkeypatch.setattr(protocol, "_CONNECT_RETRY_DELAY", 0)
        return sock
    return _install


def _load_fake_module(file="fake_ultimatte.py"):
    # By file path, not by import name: the tests also run against an
    # installed wheel, which ships without ultimattewire.tests.
    import importlib.util
    from pathlib import Path
    path = Path(__file__).with_name(file)
    spec = importlib.util.spec_from_file_location(
        "_ultimatte_" + file[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_FAKE = _load_fake_module()
_FAKE_4K = _load_fake_module("fake_captured_4k.py")


@pytest.fixture
def fake_4k():
    """A fake Ultimatte 12 4K doing what the real one did in the 2026-11-11
    and -12 captures (named frame buffers, image upload on 9996, delete,
    flush, sampling, the cursor and its colour stream); ``.png(w, h)`` and
    ``.jpg(w, h)`` make image headers to upload. Stopped after the test."""
    unit = _FAKE_4K.FakeCaptured4K().start()
    unit.png = _FAKE_4K.png
    unit.jpg = _FAKE_4K.jpg
    yield unit
    unit.stop()


@pytest.fixture
def fake_unit():
    """Start fake Ultimattes speaking the documented 9998 protocol on
    127.0.0.1: ``fake_unit(model="8K", protocol="2.0")``. Each is stopped
    at the end of the test."""
    started = []

    def _start(model="8K", protocol="2.0", **attrs):
        unit = _FAKE.FakeUltimatte(model, protocol)
        for k, v in attrs.items():
            setattr(unit, k, v)
        started.append(unit.start())
        return unit
    yield _start
    for unit in started:
        unit.stop()


@pytest.fixture
def fake_module():
    return _FAKE
