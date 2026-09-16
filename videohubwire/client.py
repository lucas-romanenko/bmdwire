# SPDX-License-Identifier: MIT
"""videohubwire.client — Videohub Ethernet Protocol client.

Text-based, block-oriented protocol over TCP 9990, documented in
Blackmagic's "Videohub Ethernet Protocol" PDF (ships with the Videohub
SDK). A block is a header line ending in ``:``, zero or more body
lines, and a blank-line terminator::

    VIDEO OUTPUT ROUTING:
    0 12
    3 7
    <blank>

On connect the hub dumps its full state as a run of blocks (protocol
preamble, device info, input/output labels, output locks, routing —
newer firmware ends the dump with ``END PRELUDE:``). After that it
PUSHES the same block shapes whenever state changes from any client
(another panel routes, a label edit), and answers commands with a bare
``ACK`` or ``NAK`` block.

This client is synchronous and single-socket (no worker thread): connect parses the preamble into a state snapshot;
:meth:`state` drains any pushed updates and returns the current view;
:meth:`route` sends one routing change and waits for ACK/NAK, applying
any interleaved push blocks on the way. Intended usage is short-lived,
per-request connections: the preamble for even a 120x120 hub is a few
KB, so connect-read-act-close takes milliseconds on a LAN.

Lock states in ``VIDEO OUTPUT LOCKS``: ``U`` unlocked, ``O`` locked by
this connection ("owned"), ``L`` locked by another client. This client
never takes locks; it reports them so the UI can render locked
destinations read-only. Routing a locked destination gets a NAK.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


DEFAULT_PORT = 9990
DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 3.0

# The preamble has no length header and pre-END-PRELUDE firmware never
# marks its end. Treat it as complete once the essential blocks arrived
# and the wire has gone quiet for this long.
PREAMBLE_QUIET_S = 0.3

# One retry on the initial TCP connect: the first packet to a device the
# host hasn't talked to recently can be eaten by ARP resolution (the
# same cold-target behaviour other Blackmagic devices show).
CONNECT_RETRY_DELAY_S = 0.3


class VideohubError(Exception):
    """Protocol-level failure: NAK from the hub, closed connection, or
    a timeout waiting for a response."""


class Videohub:
    """Client for one Videohub. Context manager::

        with Videohub(ip) as vh:
            snap = vh.state()
            vh.route(dest, src)

    ``socket_factory`` is a hook for tests: a callable with the
    signature of :func:`socket.create_connection` returning an object
    with ``sendall``, ``recv``, ``settimeout``, ``close``, ``shutdown``.
    """

    def __init__(self,
                 host: str,
                 port: int = DEFAULT_PORT,
                 *,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 read_timeout: float = DEFAULT_READ_TIMEOUT,
                 socket_factory: Optional[Callable] = None):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._socket_factory = socket_factory or socket.create_connection
        self._sock = None
        self._buf = b''

        # State assembled from preamble + pushed blocks. Dicts keyed by
        # int index — the hub addresses everything 0-based.
        self.protocol_version: str = ''
        self.device: Dict[str, str] = {}
        self.input_labels: Dict[int, str] = {}
        self.output_labels: Dict[int, str] = {}
        self.routing: Dict[int, int] = {}     # dest -> src
        self.locks: Dict[int, str] = {}       # dest -> 'U' | 'O' | 'L'

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> 'Videohub':
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def connect(self) -> None:
        """Open the TCP connection and parse the state preamble."""
        if self._sock is not None:
            self.close()
        try:
            self._sock = self._socket_factory((self.host, self.port),
                                              timeout=self.connect_timeout)
        except OSError:
            time.sleep(CONNECT_RETRY_DELAY_S)
            self._sock = self._socket_factory((self.host, self.port),
                                              timeout=self.connect_timeout)
        self._buf = b''
        try:
            self._read_preamble()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def video_inputs(self) -> int:
        return int(self.device.get('video inputs') or 0)

    @property
    def video_outputs(self) -> int:
        return int(self.device.get('video outputs') or 0)

    def state(self) -> dict:
        """Drain pending pushed updates and return a UI-ready snapshot.

        Shape::

            {'device': {'model_name', 'friendly_name', 'video_inputs',
                        'video_outputs', 'protocol_version'},
             'inputs':  [{'index', 'label'}, ...],
             'outputs': [{'index', 'label', 'source', 'lock'}, ...]}
        """
        self._drain()
        n_in = self.video_inputs or (max(self.input_labels, default=-1) + 1)
        n_out = self.video_outputs or (max(self.output_labels, default=-1) + 1)
        return {
            'device': {
                'model_name': self.device.get('model name', ''),
                'friendly_name': self.device.get('friendly name', ''),
                'video_inputs': n_in,
                'video_outputs': n_out,
                'protocol_version': self.protocol_version,
            },
            'inputs': [
                {'index': i,
                 'label': self.input_labels.get(i) or f'Input {i + 1}'}
                for i in range(n_in)
            ],
            'outputs': [
                {'index': i,
                 'label': self.output_labels.get(i) or f'Output {i + 1}',
                 'source': self.routing.get(i),
                 'lock': self.locks.get(i, 'U')}
                for i in range(n_out)
            ],
        }

    def route(self, dest: int, src: int) -> None:
        """Route ``src`` to ``dest``. Raises :class:`VideohubError` on
        NAK (typically a locked destination) or response timeout."""
        dest, src = int(dest), int(src)
        if dest < 0 or src < 0:
            raise VideohubError(f'invalid route {dest} <- {src}')
        n_out, n_in = self.video_outputs, self.video_inputs
        if (n_out and dest >= n_out) or (n_in and src >= n_in):
            raise VideohubError(
                f'route {dest} <- {src} outside this hub '
                f'({n_in}x{n_out})')
        self._send(f'VIDEO OUTPUT ROUTING:\n{dest} {src}\n\n')
        deadline = time.monotonic() + self.read_timeout
        while True:
            block = self._recv_block(deadline - time.monotonic())
            if block is None:
                raise VideohubError(
                    f'timed out waiting for ACK routing {dest} <- {src}')
            header = block[0].strip()
            if header == 'ACK':
                # The hub also broadcasts the changed ROUTING block;
                # apply optimistically so a state() right after is
                # correct even if that push is still in flight.
                self.routing[dest] = src
                return
            if header == 'NAK':
                raise VideohubError(
                    f'hub refused route {dest} <- {src} '
                    f'(destination locked?)')
            # Interleaved push (someone else routing, etc.) — apply and
            # keep waiting for our answer.
            self._apply_block(block)

    def set_input_label(self, index: int, label: str) -> None:
        """Rename an input port. Empty label is allowed (clears it —
        consumers fall back to 'Input N')."""
        self._set_label('INPUT LABELS', self.input_labels,
                        self.video_inputs, index, label)

    def set_output_label(self, index: int, label: str) -> None:
        """Rename an output port."""
        self._set_label('OUTPUT LABELS', self.output_labels,
                        self.video_outputs, index, label)

    def _set_label(self, block: str, target: dict, count: int,
                   index: int, label: str) -> None:
        index = int(index)
        # Newlines would break the block framing; the hub caps labels
        # around 64 chars — enforce both before they hit the wire.
        label = ' '.join(str(label).split())
        if len(label) > 64:
            raise VideohubError('label too long (max 64 characters)')
        if index < 0 or (count and index >= count):
            raise VideohubError(f'{block.lower()} index {index} outside '
                                f'this hub')
        self._send(f'{block}:\n{index} {label}\n\n')
        deadline = time.monotonic() + self.read_timeout
        while True:
            resp = self._recv_block(deadline - time.monotonic())
            if resp is None:
                raise VideohubError(
                    f'timed out waiting for ACK renaming {block.lower()} '
                    f'{index}')
            header = resp[0].strip()
            if header == 'ACK':
                target[index] = label
                return
            if header == 'NAK':
                raise VideohubError(
                    f'hub refused label change on {block.lower()} {index}')
            self._apply_block(resp)

    def ping(self) -> None:
        """Cheap liveness check (``PING:`` block, ACK expected)."""
        self._send('PING:\n\n')
        deadline = time.monotonic() + self.read_timeout
        while True:
            block = self._recv_block(deadline - time.monotonic())
            if block is None:
                raise VideohubError('timed out waiting for PING ack')
            if block[0].strip() == 'ACK':
                return
            self._apply_block(block)

    # ------------------------------------------------------------------
    # Wire I/O
    # ------------------------------------------------------------------

    def _send(self, text: str) -> None:
        if self._sock is None:
            raise VideohubError('not connected')
        self._sock.sendall(text.encode('utf-8'))

    def _recv_block(self, timeout: float) -> Optional[List[str]]:
        """Read one blank-line-terminated block. Returns the block's
        lines (header first), or None if ``timeout`` elapses first."""
        deadline = time.monotonic() + max(0.0, timeout)
        while b'\n\n' not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise VideohubError('connection closed by hub')
            self._buf += chunk
        raw, _, self._buf = self._buf.partition(b'\n\n')
        return raw.decode('utf-8', 'replace').split('\n')

    def _read_preamble(self) -> None:
        """Consume the initial state dump.

        Newer firmware terminates it with ``END PRELUDE:``; older
        firmware just stops talking. Accept either: done on the marker,
        or once the essential blocks have arrived and the wire has been
        quiet for PREAMBLE_QUIET_S.
        """
        deadline = time.monotonic() + self.read_timeout
        while True:
            has_essentials = bool(self.device) and bool(self.routing)
            block = self._recv_block(
                PREAMBLE_QUIET_S if has_essentials
                else deadline - time.monotonic())
            if block is None:
                if has_essentials:
                    return
                raise VideohubError(
                    f'no state preamble from {self.host} — is this a '
                    f'Videohub?')
            if self._apply_block(block) == 'END PRELUDE:':
                return

    def _drain(self) -> None:
        """Apply any pushed blocks sitting in the socket, non-blocking."""
        while True:
            block = self._recv_block(0.05)
            if block is None:
                return
            self._apply_block(block)

    # ------------------------------------------------------------------
    # Block parsing
    # ------------------------------------------------------------------

    def _apply_block(self, lines: List[str]) -> str:
        """Apply one block to local state; returns the header line."""
        header = lines[0].strip()
        body = [ln for ln in lines[1:] if ln.strip()]
        if header == 'PROTOCOL PREAMBLE:':
            for key, val in self._kv(body):
                if key == 'version':
                    self.protocol_version = val
        elif header == 'VIDEOHUB DEVICE:':
            for key, val in self._kv(body):
                self.device[key] = val
        elif header == 'INPUT LABELS:':
            self._apply_indexed(body, self.input_labels, str)
        elif header == 'OUTPUT LABELS:':
            self._apply_indexed(body, self.output_labels, str)
        elif header == 'VIDEO OUTPUT ROUTING:':
            self._apply_indexed(body, self.routing, int)
        elif header == 'VIDEO OUTPUT LOCKS:':
            self._apply_indexed(body, self.locks, str)
        # Everything else (CONFIGURATION:, SERIAL PORT..., MONITORING
        # OUTPUT..., END PRELUDE:, ACK echoes) is deliberately ignored.
        return header

    @staticmethod
    def _kv(body: List[str]):
        for line in body:
            key, sep, val = line.partition(':')
            if sep:
                yield key.strip().lower(), val.strip()

    @staticmethod
    def _apply_indexed(body: List[str], target: dict, cast) -> None:
        """Parse ``<index> <value>`` lines. Values may contain spaces
        (labels like ``0 CAM 1``) — only the first token is the index.
        A malformed line is skipped and logged, not fatal."""
        for line in body:
            tok, _, rest = line.strip().partition(' ')
            try:
                target[int(tok)] = cast(rest.strip())
            except (ValueError, TypeError):
                logger.warning('videohubwire: skipping malformed line %r', line)
