# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
import collections
import errno
import logging
import os
import select
import socket
import struct
import threading
import time
from queue import Queue

from atemwire._socketqueue import SocketQueue
from atemwire._transfer import TransferQueueFlushed


# Send-path errnos that indicate a MOMENTARY network condition rather than a
# dead socket. The UDP thread drops the packet and keeps running on these;
# anything else (EBADF from a deliberate close, etc.) still terminates the
# thread. A thread killed by one transient error left a zombie connection —
# worker parked forever, is_connected True, send() silently dropping
# (2026-07-06).
TRANSIENT_SEND_ERRNOS = frozenset({
    errno.ENETUNREACH,   # VPN/route flap
    errno.EHOSTUNREACH,  # ICMP host-unreachable burst (ATEM rebooting)
    errno.ECONNREFUSED,  # ICMP port-unreachable on a connected UDP socket
    errno.ENOBUFS,       # kernel send buffer full under aggressive drain
    errno.EAGAIN,        # transient would-block
    errno.EPERM,         # conntrack table full drops the send with EPERM
    errno.EINTR,         # interrupted syscall
})


# ---------------------------------------------------------------------------
# Packet-trace plumbing — single source of truth for ``ATEMWIRE_PACKET_TRACE``.
#
# Set ``ATEMWIRE_PACKET_TRACE=1`` to enable per-packet wire-level logs from
# this module and ``connection.py``. Off by default — production logs
# would fill at hundreds of lines per second per connection.
#
#   ``atemwire.trace.packet`` — TX/RX wire packets (here)
#   ``atemwire.trace.drain``  — outbound command-queue drains (connection.py)
#
# Both ``ENABLED`` and the loggers are imported by ``connection.py`` via
# ``from atemwire.transport import ENABLED as TRACE_ENABLED, drain_log``.
# ---------------------------------------------------------------------------

TRACE_ENABLED: bool = (
    os.environ.get('ATEMWIRE_PACKET_TRACE', '').strip().lower()
    in ('1', 'true', 'yes', 'on')
)
packet_log = logging.getLogger('atemwire.trace.packet')
drain_log = logging.getLogger('atemwire.trace.drain')


def _trace_command_tags(data: bytes) -> list:
    """Pull out the command tags from a reliable data-bearing packet.

    A reliable packet's data is a series of (length:u16, 2 bytes pad,
    tag:4 bytes, payload...) entries. Returns just the tags so a trace
    line stays short and readable.
    """
    tags = []
    offset = 0
    while offset + 8 <= len(data):
        try:
            length = struct.unpack_from('>H', data, offset)[0]
            tag = data[offset + 4:offset + 8]
            if length < 8 or offset + length > len(data):
                break
            tags.append(tag.decode('latin-1', 'replace'))
            offset += length
        except Exception:
            break
    return tags


class ConnectionReady:
    def __init__(self):
        pass


class Wakeup:
    """Sentinel returned from ``receive_packet`` when an external caller has
    poked the worker awake (via ``thread_recv_queue.put(Wakeup())``) so it
    can drain a pending outbound command queue.

    Without this, the worker thread blocks indefinitely inside
    ``receive_packet`` while ATEM is only sending control PINGs (length 12,
    no data) — those don't break out of the inner ``while True`` loop, so
    ``protocol.loop()`` never returns and ``_drain_cmd_queue()`` never runs.
    The result is that any command queued via ``ATEMConnection.send()``
    sits in the queue until ATEM happens to emit a data packet, which can
    be tens of seconds on an idle ATEM. ``Wakeup`` gives ``send()`` a way
    to break that block deterministically."""

    def __init__(self):
        pass


class Packet:
    STRUCT_HEADER = struct.Struct('>HHH 2x HH')
    STRUCT_USB = struct.Struct('<I')

    def __init__(self):
        self.flags = 0
        self.length = 0
        self.session = 0
        self.sequence_number = 0
        self.acknowledgement_number = 0
        self.remote_sequence_number = 0
        # Header bytes 6-7 — the sequence a FLAG_REQUEST_RETRANSMISSION
        # packet asks us to resend from. STRUCT_HEADER SKIPS these bytes
        # ('2x'), so ``remote_sequence_number`` (bytes 8-9, the 0x61
        # magic on our ACKs) is NOT this field — reading it there
        # replayed whole sessions from seq ~0 and wedged the switcher
        # (live, 2026-06-12). Parsed explicitly in from_bytes; never
        # set on outgoing packets (we don't request retransmission).
        self.retransmission_request = 0
        self.data = None
        self.debug = False
        self.label = None
        self.last_packet_time = None

    @classmethod
    def from_bytes(cls, packet):
        res = cls()
        fields = cls.STRUCT_HEADER.unpack_from(packet)
        res.length = fields[0] & ~(0x1f << 11)
        res.flags = (fields[0] & (0x1f << 11)) >> 11

        if res.length != len(packet):
            raise ValueError(
                "Incomplete or corrupt packet received, {} in header but data length is {}".format(
                    res.length, len(packet)))

        res.session = fields[1]
        res.acknowledgement_number = fields[2]
        res.remote_sequence_number = fields[3]
        res.sequence_number = fields[4]
        res.retransmission_request = struct.unpack_from('>H', packet, 6)[0]
        res.data = packet[12:]
        return res

    def to_bytes(self):
        header_len = 12
        data_len = len(self.data) if self.data is not None else 0
        packet_len = header_len + data_len
        result = self.STRUCT_HEADER.pack(
            packet_len + (self.flags << 11),
            self.session,
            self.acknowledgement_number,
            self.remote_sequence_number,
            self.sequence_number)

        if self.data:
            result += bytes(self.data)

        return result

    def to_usb(self):
        data_len = len(self.data) if self.data is not None else 0
        result = self.STRUCT_USB.pack(data_len)
        if self.data:
            result += bytes(self.data)
        return result

    def __repr__(self):
        flags = ''
        extra = ''
        if self.flags & UdpProtocol.FLAG_RELIABLE:
            flags += ' RELIABLE'
        if self.flags & UdpProtocol.FLAG_SYN:
            flags += ' SYN'
        if self.flags & UdpProtocol.FLAG_RETRANSMISSION:
            flags += ' RETRANSMISSION'
        if self.flags & UdpProtocol.FLAG_REQUEST_RETRANSMISSION:
            flags += ' REQ-RETRANSMISSION'
            extra = ' req={}'.format(self.retransmission_request)
        if self.flags & UdpProtocol.FLAG_ACK:
            flags += ' ACK'
            extra = ' ack={}'.format(self.acknowledgement_number)
        flags = flags.strip()
        data_len = len(self.data) if self.data is not None else 0
        label = ''
        if self.label:
            label = ' ' + self.label
        return '<Packet flags={} data={} sequence={}{}{}>'.format(flags, data_len, self.sequence_number, extra, label)

class BaseProtocol:
    def __init__(self):
        self.send_queue = collections.deque(maxlen=1024)
        self.queue_enabled = False
        self.queue_callback = None
        self.mark_next_connected = False
        self.batch_size = 1
        self.batch_delay = 0

    def _send_packet(self, packet):
        raise NotImplementedError()

    def queue_packet(self, packet):
        # send_queue is a bounded deque: appending at maxlen silently
        # discards the OLDEST queued packet — for reliable transfer chunks
        # that's data corruption. FTCD budgets keep it far below the cap in
        # practice, so overflow is a bug worth a loud line, not silence.
        if self.send_queue.maxlen is not None and \
                len(self.send_queue) == self.send_queue.maxlen:
            # getattr: BaseProtocol has no self.log (UdpProtocol sets it).
            log = getattr(self, 'log', None) or logging.getLogger(__name__)
            log.error(
                'send_queue overflow (maxlen=%d) — dropping oldest queued '
                'packet; the in-flight transfer is likely corrupt',
                self.send_queue.maxlen)
        self.send_queue.append(packet)

    def queue_trigger(self):
        if len(self.send_queue) > 0:
            self.queue_enabled = True
            for i in range(0, min(len(self.send_queue), self.batch_size)):
                p = self.send_queue.popleft()
                self._send_packet(p)
                if self.queue_callback is not None:
                    self.queue_callback(len(self.send_queue), len(p.data) - 4)
            time.sleep(self.batch_delay)
        elif self.queue_enabled:
            self.queue_enabled = False
            return True
        return False


class UdpProtocol(BaseProtocol):
    STATE_CLOSED = 0
    STATE_SYN_SENT = 1
    STATE_SYN_RECEIVED = 2
    STATE_ESTABLISHED = 3

    FLAG_RELIABLE = 1
    FLAG_SYN = 2
    FLAG_RETRANSMISSION = 4
    FLAG_REQUEST_RETRANSMISSION = 8
    FLAG_ACK = 16

    # Retransmission window: how far back (in 16-bit
    # sequence numbers) the retransmission_buffer reaches. Requests
    # further back than this are unserviceable — the ATEM's own reliable
    # window is far smaller, so a request outside it means the session
    # is already beyond recovery. Also bounds buffer memory and prevents
    # a wrap-stale entry from being served for a fresh sequence.
    RETRANSMIT_WINDOW = 4096

    # In-order delivery park window: how far ahead of the delivery cursor
    # a packet may be parked while its gap fills. The ATEM's in-flight
    # window is a handful of packets; a jump beyond this means the session
    # is beyond recovery and we resync rather than deadlock.
    DELIVERY_PARK_WINDOW = 512

    # Per-request resend volley cap. A real request is for the ATEM's
    # small in-flight window; if the gap is somehow larger, resending
    # the first MAX_BURST is enough — the ATEM processes them, advances,
    # and re-requests from wherever it is still missing, which paces the
    # recovery naturally. Hard backstop so no single request — however
    # malformed — can ever flood the switcher (an unpaced full-session
    # replay is exactly what wedged a production switcher live on 2026-06-12).
    RETRANSMIT_MAX_BURST = 128

    def __init__(self, ip, port=9910, timeout=5, *, aggressive_drain=False):
        super().__init__()
        self.ip = ip
        self.port = port
        self.timeout = timeout

        # Aggressive drain: on each queue_trigger, empty the whole send_queue
        # in batches (instead of batch_size packets per incoming ATEM packet).
        # Bypasses the ATEM's ACK pacing — lifts upload throughput from
        # ~300 KB/s to ~3 MB/s on 1080p stills. Only makes sense when the
        # caller is pushing a lot of outbound (bulk upload); for normal
        # control traffic the default paced behavior is correct.
        self.aggressive_drain = aggressive_drain

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(self.timeout)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024 * 16)

        self.thread = threading.Thread(None, self._udp_thread, "atem-udp", daemon=True)

        self.local_sequence_number = 0
        self.remote_sequence_number = 0

        # Contiguous-ACK high-water mark. The ATEM sends
        # its initial state as a burst of reliable, sequenced packets and
        # retransmits anything we don't ACK. We must ACK only the highest
        # *gap-free* sequence so a dropped packet in the middle of the burst is
        # resent rather than silently lost (which left sources blank). Distinct
        # from ``remote_sequence_number`` (latest received), which other code
        # relies on. ``_rx_established`` gates this on the first reliable packet
        # so the handshake/control phase is untouched.
        self.ack_number = 0
        self._rx_established = False

        # In-order delivery (2026-07-02, ASC-style shared-session step 1):
        # reliable packets are handed to consumers strictly in sequence
        # order. A retransmitted packet used to be DELIVERED late (arrival
        # order) even though the ACK logic above was gap-correct — for the
        # native transfer path that appended file chunks out of order and
        # corrupted the reassembled frame (observed live: 2 of 4 benchmark
        # downloads returned odd lengths from single-packet loss).
        # ``_deliver_next`` is the next sequence owed to the consumer
        # (established lazily on the first gated packet); ahead-of-gap
        # packets park in ``_parked`` and drain, in order, straight onto
        # thread_recv_queue when the gap fills.
        self._deliver_next = None
        self._parked = {}
        # A packet pulled from thread_recv_queue that ``receive_packet`` had
        # to put aside because it owed the caller a sentinel first
        # (ConnectionReady / TransferQueueFlushed). It is handed out on the
        # NEXT call. Before this existed the packet was simply dropped: the
        # switcher's first packet after the state dump never reached the
        # protocol layer, and its retransmissions were then deduplicated as
        # "already received". When that packet was the LKOB answering a
        # lock request sent during the dump, the session held the media
        # lock without knowing it — for the rest of its life (2026-09-16).
        self._stashed_packet = None

        self.state = UdpProtocol.STATE_CLOSED
        self.session_id = 0x1337

        self.enable_ack = False
        self.had_traffic = False

        self.received_packets = collections.deque(maxlen=1024)
        self.retransmission_buffer = {}

        self.thread_queue = SocketQueue()
        self.thread_recv_queue = Queue()

        if aggressive_drain:
            # Tuned for bulk-upload throughput.
            self.batch_size = 10
            self.batch_delay = 0.001
        else:
            self.batch_size = 5
            self.batch_delay = 0.003

        self.log = logging.getLogger('UdpTransport')
        self.packet_success = 0
        self.packet_errors = 0
        # Rate-limited retransmission summary (one line per ~5s max,
        # replacing the historical one-ERROR-per-recovered-packet walls).
        self._retrans_burst = 0
        self._retrans_last_log = 0.0

        # Lightweight diagnostics, useful even when ATEMWIRE_PACKET_TRACE is off:
        # ``last_rx_monotonic`` is the time of the most recent successfully-
        # decoded packet from the ATEM (any packet, including ACK-only). A
        # caller can use it to detect "we sent a command and nothing came
        # back" — if last_rx_monotonic doesn't advance during the wait, the
        # ATEM isn't responding (vs. the command never going out).
        self.last_rx_monotonic: float = 0.0
        self.rx_packet_count: int = 0
        self.tx_packet_count: int = 0

    def queue_trigger(self):
        """Paced send (default) or aggressive drain (if aggressive_drain=True).

        Return semantics identical to BaseProtocol.queue_trigger — returns
        True exactly once when the queue transitions from "had items" to
        "empty", False otherwise. The call sites in ``loop()`` depend on this
        to emit a ``TransferQueueFlushed`` sentinel.
        """
        if not self.aggressive_drain:
            return super().queue_trigger()

        sq = self.send_queue
        if len(sq) > 0:
            self.queue_enabled = True
            while len(sq) > 0:
                for _ in range(min(len(sq), self.batch_size)):
                    p = sq.popleft()
                    self._send_packet(p)
                    if self.queue_callback is not None:
                        self.queue_callback(len(sq), len(p.data) - 4)
                if len(sq) > 0:
                    time.sleep(self.batch_delay)
            return False
        elif self.queue_enabled:
            self.queue_enabled = False
            return True
        return False

    def _udp_thread(self):
        # A dying transport thread must never strand its consumers:
        # protocol.loop() blocks on thread_recv_queue with NO timeout, so
        # death-without-sentinel turns "switcher went quiet mid-transfer"
        # into callers hung forever (observed 2026-07-02: upload jobs that
        # never finished until their process was restarted). The
        # sentinel makes loop() surface a disconnect immediately. EBADF
        # after a deliberate close lands here too; what used to be a
        # stderr traceback is now one log line.
        try:
            self._udp_thread_loop()
        except Exception as e:
            self.log.error(f"UDP thread exiting: {e!r}")
        finally:
            try:
                self.thread_recv_queue.put(None)
            except Exception:
                pass

    def _udp_thread_loop(self):
        while True:
            readable, _, _ = select.select([self.sock, self.thread_queue], [], [], self.timeout)
            if not readable:
                # Timeout
                self.log.error("Timeout reading from ATEM")
                self.state = UdpProtocol.STATE_CLOSED
                self.connect()
                self.thread_recv_queue.put(None)
                continue
            for queue in readable:
                if queue is self.sock:
                    packet = self._receive_packet_low()
                    if packet is not None:
                        self.thread_recv_queue.put(packet)
                elif queue is self.thread_queue:
                    item = queue.get()
                    if item is None:
                        # Defensive: tolerate a stray wake sentinel on the
                        # send queue (no current producer).
                        continue
                    try:
                        self._send_packet_low(item)
                    except OSError as e:
                        if e.errno in TRANSIENT_SEND_ERRNOS:
                            # A momentary network hiccup (VPN blip, ICMP
                            # unreachable burst, full socket buffer) must
                            # not kill the transport thread: a dead thread
                            # leaves a zombie connection whose worker
                            # blocks forever while is_connected stays True
                            # and send() silently drops (2026-07-06).
                            # Drop the packet — reliable traffic is
                            # recovered by the retransmit machinery (the
                            # ATEM requests the sequence gap; we serve it
                            # from retransmission_buffer).
                            self.log.error(
                                f"Transient send error (packet dropped, "
                                f"thread stays up): {e}")
                            continue
                        self.log.error(e)
                        # Queue a None to signal the socket died
                        self.thread_recv_queue.put(None)
                        return
                else:
                    self.thread_recv_queue.put(None)
                    raise RuntimeError("Unexpected result from select()")

    def _send_packet(self, packet):
        self.thread_queue.put(packet)
        self.packet_success += 1

    def close_session(self):
        """Best-effort protocol-level goodbye (hello opcode 0x04).

        Without this, closing the socket ABANDONS the session: the ATEM
        keeps it alive — counted against its small session table, any
        still-store lock included — until it reaps it on its own
        (measured 2026-09-16 on a 1 M/E Constellation HD: the lock of an
        abandoned session came back ~5 s after the socket closed; the
        "~5 minutes" this used to say was never measured). Enough
        abandoned sessions in flight and the switcher refuses all new
        connections and/or never grants the media lock, which reads as a
        "wedged network engine" that only a power cycle clears (observed
        live on two switchers, 2026-07-02). Every close path must call
        this BEFORE ``sock.close()``. The goodbye itself releases every
        lock the session holds within ~10 ms (measured the same day).

        Fire-and-forget: one datagram, no wait for the ATEM's reply —
        we're tearing down regardless, and a lost goodbye is no worse
        than the old behaviour.
        """
        if self.state != UdpProtocol.STATE_ESTABLISHED:
            return
        try:
            bye = Packet()
            bye.flags = UdpProtocol.FLAG_SYN
            bye.label = 'closing'
            bye.data = [0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
            self._send_packet_low(bye)
        except Exception:
            pass
        self.state = UdpProtocol.STATE_CLOSED

    def _send_packet_low(self, packet):
        packet.session = self.session_id
        if not packet.flags & UdpProtocol.FLAG_ACK:
            if self.local_sequence_number == -1:
                self.local_sequence_number = 0
            packet.sequence_number = (self.local_sequence_number + 1) % 0x8000
        try:
            raw = packet.to_bytes()
        except (struct.error, TypeError) as e:
            # A packet with post-reset None fields must not kill the UDP
            # thread (a struct.error here left upload jobs hung in
            # 'processing' forever, 2026-07-02). Drop it: either the
            # session is dying anyway or the retransmit machinery
            # recovers the gap.
            self.log.error(f"dropping unserializable packet ({e}): {packet!r}")
            return
        sent = self.sock.sendto(raw, (self.ip, self.port))
        self.tx_packet_count += 1
        if TRACE_ENABLED:
            data_len = len(packet.data) if packet.data else 0
            tags = _trace_command_tags(packet.data) if packet.data and packet.flags & UdpProtocol.FLAG_RELIABLE else []
            short_sent = '' if sent == len(raw) else f' SHORTSENT={sent}/{len(raw)}'
            packet_log.info(
                f"TX {self.ip}:{self.port} session={self.session_id!r} "
                f"seq={packet.sequence_number} ack={packet.acknowledgement_number} "
                f"flags=0x{packet.flags:02x} datalen={data_len} "
                f"tags={tags} label={packet.label!r}{short_sent}"
            )
        if sent < len(raw):
            self.log.error(f"sendto returned partial {sent}/{len(raw)} bytes")
        self.log.debug('> {}'.format(packet))
        if packet.flags & (UdpProtocol.FLAG_SYN | UdpProtocol.FLAG_ACK) == 0:
            # 15-BIT sequence space (wraps at 0x8000, NOT 0x10000): observed
            # live 2026-07-07 — the ATEM's own sequences wrap 32767 -> 0, and
            # 16-bit math here desynced the delivery cursor + contiguous ACK
            # at every wrap (once per ~6 still transfers), triggering a
            # 20k-packet retransmission storm that stalled transfers ~40s.
            self.local_sequence_number = (self.local_sequence_number + 1) % 0x8000
            # Buffer ONLY sequence-consuming packets for retransmission.
            # Buffering unconditionally — as this did
            # historically — let every outgoing ACK overwrite the buffer
            # entry of the newest reliable packet (ACKs don't advance
            # local_sequence_number), so a retransmit request for that
            # sequence would have resent an ACK instead of the lost
            # command. Sliding window: entries older than
            # RETRANSMIT_WINDOW sequences are dropped, which both bounds
            # memory on long sessions and guarantees a wrap-stale entry
            # is never served for a fresh sequence number.
            self.retransmission_buffer[self.local_sequence_number] = packet
            self.retransmission_buffer.pop(
                (self.local_sequence_number - UdpProtocol.RETRANSMIT_WINDOW)
                % 0x8000, None)

        if packet.label == "_handshake":
            # Clear temporary session id, use the session id received in the first packet from the remote
            self.session_id = None
            if TRACE_ENABLED:
                packet_log.info(f"session_id ← None (post-handshake; will be set from next RX)")

    def _receive_packet(self):
        return self.thread_recv_queue.get()

    def _receive_packet_low(self):
        try:
            data, address = self.sock.recvfrom(2048)
        except socket.timeout:
            # No longer receiving data from the hardware, reset the state of the connection and re-init
            self.log.error("Socket timeout")
            self.state = UdpProtocol.STATE_CLOSED
            self.connect()
            return
        # The socket is unconnected, so ANY host on the VLAN can hit our
        # ephemeral port. Before this guard, one foreign/garbage datagram
        # raised out of Packet.from_bytes and killed the transport thread —
        # dropping the operator's live session (pool eviction recovered it,
        # but the session still died). Filter by peer and drop malformed
        # datagrams instead.
        if address[0] != self.ip:
            self.log.debug(f"Ignoring datagram from foreign peer {address[0]}")
            return
        try:
            packet = Packet.from_bytes(data)
        except (ValueError, struct.error) as e:
            self.log.warning(f"Ignoring malformed datagram from {address[0]}: {e}")
            return

        if packet.flags & UdpProtocol.FLAG_RETRANSMISSION:
            # Retransmitted packets are the RECOVERY working (in-order
            # delivery + contiguous ACK make the ATEM resend what we
            # missed) — routine during bulk transfers under load. The old
            # one-ERROR-per-packet logging produced hundred-line walls per
            # burst; keep counters, log one rate-limited summary instead.
            if len(data) > 12:
                self.packet_errors += 1
            self._retrans_burst += 1
            now_mono = time.monotonic()
            if now_mono - self._retrans_last_log >= 5.0:
                self.log.warning(
                    f"received {self._retrans_burst} retransmitted packet(s) "
                    f"since last summary (recovered normally)")
                self._retrans_burst = 0
                self._retrans_last_log = now_mono
        else:
            self.packet_success += 1

        if packet.flags & UdpProtocol.FLAG_REQUEST_RETRANSMISSION:
            # The ATEM missed one of our reliable packets and is asking
            # for everything from ``remote_sequence_number`` onward. Not
            # honouring this (the pre-fix behaviour)
            # silently dropped the commands AND left the ATEM holding a
            # permanently gapped inbound stream — observed live wedging
            # the switcher's network engine until power cycle.
            self.packet_errors += 1
            self._retransmit_from(packet.retransmission_request)

        new_sequence_number = packet.sequence_number
        self.remote_sequence_number = new_sequence_number

        if self.session_id is None:
            self.session_id = packet.session
            if TRACE_ENABLED:
                packet_log.info(
                    f"session_id ← 0x{packet.session:04x} (set from first RX after handshake)"
                )

        self.last_rx_monotonic = time.monotonic()
        self.rx_packet_count += 1

        if TRACE_ENABLED:
            data_len = len(packet.data) if packet.data else 0
            tags = _trace_command_tags(packet.data) if packet.data and data_len > 0 else []
            packet_log.info(
                f"RX {address[0]}:{address[1]} session=0x{packet.session:04x} "
                f"seq={packet.sequence_number} ack={packet.acknowledgement_number} "
                f"flags=0x{packet.flags:02x} datalen={data_len} tags={tags}"
            )

        is_retransmissions = packet.flags & UdpProtocol.FLAG_RETRANSMISSION
        if is_retransmissions:
            if self.remote_sequence_number in self.received_packets:
                return True

        self.received_packets.append(self.remote_sequence_number)
        self._update_ack_number(packet)

        # ACK if: we're already in ack-mode and the packet is reliable, OR
        # we haven't entered ack-mode yet and this is a no-data control
        # packet (kicks off ack-mode on the initial control frame).
        # ACK every reliable packet once the session is ESTABLISHED. The
        # historical rule waited for the first no-data control frame before
        # ACKing anything, so the whole initial state dump went unACKed
        # until the switcher's post-dump ping — on a LAN that is ~5 ms, but
        # the switcher's retransmit timer is ~80 ms, and any dump slower
        # than that (VPN, a busy switcher, or a data packet instead of a
        # ping after the dump) was retransmitted in full over and over
        # until we finally ACKed (measured 2026-09-16: the entire 20-packet
        # dump resent every 80 ms). ATEM Software Control ACKs each packet.
        if (packet.flags & UdpProtocol.FLAG_RELIABLE
                and (self.enable_ack
                     or self.state == UdpProtocol.STATE_ESTABLISHED)) or \
                (not self.enable_ack and len(packet.data) == 0):
            self.enable_ack = True
            # ACK this
            ack = Packet()
            ack.flags = UdpProtocol.FLAG_ACK
            ack.acknowledgement_number = self.ack_number
            ack.remote_sequence_number = 0x61
            self._send_packet(ack)

        # ---- In-order delivery gate (see __init__ notes) ----
        # Applies to reliable packets after rx-establishment; handshake and
        # non-reliable control frames pass through untouched. Runs on the
        # UDP thread, the sole thread_recv_queue producer, so pushing the
        # gap-filling packet plus its drained successors preserves order.
        if not (packet.flags & UdpProtocol.FLAG_RELIABLE) or not self._rx_established:
            return packet

        # All cursor arithmetic in the ATEM's 15-BIT sequence space (mask
        # 0x7FFF, half-space 0x4000). The 16-bit math this shipped with
        # turned every legitimate 32767->0 wrap into an "absurd jump"
        # resync, desyncing the gate mid-transfer (2026-07-07).
        seq = packet.sequence_number
        if self._deliver_next is None:
            # First gated packet establishes the delivery cursor.
            self._deliver_next = (seq + 1) & 0x7FFF
            return packet

        dist = (seq - self._deliver_next) & 0x7FFF
        if dist == 0:
            self._deliver_next = (seq + 1) & 0x7FFF
            if not self._parked:
                return packet
            # The gap just filled — drain every parked successor in order.
            self.thread_recv_queue.put(packet)
            while self._deliver_next in self._parked:
                self.thread_recv_queue.put(self._parked.pop(self._deliver_next))
                self._deliver_next = (self._deliver_next + 1) & 0x7FFF
            return True
        if dist > 0x4000:
            # Behind the cursor: a duplicate of something already delivered.
            return True
        if dist <= UdpProtocol.DELIVERY_PARK_WINDOW:
            # Ahead of a gap: hold it; the stalled ACK above makes the ATEM
            # retransmit the missing packet, which will drain this one.
            self._parked[seq] = packet
            return True
        # Absurd forward jump — beyond any real in-flight window. Resync
        # rather than deadlock; drop the (equally stale) parked packets.
        self.log.error(
            f"in-order delivery resync: cursor {self._deliver_next} -> {seq} "
            f"({len(self._parked)} parked dropped)")
        self._parked.clear()
        self._deliver_next = (seq + 1) & 0x7FFF
        return packet

    def _update_ack_number(self, packet):
        """Advance the contiguous-ACK high-water mark.

        We ACK only the highest *gap-free* sequence: if a reliable packet in
        the middle of the ATEM's burst was dropped, the ACK stalls before the
        gap so the ATEM retransmits the missing packet (its retransmits are
        handled above) instead of us silently swallowing it. The first reliable
        packet establishes the baseline; before that (handshake / no-data
        control frames) we mirror the latest sequence so that phase is
        unchanged. No worse than the old behaviour in the no-loss case, where
        the contiguous mark always equals the latest sequence.
        """
        if not self._rx_established:
            self.ack_number = self.remote_sequence_number
            if packet.flags & UdpProtocol.FLAG_RELIABLE:
                self._rx_established = True
            return
        # Drain every contiguous sequence we now hold (15-bit wrap-aware —
        # the ATEM wraps at 0x8000; 16-bit math stalled the ACK at 32767
        # forever after a wrap, making the ATEM retransmit its entire
        # buffer in a loop: the observed 20k-packet storm, 2026-07-07).
        while ((self.ack_number + 1) & 0x7FFF) in self.received_packets:
            self.ack_number = (self.ack_number + 1) & 0x7FFF

    def _retransmit_from(self, request_seq):
        """Resend every buffered packet from ``request_seq`` through the
        latest sent sequence, in order, marked FLAG_RETRANSMISSION
        (the outbound twin of ``_update_ack_number``).

        The ATEM requests retransmission from the first sequence it is
        missing; per the protocol's go-back-N semantics everything from
        that point must be resent in order. Resends go straight to the
        socket — ``_send_packet_low`` would assign FRESH sequence
        numbers, but a retransmission must carry its original one. Safe
        because this runs on ``_udp_thread`` (via
        ``_receive_packet_low``), the same thread that drains
        ``thread_queue`` into ``_send_packet_low``.

        Returns the number of packets resent. Sequences absent from the
        buffer (never sent, or aged past RETRANSMIT_WINDOW) are skipped;
        a request entirely outside the window resends nothing and logs.
        """
        last = self.local_sequence_number
        span = (last - request_seq) & 0x7FFF
        if span >= UdpProtocol.RETRANSMIT_WINDOW:
            self.log.error(
                f"retransmission requested from seq {request_seq} but latest "
                f"sent is {last} — outside the {UdpProtocol.RETRANSMIT_WINDOW}"
                f"-seq window, cannot service")
            return 0
        resent = 0
        for offset in range(min(span + 1, UdpProtocol.RETRANSMIT_MAX_BURST)):
            seq = (request_seq + offset) & 0x7FFF
            pkt = self.retransmission_buffer.get(seq)
            if pkt is None:
                continue
            pkt.flags |= UdpProtocol.FLAG_RETRANSMISSION
            try:
                self.sock.sendto(pkt.to_bytes(), (self.ip, self.port))
            except OSError as exc:
                self.log.error(f"retransmit of seq {seq} failed: {exc}")
                break
            self.tx_packet_count += 1
            resent += 1
            if TRACE_ENABLED:
                packet_log.info(
                    f"TX-RETRANSMIT {self.ip}:{self.port} seq={seq} "
                    f"flags=0x{pkt.flags:02x} "
                    f"datalen={len(pkt.data) if pkt.data else 0}")
        self.log.warning(
            f"retransmission requested from seq {request_seq}: resent "
            f"{resent} packet(s) through seq {last}")
        return resent

    def _handshake(self, packet):
        if not packet.flags & UdpProtocol.FLAG_SYN:
            return

        if not packet.session == self.session_id:
            return

        response_code = packet.data[0]

        self.log.debug('Got response 0x{:02X} to handshake'.format(response_code))

        if response_code == 0x02:
            self.state = UdpProtocol.STATE_ESTABLISHED

        # Got a valid 2nd handshake packet, send back the third one
        response = Packet()
        response.flags = UdpProtocol.FLAG_ACK
        response.label = "_handshake"
        self._send_packet(response)

    def connect(self):
        if self.state != UdpProtocol.STATE_CLOSED:
            raise RuntimeError("Trying to open an connection that's already open")

        if not self.thread.is_alive():
            # A Thread object can only be start()ed ONCE. If the UDP thread
            # already ran and died, start() raises "threads can only be
            # started once" — so this branch could never actually restart a
            # dead transport. Build a fresh Thread in that case (ident is
            # None only for a never-started thread).
            if self.thread.ident is not None:
                self.thread = threading.Thread(
                    None, self._udp_thread, "atem-udp", daemon=True)
            self.thread.start()

        # Reset internal state
        self.local_sequence_number = -1
        self.remote_sequence_number = 0
        self.ack_number = 0
        self._rx_established = False
        # Fresh delivery cursor — parked packets carry the OLD session's
        # sequence space and must never drain into the new one.
        self._deliver_next = None
        self._parked.clear()
        self._stashed_packet = None
        self.session_id = 0x1337
        self.enable_ack = False
        # Buffered packets carry the OLD session id — never serve them
        # for a retransmit request in the new session.
        self.retransmission_buffer.clear()
        # received_packets drives BOTH the retransmission dup-drop and the
        # contiguous-ACK high-water walk. Stale sequence numbers from the OLD
        # session (an in-place reconnect reuses this object) would let the new
        # session falsely ACK a gap it never received — and drop the ATEM's
        # retransmission as a "duplicate" — i.e. silent reliable-data loss until
        # the next full reconnect. Clear it and the traffic flag.
        self.received_packets.clear()
        self.had_traffic = False

        # Create first syn packet
        syn = Packet()
        syn.flags = UdpProtocol.FLAG_SYN
        syn.data = [
            0x01, 0x00,
            0x00, 0x00,
            0x00, 0x00,
            0x00, 0x00,
        ]
        self._send_packet(syn)
        self.state = UdpProtocol.STATE_SYN_SENT

    def receive_packet(self):
        while True:
            if self._stashed_packet is not None:
                # Owed from the previous call, which returned a sentinel
                # instead of this packet. See _stashed_packet in __init__.
                packet, self._stashed_packet = self._stashed_packet, None
            else:
                packet = self._receive_packet()

            if packet is True:
                continue
            if packet is None and not self.had_traffic:
                # Pre-handshake disconnect sentinel (the UDP thread's 5s
                # timeout tick against an unresponsive host — the thread
                # already re-SYNs on its own). MUST return, not swallow:
                # every pumping caller (wait_ready, the pooled worker, the
                # uploader's pump loops) checks its deadline/stop-event only
                # BETWEEN loop() returns. Swallowing these made a connect to
                # an unreachable ATEM un-timeout-able and un-killable —
                # hung upload processes + orphaned worker threads
                # (2026-07-06).
                return None
            if packet is None and self.state == UdpProtocol.STATE_SYN_SENT:
                # No response in connect, retry connection
                self.state = UdpProtocol.STATE_CLOSED
                self.had_traffic = False
                self.connect()
                return None

            if packet is None:
                # When None is in the receive queue the socket has disconnected
                return None

            if isinstance(packet, Wakeup):
                # An external caller poked us awake — typically because a
                # command was just queued and the upper layer wants the
                # worker to drain immediately rather than wait for ATEM's
                # next data packet. Bubble it up so ``loop()`` returns.
                return packet

            if self.mark_next_connected:
                # The packet just pulled is NOT consumed by the sentinel:
                # keep it for the next call. Dropping it lost the first
                # packet after every state dump — a real, reliable packet
                # the switcher will not resend usefully (its retransmits
                # are deduplicated as already-received).
                self.mark_next_connected = False
                self._stashed_packet = packet
                return ConnectionReady()

            if self.enable_ack and self.queue_trigger():
                # Same rule: the flush sentinel must not eat the packet
                # that happened to be pulled when the send queue drained —
                # mid-upload that packet can be the FTCD budget grant, and
                # losing it stalls the transfer until the caller's timeout.
                self._stashed_packet = packet
                return TransferQueueFlushed()

            if self.state == UdpProtocol.STATE_SYN_SENT:
                # Got response for the first handshake packet
                self.had_traffic = True
                self._handshake(packet)
            elif self.state == UdpProtocol.STATE_ESTABLISHED:
                if packet.length == 12:
                    # This is a control packet, deal with it in the transport layer
                    if not self.enable_ack:
                        # This is the first ACK from the mixer, after this we should send ACKs bac
                        self.enable_ack = True
                        # self.local_sequence_number = 0
                        ack = Packet()
                        ack.flags = UdpProtocol.FLAG_ACK
                        ack.acknowledgement_number = self.ack_number
                        ack.remote_sequence_number = 0x61
                        ack.label = 'initial ack after connection'
                        self._send_packet(ack)
                    # Retransmission requests are handled at the socket
                    # layer in _receive_packet_low;
                    # no other control packets need action here.

                    # Send queued up bulk traffic after the ack
                    if self.queue_trigger():
                        return TransferQueueFlushed()
                else:
                    # Data packet for the upper layer
                    return packet

    def send_packet(self, packet):
        self._send_packet(packet)


