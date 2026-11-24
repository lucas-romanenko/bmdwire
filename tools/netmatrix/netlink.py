# SPDX-License-Identifier: LGPL-3.0-only
"""A network in a process: a UDP relay between a client and a switcher
that delays, jitters, reorders, loses, rate-limits and size-limits the
datagrams going each way, as ``tc netem`` does, but on any OS and without
root. The network matrix (``tools/netmatrix.py``) and its fast subset in
the test suite put it between the real atemwire client and the fake
switcher (``fakeswitcher.py``).

One direction is a ``Path``:

- ``delay`` and ``jitter`` (seconds): each datagram is held ``delay``
  plus a uniform random ``-jitter..+jitter``. With ``reorder`` False the
  datagrams still leave in the order they came (jitter as a queue makes
  it); with ``reorder`` True each is held on its own, so they overtake
  each other (netem's jitter).
- ``late`` (0..1) and ``late_by`` (seconds): each datagram held back
  that much longer with that chance, so the ones after it overtake it
  (occasional reordering, as over Wi-Fi or two paths).
- ``loss`` (0..1): each datagram dropped with that chance.
- ``rate`` (bits/s) and ``queue`` (datagrams): a bottleneck. Datagrams go
  through it one at a time at ``rate``; one arriving with ``queue``
  already waiting is dropped (a short router queue, tail drop).
- ``max_dgram`` (bytes of UDP payload): anything bigger is dropped
  silently, as on a VPN with a small path MTU and no ICMP back.
- ``down``: everything dropped (a pulled cable), set with ``Relay.cut``.

The relay listens on ``listen`` for the client; for each client address
it opens its own socket towards the switcher, so the switcher sees one
peer per client as it would.
"""
import heapq
import random
import select
import socket
import threading
import time


def quiet_on_windows(sock):
    """Windows reports an ICMP port-unreachable from an earlier send as an
    error on the next receive (WSAECONNRESET) on a UDP socket; a relay
    must not stop for it."""
    if hasattr(socket, 'SIO_UDP_CONNRESET'):
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        except (OSError, ValueError):
            pass
    return sock


class Path:
    def __init__(self, delay=0.0, jitter=0.0, reorder=False, loss=0.0,
                 rate=0, queue=1000, max_dgram=0, seed=1, late=0.0, late_by=0.0):
        self.delay = delay
        self.jitter = jitter
        self.reorder = reorder
        self.late = late
        self.late_by = late_by
        self.loss = loss
        self.rate = rate
        self.queue = queue
        self.max_dgram = max_dgram
        self.down = False
        self.rng = random.Random(seed)
        self._link_free = 0.0       # when the bottleneck is free again
        self._queued = []           # times queued datagrams leave the bottleneck
        self._last_out = 0.0        # in-order jitter: none leaves before this
        self.stats = {'in': 0, 'out': 0, 'lost': 0, 'too_big': 0,
                      'queue_full': 0, 'cut': 0, 'reordered': 0}
        self._last_seq_out = 0

    def admit(self, size, now):
        """When a datagram of ``size`` bytes arriving ``now`` comes out the
        far end, or None when it is dropped."""
        self.stats['in'] += 1
        if self.down:
            self.stats['cut'] += 1
            return None
        if self.max_dgram and size > self.max_dgram:
            self.stats['too_big'] += 1
            return None
        if self.loss and self.rng.random() < self.loss:
            self.stats['lost'] += 1
            return None
        t = now
        if self.rate:
            while self._queued and self._queued[0] <= now:
                heapq.heappop(self._queued)
            if len(self._queued) >= self.queue:
                self.stats['queue_full'] += 1
                return None
            start = max(now, self._link_free)
            self._link_free = start + (size + 28) * 8.0 / self.rate
            heapq.heappush(self._queued, self._link_free)
            t = self._link_free
        t += self.delay
        if self.jitter:
            t += self.rng.uniform(-self.jitter, self.jitter)
        if self.late and self.rng.random() < self.late:
            # Held back on its own (another path, a retry on Wi-Fi): the
            # ones after it overtake it.
            t += self.late_by
            self.stats['reordered'] += 1
            self.stats['out'] += 1
            return max(t, now)
        if not self.reorder:
            t = max(t, self._last_out)
        elif t < self._last_out:
            self.stats['reordered'] += 1
        self._last_out = max(self._last_out, t)
        self.stats['out'] += 1
        return max(t, now)


class Relay:
    """The relay, on its own thread (``start``/``stop``) or in a process
    of its own (``tools/netmatrix.py`` runs it so, away from the client's
    GIL)."""

    def __init__(self, target, up, down, listen=('127.0.0.1', 0)):
        self.target = target
        self.up = up                # client -> switcher
        self.down = down            # switcher -> client
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(listen)
        quiet_on_windows(self.sock)
        self.addr = self.sock.getsockname()
        self._peers = {}            # client addr -> socket to the switcher
        self._back = {}             # that socket -> client addr
        self._heap = []
        self._n = 0
        self._stop = threading.Event()
        self._thread = None
        self._wake_r, self._wake_w = socket.socketpair() if hasattr(socket, 'socketpair') else (None, None)

    def cut(self, down=True):
        """Pull (True) or plug back (False) the cable, both ways."""
        self.up.down = self.down.down = down

    def _peer(self, client):
        s = self._peers.get(client)
        if s is None:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((self.addr[0], 0))
            quiet_on_windows(s)
            self._peers[client] = s
            self._back[s] = client
        return s

    def _forward(self, path, data, out_sock, dest, now):
        t = path.admit(len(data), now)
        if t is None:
            return
        self._n += 1
        heapq.heappush(self._heap, (t, self._n, out_sock, dest, data))

    def run(self, until=None):
        while not self._stop.is_set():
            now = time.monotonic()
            if until is not None and now >= until:
                return
            while self._heap and self._heap[0][0] <= now:
                _, _, s, dest, data = heapq.heappop(self._heap)
                try:
                    s.sendto(data, dest)
                except OSError:
                    pass
            wait = 0.05
            if self._heap:
                wait = min(wait, max(0.0, self._heap[0][0] - now))
            socks = [self.sock] + list(self._back)
            if self._wake_r is not None:
                socks.append(self._wake_r)
            try:
                r, _, _ = select.select(socks, [], [], wait)
            except (OSError, ValueError):
                return
            now = time.monotonic()
            for s in r:
                if s is self._wake_r:
                    try:
                        s.recv(64)
                    except OSError:
                        pass
                    continue
                for _ in range(256):
                    try:
                        s.setblocking(False)
                        data, src = s.recvfrom(65535)
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError:
                        # Windows: an ICMP port unreachable from an earlier
                        # send surfaces here (WSAECONNRESET); not ours.
                        break
                    if s is self.sock:
                        self._forward(self.up, data, self._peer(src), self.target, now)
                    else:
                        self._forward(self.down, data, self.sock, self._back[s], now)

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True, name='netlink-relay')
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._wake_w is not None:
            try:
                self._wake_w.send(b'x')
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(2)
        for s in [self.sock] + list(self._back):
            s.close()
