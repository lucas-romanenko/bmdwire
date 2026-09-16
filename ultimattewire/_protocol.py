# SPDX-License-Identifier: MIT
"""
Low-level Ultimatte protocol primitives shared by every ultimattewire
feature: ports, timeouts, and the 9996 binary-channel frame I/O.

Binary-channel frame formats (TCP 9996):
    Read  : send [opcode:2BE][len:4BE][payload]
            recv [len:4BE][body]
    Write : send [opcode:2BE][name_len:4BE][name:N][data_len:4BE][data:M][0x00]
            recv 6 bytes of zeros = success ACK

The trailing 0x00 on writes and the exact framing are required — the
unit will not ACK frames that don't match. Do not change these without
re-validating against hardware.
"""

import socket
import struct
import time


CONTROL_PORT = 9998          # text channel (preamble, FILE LIST, etc.)
SETTINGS_PORT = 9996         # binary channel (read/write slot blobs + resources)
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 30.0
ACK_LEN = 6                  # write-ack: 6 zero bytes
_CONNECT_RETRY_DELAY = 0.25  # seconds between connect attempts


def _recv_exact(sock, n):
    """Read exactly n bytes from sock, or short-read on EOF."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _connect_with_retry(host, port, timeout=CONNECT_TIMEOUT):
    """Open a TCP connection to (host, port) with one retry on transient
    failure. Retries cover only the connect phase — once a socket is
    returned, data-side errors are real failures and not retried.

    Defeats cold-ARP and first-packet hiccups that present as connection
    refused / OSError / timeout on the first attempt and resolve on the
    second a few hundred ms later.
    """
    last_error = None
    for attempt in range(2):
        try:
            return socket.create_connection((host, port), timeout=timeout)
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            last_error = e
            if attempt == 0:
                time.sleep(_CONNECT_RETRY_DELAY)
    raise last_error


def binary_request(host, opcode, payload, timeout=CONNECT_TIMEOUT):
    """Issue a binary read on 9996 and return the body bytes (may be empty).

    Frame: send [opcode:2BE][len:4BE][payload]
           recv [len:4BE][body]

    A short read of the length header (including a clean immediate EOF;
    we just sent a request, so a reply is owed) or of the body raises
    IOError instead of returning empty/truncated bytes (2026-07-06).
    Before that, a unit that accepted the connection but closed before or
    mid reply yielded silent empty/truncated resources that the archive
    path would zip and later RESTORE to live hardware. A genuinely empty resource (header
    says length 0) still returns b"" cleanly.
    """
    if isinstance(payload, str):
        payload = payload.encode()
    # Cold-ARP retry like upload_one/read_preamble — the read path was the
    # one caller still connecting bare, so archive reads sporadically
    # failed on cold units while writes succeeded.
    s = _connect_with_retry(host, SETTINGS_PORT, timeout=timeout)
    s.settimeout(timeout)
    try:
        frame = struct.pack(">HI", opcode, len(payload)) + payload
        s.sendall(frame)
        length_bytes = _recv_exact(s, 4)
        if len(length_bytes) < 4:
            raise IOError(
                f"short read on length header (got {len(length_bytes)} of 4 "
                f"bytes; unit closed before replying)"
            )
        (length,) = struct.unpack(">I", length_bytes)
        if not length:
            return b""
        body = _recv_exact(s, length)
        if len(body) != length:
            raise IOError(
                f"short read on body (got {len(body)} of {length} bytes)"
            )
        return body
    finally:
        s.close()


def upload_one(host, opcode, name, payload, connect_timeout=CONNECT_TIMEOUT,
               read_timeout=READ_TIMEOUT):
    """Issue a binary write on 9996. Raises IOError if the unit rejects.

    Frame: send [opcode:2BE][name_len:4BE][name:N][data_len:4BE][data:M][0x00]
           recv 6 bytes of zeros = success ACK

    The trailing 0x00 is required — the unit will not ACK without it.
    """
    name_bytes = name.encode("utf-8")
    frame = (
        struct.pack(">H", opcode)
        + struct.pack(">I", len(name_bytes))
        + name_bytes
        + struct.pack(">I", len(payload))
        + payload
        + b"\x00"
    )
    s = _connect_with_retry(host, SETTINGS_PORT, timeout=connect_timeout)
    try:
        s.settimeout(read_timeout)
        s.sendall(frame)
        # The unit sends the ACK then closes the connection quickly, so recv()
        # can surface EOF before all ACK_LEN bytes arrive depending on TCP
        # timing. Tolerate short reads as long as we get something that looks
        # like a success ACK (all zeros).
        ack = b""
        while len(ack) < ACK_LEN:
            try:
                chunk = s.recv(ACK_LEN - len(ack))
            except socket.timeout:
                raise IOError(
                    f"timeout waiting for ACK (got {len(ack)} of {ACK_LEN} bytes)"
                )
            if not chunk:
                break
            ack += chunk
        if len(ack) == 0:
            raise IOError("connection closed with no ACK bytes (frame rejected)")
        if any(b != 0 for b in ack):
            raise IOError(f"non-zero ACK from unit (likely error): {ack.hex()}")
    finally:
        s.close()
