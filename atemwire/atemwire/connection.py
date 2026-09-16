# SPDX-License-Identifier: LGPL-3.0-only
"""
Thread-safe wrapper around ``AtemProtocol``.

atemwire's ``AtemProtocol`` is NOT thread-safe — ``loop()``,
``send_commands()``, and any other direct protocol calls must all happen on
one thread. ``ATEMConnection`` runs them on a dedicated worker thread; any
other thread wanting to send a command uses ``send(cmd)`` which enqueues
via a ``queue.Queue``.

The worker thread opens the atemwire connection, waits for the initial state
dump via ``atemwire.ready.wait_ready``, then pumps ``loop()`` and drains the
outgoing command queue until stopped.

Threading contract
==================

State guarded by ``_lifecycle_lock``:
    is_connected, ip_address, _protocol, _worker, _stop_event,
    _ready_event, _connect_error.

Worker liveness states (informal, reflected via ``is_connected`` +
``_worker.is_alive()``):

    [absent]     no worker has been started      _worker is None
    starting     worker thread started, not yet  _worker.is_alive(),
                 ready                            is_connected=False,
                                                  _ready_event clear
    ready        worker pumping protocol.loop    _worker.is_alive(),
                                                  is_connected=True,
                                                  _ready_event set
    stopping     _stop_event set, worker         _worker.is_alive(),
                 winding down                     is_connected=False
    dead         worker has exited               not _worker.is_alive(),
                                                  is_connected=False

``send()`` raises ``ConnectionDeadError`` if the worker is not in the
``ready`` state — i.e. if the queue would silently fill with no drain.
This is the contract that prevents the historical "command goes nowhere
and we wait 10s for a state update that never comes" failure mode.

Disconnect protocol:
    1. Set ``_stop_event``.
    2. Wake the worker out of its blocking ``thread_recv_queue.get()`` by
       putting ``None`` on ``transport.thread_recv_queue``. Otherwise the
       worker would never see ``_stop_event`` and ``worker.join`` would
       time out, leaving an orphan thread holding the socket.
    3. Join the worker (timeout=2.0s).
    4. If the worker did still not exit (extreme case — daemon thread
       is dead, or a bug elsewhere), close the socket from outside as a
       last-resort kick. The worker's next ``protocol.loop`` raises and
       the worker exits via the exception path.
    5. Drop references.
"""

import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

from atemwire.protocol import AtemProtocol
from atemwire.ready import WaitAborted, wait_ready
from atemwire.transport import TRACE_ENABLED, Wakeup, drain_log

logger = logging.getLogger(__name__)


# Wait up to this long for initial state (video-mode) after calling connect().
# Handshake plus the COMPLETE initial state dump (InCm seen). Ready used to
# mean "video-mode has arrived", which is the first packet of the dump, so
# 6 s was generous; a whole dump over a VPN can take several seconds on a
# large switcher, and a connect that times out is worse than one that takes
# eight seconds.
CONNECT_TIMEOUT = 15.0

# Cadence at which the worker pumps protocol.loop() while idle.
PUMP_INTERVAL = 0.01

# Spacing inserted BETWEEN back-to-back reliable packets when a single drain
# produced more than one (i.e. a burst — e.g. a profile apply dumping
# hundreds of commands). Without it the transport flushes up to batch_size
# (5) large reliable packets in <1ms on one ATEM packet; the switcher drops
# the tail of that microburst. Outbound retransmit-request handling now
# recovers such drops (the ATEM requests them, ``_retransmit_from`` in
# transport.py serves them go-back-N, 2026-06-12), but pacing is
# cheaper: it avoids the drop and the recovery round-trip in the first place.
# One packet per drain (normal live control — a slider tick, a cut) skips the
# spacing entirely, so realtime control latency is unchanged.
BURST_SEND_SPACING = 0.01

# How long disconnect() waits for the worker to exit cleanly. We FIRST wake
# the worker (via thread_recv_queue.put(None)), so this should normally
# complete in well under 100ms. The 2.0s budget is a safety net.
DISCONNECT_JOIN_TIMEOUT = 2.0


class ConnectionDeadError(RuntimeError):
    """Raised when a caller tries to use an ``ATEMConnection`` whose worker
    thread is no longer running.

    Replaces the historical silent-drop behaviour: ``send()`` used to log
    a warning and discard the command, which caused capture timeouts of
    up to 10s when the actual cause was that the worker had died. Now the
    failure surfaces immediately at the call site."""


class ATEMConnection:
    """Single ATEM connection — atemwire + dedicated worker thread.

    See module docstring for the threading contract.
    """

    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        self.ip_address: Optional[str] = None

        # Owned exclusively by the worker thread once started.
        self._protocol: Optional[AtemProtocol] = None

        # Cross-thread channels.
        self._cmd_queue: "queue.Queue[Any]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()   # set by worker once mixerstate is populated
        self._connect_error: Optional[str] = None

        # ``_handshake_done`` is True once the initial handshake + state
        # dump succeeded for the CURRENT worker. Cleared on disconnect so
        # ``is_connected`` (which reads worker.is_alive() AND this flag)
        # immediately reflects the post-disconnect state.
        self._handshake_done: bool = False

        self._lifecycle_lock = threading.Lock()  # serializes connect/disconnect calls
        self._transfer_serial_lock = threading.Lock()  # one native transfer at a time

        # Optional callback fired exactly once when the worker exits
        # unexpectedly (i.e. not via a deliberate disconnect()). The pool
        # uses this to evict dead instances. Set via ``set_on_died``.
        self._on_died: Optional[Callable[["ATEMConnection"], None]] = None
        self._died_signalled = False

        # Latched state — atemwire's macro-play-status (MRPr) reports the LIVE
        # running macro index only (0xFFFF when idle). The PyATEMMax fork
        # additionally latched a "last run" index that persists across idle
        # transitions; we reproduce that here by observing MRPr events in
        # the worker thread. Reads from other threads see the latched int.
        self.last_run_macro_index: int = -1

        logger.info(f"ATEM Connection Manager initialized for instance {instance_id}")

    # ---------------------------------------------------------------------
    # Public API (thread-safe unless noted)
    # ---------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """True iff the worker is alive AND the handshake has completed.

        This is NOT just "did connect() succeed" — it also reflects whether
        the worker thread is currently running. A worker that exited (any
        cause) flips this to False immediately, even if disconnect() was
        never explicitly called. Callers can rely on it as a fast pre-check
        before doing expensive work that would otherwise time out silently.
        """
        if not self._handshake_done:
            return False
        worker = self._worker
        return worker is not None and worker.is_alive()

    @property
    def mixerstate(self) -> dict:
        """Live state dict from atemwire. Safe to read from any thread — CPython
        dict reads are atomic under the GIL. Returns {} when not connected."""
        if self._protocol is None:
            return {}
        return self._protocol.mixerstate

    @property
    def protocol(self):
        """Underlying ``AtemProtocol`` — exposed for event registration and
        mixerstate access by long-lived observers (e.g. the media-pool
        watcher's binder). Returns None when not connected. Most callers
        should use ``send()``, ``mixerstate``, and the ``download_*``
        helpers instead."""
        return self._protocol

    def download_still(self, slot: int, *, timeout: float = 30.0,
                       progress_callback: Optional[Callable[[float], None]] = None) -> bytes:
        """Blocking native download of a STILL slot over this connection's
        normal packet loop. Returns RLE-decoded frame bytes ready for
        ``atemwire.imaging.atem_to_rgb``. See ``_download_transfer``."""
        return self._download_transfer(
            0, slot, timeout=timeout, progress_callback=progress_callback)

    def clear_still(self, slot: int, *, timeout: float = 5.0) -> None:
        """Blocking LOCKED clear of a STILL slot.

        Rides the transfer machinery's lock discipline (LOCK → LKOB →
        CSTL → per-frame release). The switcher silently ignores a bare
        CSTL from a session that does not hold the still-store lock
        (observed live 2026-07-29), so a plain ``send(ClearStillCommand)``
        is NOT a reliable clear — use this instead.

        Returns once the CSTL has been dispatched under the lock. That
        means "the switcher will process it", not "the slot is empty" —
        callers needing confirmation watch ``mediaplayer-file-info`` for
        the slot's ``is_used`` flipping False (the MPfe ack).

        Raises ``ConnectionDeadError`` if the connection is not ready, and
        ``TimeoutError`` if the lock wasn't granted within ``timeout``
        (another client holds the media lock). On timeout the queued clear
        is dequeued so it cannot fire much later against a slot an
        operator may have since reused.
        """
        if not self.is_connected:
            raise ConnectionDeadError(
                f"clear_still: connection to {self.ip_address} is not ready")
        ip = self.ip_address
        protocol = self._protocol
        done = threading.Event()

        def _on_dispatched(store_id, slot_idx):
            if store_id == 0 and slot_idx == slot:
                done.set()

        handler_id = protocol.on('clear-dispatched', _on_dispatched)
        try:
            task = protocol.queue_clear(0, slot)
            if not done.wait(timeout):
                dequeued = False
                try:
                    dequeued = protocol.dequeue_clear(task)
                except Exception:
                    # A dequeue FAILURE is ambiguous (we don't know whether
                    # the clear ran) — log it instead of silently treating
                    # it as "already dispatched" below.
                    logger.warning(
                        'clear_still: dequeue_clear failed for slot %s — '
                        'treating the clear as dispatched', slot,
                        exc_info=True)
                if dequeued:
                    raise TimeoutError(
                        f"clear_still: store lock for {ip} not granted "
                        f"within {timeout}s — another client is holding "
                        f"the media lock; clear of slot {slot} abandoned")
                # Task already dispatched — the event just raced our wait.
        finally:
            try:
                protocol.off('clear-dispatched', handler_id)
            except Exception:
                pass

    def download_macro(self, slot: int, *, timeout: float = 10.0) -> bytes:
        """Blocking native download of a MACRO slot's raw bytecode over this
        connection's normal packet loop (store 0xFFFF skips the still-store
        lock; the protocol layer sets the macro-store request magic). An
        empty macro slot returns ``b''`` (the ATEM sends FTDC with no
        FTDa). Parse with ``atemwire.macrotransfer.decode_macro_bytecode``.
        Bare-protocol callers (dev tools with no worker thread) should use
        ``atemwire.macrotransfer.download_macro_bytecode`` instead, which
        pumps the loop itself."""
        return self._download_transfer(0xFFFF, slot, timeout=timeout)

    def _download_transfer(self, store: int, index: int, *, timeout: float,
                           progress_callback: Optional[Callable[[float], None]] = None) -> bytes:
        """Shared blocking native-transfer wait (ASC-style shared-session
        path, 2026-07-02).

        The transfer interleaves with control traffic and state events — no
        ``exclusive_access``, so nothing sharing this connection freezes
        (measured: ~2s per 1080p still with ~20ms command echoes during the
        transfer). Safe to call from any thread; the worker thread pumps the
        packets. Serialized per connection (one transfer at a time — the
        ATEM's store lock enforces that for stills anyway).

        ``progress_callback`` receives a 0..1 fraction, fired from the
        worker thread — keep it cheap. Raises ``ConnectionDeadError`` if the
        connection is not ready, ``TimeoutError`` on timeout (after aborting
        the transfer so the connection's transfer lane can't stay wedged
        for later callers).
        """
        if not self.is_connected:
            raise ConnectionDeadError(
                f"download: connection to {self.ip_address} is not ready")
        # Captured up front: a disconnect racing the timeout wait clears
        # self.ip_address, and the error would read "on None".
        ip = self.ip_address
        protocol = self._protocol
        with self._transfer_serial_lock:
            done = threading.Event()
            result: dict = {}

            def _on_done(store_id, slot_idx, data):
                if store_id == store and slot_idx == index:
                    result['data'] = data
                    done.set()

            def _on_progress(store_id, slot_idx, fraction):
                if (progress_callback is not None
                        and store_id == store and slot_idx == index):
                    try:
                        progress_callback(fraction)
                    except Exception:
                        pass

            def _on_error(contents):
                # The protocol raises this ONLY for a fatal status (the
                # lock-dance statuses 1/5/6 are recovered internally), and
                # transfers on this connection are serialized, so it is
                # ours. It has already dropped the task and released the
                # lane — fail now instead of sitting out the timeout.
                result['error'] = contents
                done.set()

            done_id = protocol.on('download-done', _on_done)
            prog_id = protocol.on('transfer-progress', _on_progress)
            err_id = protocol.on('file-transfer-error', _on_error)
            try:
                protocol.download(store, index)
                if not done.wait(timeout):
                    # Read the lock belief BEFORE abort_transfers resets it:
                    # a timeout with the store lock never granted means the
                    # ATEM is refusing PLCK — another client (ASC, another
                    # application, an abandoned session) holds the media lock.
                    # That's the single most common cause of "images won't
                    # load", so name it instead of a bare timeout.
                    lock_never_granted = False
                    try:
                        lock_never_granted = not protocol.locks.get(store)
                    except Exception:
                        pass
                    try:
                        protocol.abort_transfers()
                    except Exception:
                        pass
                    hint = (" — media store lock never granted; another "
                            "client is holding it" if lock_never_granted
                            else "")
                    raise TimeoutError(
                        f"download: store {store} slot {index} on "
                        f"{ip} timed out ({timeout}s){hint}")
                if 'error' in result:
                    raise RuntimeError(
                        f"download: store {store} slot {index} on {ip} "
                        f"rejected by the switcher: {result['error']}")
                return result['data']
            finally:
                try:
                    protocol.off('download-done', done_id)
                    protocol.off('transfer-progress', prog_id)
                    protocol.off('file-transfer-error', err_id)
                except Exception:
                    pass

    def set_on_died(self, callback: Optional[Callable[["ATEMConnection"], None]]) -> None:
        """Register a callback fired ONCE when the worker exits unexpectedly.

        "Unexpectedly" means: the worker's main loop ended without
        ``disconnect()`` having been called by the user. The pool uses
        this to evict dead instances so subsequent ``get_instance`` calls
        create a fresh one rather than handing out a zombie.
        """
        self._on_died = callback

    def send(self, command) -> None:
        """Enqueue a atemwire Command for the worker thread to transmit.

        Raises ``ConnectionDeadError`` immediately if the worker is not
        running. This is intentional: silent queuing into a queue nobody
        drains is the single most painful failure mode in this codebase
        (10s capture timeouts that look like "ATEM is broken" but are
        actually "our process is broken"). Callers must handle this.
        """
        if not self.is_connected:
            raise ConnectionDeadError(
                f"Instance {self.instance_id}: send() while connection is "
                f"not in 'ready' state (worker_alive="
                f"{self._worker is not None and self._worker.is_alive()}, "
                f"handshake_done={self._handshake_done})"
            )
        self._cmd_queue.put(command)
        # Wake the worker out of any blocking ``protocol.loop()`` so the
        # command drains immediately, even if ATEM is currently silent
        # apart from PINGs (which don't return from ``receive_packet``).
        # Without this, queued commands stall until ATEM happens to emit
        # a data packet — observable as Phase-1 capture timeouts on a
        # warm-pool connection.
        self._wake_worker_for_drain()

    def connect(self, ip_address: str) -> bool:
        """Connect to the ATEM at `ip_address`. Blocks until connected + initial
        state (video-mode) arrives, or CONNECT_TIMEOUT elapses. Returns True on
        success, False on timeout / error."""
        with self._lifecycle_lock:
            # Disconnect first if already connected — either to a different IP
            # or to the same IP but possibly stale.
            if self._worker and self._worker.is_alive():
                if self._handshake_done and self.ip_address == ip_address:
                    logger.info(f"Instance {self.instance_id}: already connected to {ip_address}")
                    return True
                self._disconnect_locked()

            self._stop_event.clear()
            self._ready_event.clear()
            self._connect_error = None
            self._handshake_done = False
            self._died_signalled = False
            self.ip_address = ip_address

            # Drain any stale queued commands from a previous connection.
            try:
                while True:
                    self._cmd_queue.get_nowait()
            except queue.Empty:
                pass

            logger.info(f"Instance {self.instance_id}: connecting to ATEM at {ip_address}")
            self._worker = threading.Thread(
                target=self._run,
                daemon=True,
                name=f"AtemConn-{self.instance_id}",
            )
            self._worker.start()

            if not self._ready_event.wait(timeout=CONNECT_TIMEOUT):
                err = self._connect_error or f"timeout after {CONNECT_TIMEOUT}s"
                logger.warning(f"Instance {self.instance_id}: connect failed ({err})")
                self._disconnect_locked()
                return False

            if not self._handshake_done:
                # _ready_event was set by an early-error path (path A/B/C/D),
                # not a successful handshake.
                err = self._connect_error or "handshake did not complete"
                logger.warning(f"Instance {self.instance_id}: connect failed ({err})")
                self._disconnect_locked()
                return False

            logger.info(f"Instance {self.instance_id}: connected to {ip_address}")
            return True

    def disconnect(self) -> None:
        """Tear down the connection. Safe to call multiple times.

        After this returns, the worker thread is no longer running (or, in
        the extreme case, the socket has been force-closed and the worker
        is winding down within milliseconds). The connection is fully
        released — pool callers can safely reuse this object for a new
        ``connect()`` or drop it.
        """
        with self._lifecycle_lock:
            self._disconnect_locked(deliberate=True)

    # ---------------------------------------------------------------------
    # Worker thread
    # ---------------------------------------------------------------------

    def _run(self):
        """Main worker loop: open the atemwire connection, wait for initial
        state, then pump loop() and drain the command queue until stop.

        On exit (any cause that wasn't a deliberate disconnect), invokes
        ``self._on_died`` if registered. ``_died_signalled`` ensures it
        fires at most once per worker.
        """
        unexpected_exit = False
        try:
            try:
                self._protocol = AtemProtocol(ip=self.ip_address)
            except Exception as e:
                self._connect_error = f"AtemProtocol init failed: {e}"
                logger.exception(f"Instance {self.instance_id}: {self._connect_error}")
                self._ready_event.set()
                return

            # Latch "last run macro" index across idle transitions. MRPr is a
            # bare (non-indexed) field so the event name is 'change:macro-play-status'.
            def _on_macro_play(contents):
                running = bool(getattr(contents, 'running', False))
                idx = int(getattr(contents, 'index', 0xFFFF) or 0xFFFF)
                if running and idx != 0xFFFF and idx >= 0:
                    self.last_run_macro_index = idx

            self._protocol.on('change:macro-play-status', _on_macro_play)

            try:
                self._protocol.connect()
            except Exception as e:
                self._connect_error = f"connect() raised: {e}"
                logger.exception(f"Instance {self.instance_id}: {self._connect_error}")
                self._close_protocol()
                self._ready_event.set()
                return

            # Wait for initial handshake + initial state dump.
            try:
                wait_ready(
                    self._protocol,
                    timeout=CONNECT_TIMEOUT,
                    pump_interval=PUMP_INTERVAL,
                    stop_event=self._stop_event,
                )
            except TimeoutError:
                self._connect_error = f"initial state timeout ({CONNECT_TIMEOUT}s)"
                logger.warning(f"Instance {self.instance_id}: {self._connect_error}")
                self._close_protocol()
                self._ready_event.set()
                return
            except WaitAborted:
                self._close_protocol()
                self._ready_event.set()
                return

            # Handshake done — flip the public flag BEFORE setting _ready_event
            # so callers that wake on _ready_event see the right state.
            self._handshake_done = True
            self._ready_event.set()
            logger.debug(
                f"Instance {self.instance_id}: worker ready "
                f"({len(self._protocol.mixerstate)} state keys)"
            )

            # Main loop: pump + drain outgoing commands. Sleep ONLY when
            # the receive queue is empty: a fixed sleep per pump throttled
            # packet processing to ~100/s, which turned a native ~6000-
            # packet still transfer from ~2s into a 60s+ timeout (found
            # live, ASC-style step 2). Idle behaviour is unchanged —
            # loop() blocks on the empty queue anyway; the sleep only
            # paces the cmd-queue drain cadence between quiet packets.
            while not self._stop_event.is_set():
                self._pump_loop_once()
                self._drain_cmd_queue()
                if self._transport_thread_died():
                    # The UDP thread is gone: no packets will ever arrive
                    # again and every send vanishes. Without this check the
                    # worker parked forever on the empty queue with
                    # is_connected still True — a zombie lane the pool kept
                    # serving (2026-07-06).
                    # Raising routes through the unexpected-exit handler:
                    # goodbye + on_died -> pool eviction -> callers reconnect.
                    raise RuntimeError(
                        "transport UDP thread died — failing the worker so "
                        "the pool evicts this connection")
                try:
                    busy = self._protocol.transport.thread_recv_queue.qsize() > 0
                except Exception:
                    busy = False
                if not busy:
                    time.sleep(PUMP_INTERVAL)

            # Stop event was set — this is a deliberate disconnect.
            self._close_protocol()
            logger.info(f"Instance {self.instance_id}: worker exited (deliberate)")
        except BaseException as e:
            # ANY uncaught exception in the worker is "unexpected". This used
            # to crash the thread silently; now we mark it and let the pool
            # evict the instance.
            unexpected_exit = True
            logger.exception(
                f"Instance {self.instance_id}: worker exited via uncaught "
                f"exception: {type(e).__name__}: {e}"
            )
            try:
                self._close_protocol()
            except Exception:
                pass
        finally:
            # If we exited the main loop because protocol.loop() returned
            # disconnect (None packet) but _stop_event was NOT set, that's
            # also unexpected.
            if not self._stop_event.is_set() and self._handshake_done:
                unexpected_exit = True
                logger.warning(
                    f"Instance {self.instance_id}: worker exited without "
                    f"_stop_event being set (unexpected)"
                )
            # Always drop the handshake-done flag last — readers of
            # is_connected should see False immediately on worker exit.
            self._handshake_done = False
            if unexpected_exit:
                self._fire_died()

    def _fire_died(self):
        """Invoke the on_died callback at most once per worker."""
        if self._died_signalled:
            return
        self._died_signalled = True
        cb = self._on_died
        if cb is None:
            return
        try:
            cb(self)
        except Exception as e:
            logger.warning(
                f"Instance {self.instance_id}: on_died callback raised: {e}"
            )

    def _pump_loop_once(self):
        if self._protocol is None:
            return
        try:
            self._protocol.loop()
        except Exception as e:
            logger.warning(f"Instance {self.instance_id}: protocol.loop() error: {e}")

    def _drain_cmd_queue(self):
        if self._protocol is None:
            return
        # Non-blocking drain. Render each command exactly once, then
        # send as a concatenated raw payload via ``protocol.send_raw``.
        # Chunking: when the batched payload would exceed the per-UDP
        # budget, flush the current batch and start a fresh one. A
        # single command whose serialization raises is logged and
        # skipped so it doesn't poison the rest of the batch.
        pending = []
        while True:
            try:
                pending.append(self._cmd_queue.get_nowait())
            except queue.Empty:
                break
        if not pending:
            return
        if TRACE_ENABLED:
            try:
                cmd_names = [type(c).__name__ for c in pending]
                drain_log.info(
                    f"Instance {self.instance_id}: draining {len(pending)} cmd(s): {cmd_names}"
                )
            except Exception:
                pass

        # Per-packet UDP budget. Commands are typically 12-32 bytes
        # each, so a budget below the protocol's hard 1300 leaves
        # headroom. We size each command by ``get_command()`` here and
        # again inside ``send_commands`` — all Send subclasses are
        # mutation-free in ``get_command`` (verified by the message
        # tests), so the double render is wasteful but harmless.
        BUDGET = 1200
        chunks: list = []   # each entry is a command-list ≤ BUDGET bytes
        batch: list = []
        total = 0
        for cmd in pending:
            try:
                size = len(cmd.get_command())
            except Exception as e:
                logger.warning(
                    f"Instance {self.instance_id}: dropping bad "
                    f"{type(cmd).__name__}: {e}"
                )
                continue
            if size > BUDGET:
                logger.warning(
                    f"Instance {self.instance_id}: dropping "
                    f"{type(cmd).__name__} ({size}B exceeds UDP budget)"
                )
                continue
            if batch and total + size > BUDGET:
                chunks.append(batch)
                batch = []
                total = 0
            batch.append(cmd)
            total += size
        if batch:
            chunks.append(batch)

        # Send the chunks, spacing back-to-back packets so a config burst
        # (profile apply) trickles into the transport instead of arriving as
        # one batch_size microburst the switcher drops the tail of. Two
        # carve-outs keep latency/throughput-sensitive paths at full speed:
        #   * a single chunk (normal live control — slider tick, cut) skips
        #     the spacing (``i`` is 0), so realtime control latency is
        #     unchanged;
        #   * while a file transfer is in flight (macro / still upload via
        #     ``protocol.upload``) we do NOT sleep. Transfer DATA bypasses
        #     this drain (it goes straight to the transport), but the
        #     transfer's FTDa/FTUA cycle is driven by ``protocol.loop()`` on
        #     THIS worker thread — a blocking sleep here starves that pump
        #     and stalls the transfer into a retransmit/timeout. So when a
        #     transfer is running, drain fast and get back to pumping.
        for i, chunk in enumerate(chunks):
            if i and not self._transfer_active():
                time.sleep(BURST_SEND_SPACING)
            self._send_batch(chunk)

    def _transfer_active(self) -> bool:
        """True while a file transfer (macro / still upload via
        ``protocol.upload``) is queued or in flight. Its FTDa/FTUA cycle is
        pumped by ``protocol.loop()`` on this worker thread, so the drain
        must not block on ``BURST_SEND_SPACING`` while one is running."""
        p = self._protocol
        if p is None:
            return False
        if getattr(p, 'transfer', None) is not None:
            return True
        if getattr(p, 'transfer_requested', False):
            return True
        tq = getattr(p, 'transfer_queue', None)
        return bool(tq) and any(tq.values())

    def _send_batch(self, batch):
        try:
            self._protocol.send_commands(batch)
        except Exception as e:
            logger.warning(
                f"Instance {self.instance_id}: send_commands() error "
                f"(dropped {len(batch)} command(s)): {e}"
            )


    def _transport_thread_died(self) -> bool:
        """True iff the protocol's transport thread was started and has
        since exited. None/absent thread (fakes, non-UDP transports) and
        not-yet-started threads report False."""
        protocol = self._protocol
        if protocol is None:
            return False
        thread = getattr(protocol.transport, 'thread', None)
        if thread is None or thread.ident is None:
            return False
        return not thread.is_alive()

    def _close_protocol(self):
        if self._protocol is None:
            return
        # Give the media lock back BEFORE the goodbye. A lock we still hold
        # at teardown outlives us: the switcher keeps it for the rest of the
        # session's life, and for an abandoned session that is its own ~5
        # minute timeout. Every client that asks for the media store in that
        # window is refused, which is a switcher that will not load a still
        # and an ATEM Software Control media pool slot that spins forever on
        # a thumbnail it cannot download. Releasing after the goodbye would
        # be releasing into a session that has already ended, so the order
        # here matters.
        try:
            self._protocol.release_locks_now()
        except Exception:
            pass
        # Protocol-level goodbye BEFORE the socket close — an abandoned
        # session wedges the switcher (see transport.close_session).
        try:
            self._protocol.transport.close_session()
        except Exception:
            pass
        try:
            self._protocol.transport.sock.close()
        except Exception:
            pass
        # Free the SocketQueue's socketpair FDs too — they were GC-only,
        # which pinned 2 FDs per leaked protocol (2026-07-06).
        try:
            self._protocol.transport.thread_queue.close()
        except Exception:
            pass
        self._protocol = None

    def _wake_worker(self):
        """Wake the worker out of any blocking ``thread_recv_queue.get()`` so
        it can observe ``_stop_event`` and exit promptly. Safe to call when
        there's no live protocol."""
        p = self._protocol
        if p is None:
            return
        try:
            # The transport's _udp_thread also writes to thread_recv_queue;
            # multiple Nones are fine (None == disconnect signal, idempotent).
            p.transport.thread_recv_queue.put(None)
        except Exception:
            pass

    def _wake_worker_for_drain(self):
        """Wake the worker so it returns from ``protocol.loop()`` and drains
        the outbound command queue. Unlike ``_wake_worker`` (which puts
        ``None`` to signal disconnect), this puts a ``Wakeup`` sentinel that
        ``receive_packet`` and ``loop()`` recognize as a no-op return."""
        p = self._protocol
        if p is None:
            return
        try:
            p.transport.thread_recv_queue.put(Wakeup())
        except Exception:
            pass

    def _disconnect_locked(self, deliberate: bool = True):
        """Caller must hold _lifecycle_lock.

        Two-phase wake-then-join. The historical bug: just setting
        ``_stop_event`` doesn't wake a worker that's blocked in
        ``thread_recv_queue.get()`` (no timeout). So the worker would
        never observe stop_event, ``worker.join(timeout=2.0)`` would
        exhaust, and ``self._worker = None`` would orphan the thread.

        Fix: put None on the transport's recv queue first. This unblocks
        receive_packet() → protocol.loop() returns → main loop checks
        _stop_event → exits → _close_protocol → done. Should complete in
        well under 100ms.
        """
        # Suppress the on_died callback for deliberate disconnects.
        if deliberate:
            self._died_signalled = True

        self._handshake_done = False
        self._stop_event.set()

        worker = self._worker
        if worker is not None and worker.is_alive():
            # Phase 1: wake the worker out of any blocking get().
            self._wake_worker()
            # Phase 2: join with a budget.
            worker.join(timeout=DISCONNECT_JOIN_TIMEOUT)

            if worker.is_alive():
                # Phase 3 (last resort): the worker still didn't exit. Close
                # the socket from outside; whatever the worker is doing,
                # it'll fail and the worker will fall through to its
                # exception handler.
                logger.warning(
                    f"Instance {self.instance_id}: worker did not exit within "
                    f"{DISCONNECT_JOIN_TIMEOUT}s after wake; force-closing socket"
                )
                self._close_protocol()
                worker.join(timeout=DISCONNECT_JOIN_TIMEOUT)
                if worker.is_alive():
                    logger.error(
                        f"Instance {self.instance_id}: worker STILL alive after "
                        f"force-close — orphan thread"
                    )

        self._worker = None
        self.ip_address = None
        self._ready_event.clear()
        logger.info(f"Instance {self.instance_id}: disconnected")


