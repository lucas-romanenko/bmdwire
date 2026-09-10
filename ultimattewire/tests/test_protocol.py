# SPDX-License-Identifier: MIT
"""9996 binary-channel framing: reads, writes, ACK handling, connect retry."""

import socket
import struct
import types

import pytest

import ultimattewire._protocol as protocol


# ---- binary_request (read) -------------------------------------------------

def test_binary_request_short_length_header_raises_and_closes(fake_binary_socket, host):
    """2 of 4 header bytes then EOF must raise, never return b''."""
    sock = fake_binary_socket([b"\x00\x00"])
    with pytest.raises(IOError, match="length header"):
        protocol.binary_request(host, 0x0003, "SavedSettings")
    assert sock.closed


def test_binary_request_immediate_eof_raises_and_closes(fake_binary_socket, host):
    """A clean 0-byte EOF is also a failed read: a reply is owed."""
    sock = fake_binary_socket([])
    with pytest.raises(IOError, match="length header"):
        protocol.binary_request(host, 0x0003, "GPISettings")
    assert sock.closed


def test_binary_request_short_body_raises_and_closes(fake_binary_socket, host):
    """Header promises 10 bytes, unit dies after 4."""
    sock = fake_binary_socket([struct.pack(">I", 10), b"abcd"])
    with pytest.raises(IOError, match="4 of 10"):
        protocol.binary_request(host, 0x0000, "slot1")
    assert sock.closed


def test_binary_request_zero_length_resource_returns_empty(fake_binary_socket, host):
    sock = fake_binary_socket([struct.pack(">I", 0)])
    assert protocol.binary_request(host, 0x0003, "GPISettings") == b""
    assert sock.closed


def test_binary_request_full_body_round_trips(fake_binary_socket, host):
    sock = fake_binary_socket([struct.pack(">I", 3), b"abc"])
    body = protocol.binary_request(host, 0x0003, "GPISettings")
    assert body == b"abc"
    assert sock.sent == struct.pack(">HI", 0x0003, len(b"GPISettings")) + b"GPISettings"
    assert sock.closed


def test_binary_request_accepts_bytes_payload(fake_binary_socket, host):
    sock = fake_binary_socket([struct.pack(">I", 1), b"x"])
    assert protocol.binary_request(host, 0x0000, b"slot1") == b"x"
    assert sock.sent.startswith(struct.pack(">HI", 0x0000, 5))


# ---- upload_one (write) ----------------------------------------------------

def _expected_frame(opcode, name, data):
    n = name.encode("utf-8")
    return (struct.pack(">H", opcode) + struct.pack(">I", len(n)) + n
            + struct.pack(">I", len(data)) + data + b"\x00")


def test_upload_one_frame_layout_and_full_ack(fake_binary_socket, host):
    sock = fake_binary_socket([b"\x00" * 6])
    protocol.upload_one(host, 0x0100, "presets/slot1", b"payload")
    assert sock.sent == _expected_frame(0x0100, "presets/slot1", b"payload")
    assert sock.sent.endswith(b"\x00")
    assert sock.closed


def test_upload_one_tolerates_short_all_zero_ack(fake_binary_socket, host):
    """The unit closes right after the ACK; 3 zero bytes then EOF is a pass."""
    sock = fake_binary_socket([b"\x00\x00\x00"])
    protocol.upload_one(host, 0x0103, "GPISettings", b"g")
    assert sock.closed


def test_upload_one_no_ack_bytes_raises(fake_binary_socket, host):
    sock = fake_binary_socket([])
    with pytest.raises(IOError, match="no ACK"):
        protocol.upload_one(host, 0x0103, "GPISettings", b"g")
    assert sock.closed


def test_upload_one_nonzero_ack_raises(fake_binary_socket, host):
    sock = fake_binary_socket([b"\x00\x00\x00\x00\x00\x01"])
    with pytest.raises(IOError, match="non-zero ACK"):
        protocol.upload_one(host, 0x0103, "SavedSettings", b"s")
    assert sock.closed


def test_upload_one_timeout_waiting_for_ack_raises(fake_binary_socket, host):
    sock = fake_binary_socket([socket.timeout()])
    with pytest.raises(IOError, match="timeout waiting for ACK"):
        protocol.upload_one(host, 0x0100, "presets/slot1", b"p")
    assert sock.closed


# ---- _connect_with_retry ---------------------------------------------------

def test_connect_with_retry_retries_once_then_succeeds(monkeypatch, host, fake_socket_cls):
    attempts = []
    good = fake_socket_cls([])

    def create_connection(addr, timeout=None):
        attempts.append(addr)
        if len(attempts) == 1:
            raise ConnectionRefusedError()
        return good

    monkeypatch.setattr(protocol, "socket", types.SimpleNamespace(
        create_connection=create_connection, timeout=socket.timeout))
    monkeypatch.setattr(protocol, "_CONNECT_RETRY_DELAY", 0)
    assert protocol._connect_with_retry(host, protocol.SETTINGS_PORT) is good
    assert attempts == [(host, protocol.SETTINGS_PORT)] * 2


def test_connect_with_retry_gives_up_after_second_failure(monkeypatch, host):
    def create_connection(addr, timeout=None):
        raise OSError("still down")

    monkeypatch.setattr(protocol, "socket", types.SimpleNamespace(
        create_connection=create_connection, timeout=socket.timeout))
    monkeypatch.setattr(protocol, "_CONNECT_RETRY_DELAY", 0)
    with pytest.raises(OSError, match="still down"):
        protocol._connect_with_retry(host, protocol.CONTROL_PORT)
