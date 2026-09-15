# SPDX-License-Identifier: LGPL-3.0-only
"""
Ref-counted singleton-per-IP pool of ``ATEMConnection`` instances.

Multiple callers — different browser tabs, the media pool watcher, etc. —
need to share one UDP socket per ATEM rather than each opening their own.
``ATEMInstanceManager`` keeps a module-level dict keyed by IP; each caller
acquires a ref via ``get_instance(ip)`` and releases it via
``release_instance(ip)``. When the last reference goes away the connection
is torn down after a 2-second grace period via ``threading.Timer``, giving
quick page transitions a window to reuse the existing socket.

IMPORTANT — single-process invariant: the instance dict is process-local.
Running several worker processes (for example multiple ASGI workers) gives
each one an independent copy, which breaks reference counting and opens
duplicate ATEM sessions. Use one process per pool, or add a shared broker.

Threading contract
==================

Two layers of locking:

    ``_instance_lock`` (class-level) guards ``_instances`` dict structure
    and per-entry ref_count / disconnect_timer / state.

    ``ATEMConnection._lifecycle_lock`` (instance-level) guards the
    connect/disconnect transitions on a single connection.

Lifecycle states (tracked in the ``state`` field of each entry):

    ACTIVE         entry exists, refcount > 0, worker alive (or being
                   started). New get_instance increments refcount.
    DRAINING       entry exists, refcount == 0, grace timer armed. New
                   get_instance cancels the timer and transitions back
                   to ACTIVE without re-handshaking.
    TEARING_DOWN   timer fired and is currently inside the callback,
                   actively disconnecting. New get_instance MUST wait
                   for the callback to complete (releases the lock).
                   When the callback returns, the entry has been
                   removed from the dict, and get_instance creates a
                   fresh ACTIVE entry.

Transitions are atomic — they all happen while holding ``_instance_lock``,
except that the actual ``connection.disconnect()`` call inside
TEARING_DOWN is the long part. Importantly, ``disconnect()`` now wakes the
worker before joining (see ``ATEMConnection._disconnect_locked``), so the
TEARING_DOWN state is brief (< 100ms typical, < 2s pathological) — the
historical 4-second pathology is gone.

Race window the historical bug exploited (now fixed):

  1. Refcount → 0. Timer armed (DRAINING).
  2. Timer fires. State → TEARING_DOWN. ``connection.disconnect()`` runs.
  3. Worker is blocked in protocol.loop() → thread_recv_queue.get().
     Setting _stop_event doesn't wake it. ``worker.join(timeout=2.0)``
     exhausts. Disconnect returns "complete" but the worker is alive.
  4. ``del cls._instances[ip]``. Lock released.
  5. Concurrent get_instance acquires lock, ip not in dict, creates a
     fresh instance.
  6. The OLD worker is orphan, holds OLD socket, still ACKs ATEM
     packets on OLD session. ATEM has TWO active sessions. State
     events / capture responses may go to OLD; NEW's mixerstate
     doesn't update; capture timeout.

The fix: ``ATEMConnection._disconnect_locked`` now wakes the worker via
``transport.thread_recv_queue.put(None)`` BEFORE joining. The worker
unblocks, observes ``_stop_event``, exits its main loop, closes its
socket — all within a few milliseconds. The TEARING_DOWN state is brief
and the new instance can never see an orphaned old worker.

Defence in depth:

  * ATEMConnection.is_connected now reflects worker liveness (it reads
    self._worker.is_alive()). A connection whose worker died for ANY
    reason — bug, OS error, force-kill — flips to is_connected=False
    immediately. acquire_connection's healing path (call conn.connect()
    if not is_connected) replaces the dead worker with a fresh one
    using the same ATEMConnection object.
  * ATEMConnection.send() raises ConnectionDeadError instead of
    silently queuing into a dead worker's queue.
  * The pool's get_instance verifies health on return (logs a warning
    if it returned a connection whose worker is not alive — which
    should now be impossible, but if it ever happens we want loud
    evidence not a 10s capture timeout).
"""

import atexit
import logging
import threading
from contextlib import contextmanager

from atemwire.connection import ATEMConnection, ConnectionDeadError  # noqa: F401

logger = logging.getLogger(__name__)


# Grace period (seconds) before disconnecting ATEM when ref_count reaches 0.
# Prevents disconnection during page transitions (connect page -> control page).
DISCONNECT_GRACE_PERIOD = 2.0

# Lifecycle states stored in instance entry's 'state' field.
_STATE_ACTIVE = 'active'
_STATE_DRAINING = 'draining'
_STATE_TEARING_DOWN = 'tearing_down'


class ATEMInstanceManager:
    """Manages shared ATEM instances with reference counting.

    IMPORTANT: process-local class state. Only safe with a single Uvicorn
    worker process.
    """

    _instances = {}
    _instance_lock = threading.Lock()

    @classmethod
    def peek_connection(cls, ip_address):
        """Return a live, already-pooled ``ATEMConnection`` for ``ip_address``,
        or ``None``. **Never creates or connects** and takes no ref — safe to
        call from a web-request thread (no handshake, no blocking I/O). Use it
        to opportunistically reach an ATEM only when a session is already up.
        """
        with cls._instance_lock:
            entry = cls._instances.get(ip_address)
            conn = entry.get('connection') if entry else None
        if conn is not None and getattr(conn, 'is_connected', False):
            return conn
        return None

    @classmethod
    def get_instance(cls, ip_address):
        """Get or create an ATEM instance, incrementing its ref count.

        Always returns an entry whose connection is in a usable state for
        ``conn.connect(ip)`` to either join (warm) or start fresh (cold).
        Will never return an entry mid-teardown — concurrent calls block
        until the teardown finishes and a fresh entry is created.
        """
        with cls._instance_lock:
            return cls._get_instance_locked(ip_address)

    @classmethod
    def _get_instance_locked(cls, ip_address):
        """Caller must hold _instance_lock."""
        existing = cls._instances.get(ip_address)
        if existing is not None:
            # If the worker died unexpectedly between callers, evict and
            # create fresh. With the fix to _disconnect_locked this should
            # be rare, but it's the defence-in-depth check that prevents
            # the historical "silent zombie" failure mode.
            conn = existing['connection']
            worker = getattr(conn, '_worker', None)
            if worker is not None and not worker.is_alive() and existing.get('state') == _STATE_ACTIVE:
                logger.warning(
                    f"ATEM instance {ip_address} found with dead worker "
                    f"(state=ACTIVE, _handshake_done="
                    f"{getattr(conn, '_handshake_done', '?')}); evicting and "
                    f"creating fresh"
                )
                cls._evict_dead_locked(ip_address, reason='dead worker on get_instance')
                existing = None

        if existing is None:
            instance_id = f"atem_{ip_address.replace('.', '_')}"
            connection = ATEMConnection(instance_id)
            entry = {
                'connection': connection,
                'ref_count': 0,
                'disconnect_timer': None,
                'state': _STATE_ACTIVE,
            }
            # Register on_died callback so any future unexpected worker
            # exit (any cause) auto-evicts the entry. Lock is acquired
            # by the callback; safe because the worker can only die
            # asynchronously.
            connection.set_on_died(cls._on_connection_died)
            cls._instances[ip_address] = entry
            logger.info(f"Created new ATEM instance for {ip_address}")
            existing = entry
        else:
            # Cancel pending disconnect — new reference arrived during grace.
            timer = existing.get('disconnect_timer')
            if timer is not None:
                timer.cancel()
                existing['disconnect_timer'] = None
                existing['state'] = _STATE_ACTIVE
                logger.info(
                    f"Cancelled pending disconnect for {ip_address} — new reference arrived"
                )

        existing['ref_count'] += 1
        logger.info(
            f"ATEM instance {ip_address} reference count: {existing['ref_count']}"
        )
        return existing

    @classmethod
    def release_instance(cls, ip_address, instance=None):
        """Release a reference. Schedules disconnect after grace period when
        the last reference goes away.

        ``instance`` (optional): the entry dict this holder got from
        ``get_instance``. When given, the release is IDENTITY-GUARDED: if
        the registry's current entry for this IP is a DIFFERENT object —
        ours was evicted after a worker death and a fresh entry created by
        another holder — the release is a no-op. Our reference died with
        the evicted entry; decrementing the replacement would steal a
        reference from its live holders (grace-teardown under their feet).
        Long-lived holders (e.g. the media-pool watcher's binder) should
        always pass it."""
        with cls._instance_lock:
            instance_now = cls._instances.get(ip_address)
            if instance_now is None:
                logger.warning(f"Attempted to release non-existent instance {ip_address}")
                return
            if instance is not None and instance_now is not instance:
                logger.info(
                    f"Release of {ip_address} skipped — entry was replaced "
                    f"(evicted after worker death); this reference already "
                    f"died with the old entry")
                return
            instance = instance_now

            if instance['ref_count'] <= 0:
                # Double release. Decrementing below zero would arm a
                # second grace timer and could steal a ref a concurrent
                # holder just took through the 0-window (2026-07-06).
                logger.error(
                    f"Double release for {ip_address} "
                    f"(ref_count={instance['ref_count']}) — ignoring")
                return

            instance['ref_count'] -= 1
            ref_count = instance['ref_count']
            logger.info(f"ATEM instance {ip_address} reference count: {ref_count}")

            if ref_count > 0:
                return

            # Cancel any prior pending timer (defensive — shouldn't have one
            # if state was ACTIVE and we just decremented to 0).
            old_timer = instance.get('disconnect_timer')
            if old_timer is not None:
                old_timer.cancel()
                instance['disconnect_timer'] = None

            instance['state'] = _STATE_DRAINING

            timer = threading.Timer(
                DISCONNECT_GRACE_PERIOD,
                cls._delayed_disconnect, args=(ip_address,),
            )
            timer.daemon = True
            instance['disconnect_timer'] = timer
            timer.start()
            logger.info(
                f"Scheduled disconnect for {ip_address} in "
                f"{DISCONNECT_GRACE_PERIOD}s (grace period)"
            )

    @classmethod
    def _delayed_disconnect(cls, ip_address):
        """Timer callback. Acquires lock, transitions to TEARING_DOWN, runs
        the disconnect, removes the entry. Held lock means concurrent
        get_instance blocks until the callback finishes — so a get_instance
        is guaranteed to see either the live entry (timer cancelled it
        first) or no entry at all (callback finished first); never an
        in-progress teardown.
        """
        with cls._instance_lock:
            instance = cls._instances.get(ip_address)
            if instance is None:
                logger.debug(
                    f"Disconnect timer for {ip_address} fired but instance "
                    f"already removed"
                )
                return
            if instance['ref_count'] > 0:
                # Someone reacquired between our timer firing and us
                # acquiring the lock. Standard cancellation race — bail.
                logger.info(
                    f"ATEM instance {ip_address} reconnected during grace period"
                )
                return
            if instance.get('state') == _STATE_TEARING_DOWN:
                # Pathological: somehow this got here twice. Idempotent.
                return

            instance['state'] = _STATE_TEARING_DOWN
            logger.info(f"Grace period expired for {ip_address}, disconnecting")
            try:
                instance['connection'].disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting ATEM {ip_address}: {e}")
            del cls._instances[ip_address]
            logger.info(f"ATEM instance {ip_address} cleaned up")

    @classmethod
    def _evict_dead_locked(cls, ip_address, reason: str):
        """Caller must hold _instance_lock. Tear down (best-effort) and
        remove an instance whose worker has died unexpectedly."""
        instance = cls._instances.get(ip_address)
        if instance is None:
            return
        timer = instance.get('disconnect_timer')
        if timer is not None:
            timer.cancel()
        try:
            instance['connection'].disconnect()
        except Exception as e:
            logger.error(f"Error disconnecting dead instance {ip_address}: {e}")
        del cls._instances[ip_address]
        logger.warning(f"ATEM instance {ip_address} evicted (reason: {reason})")

    @classmethod
    def _on_connection_died(cls, conn):
        """Callback registered on every new ATEMConnection. Fires once when
        the worker exits unexpectedly (not via deliberate disconnect).

        Runs on the worker thread itself. Acquires _instance_lock to
        evict the entry from the pool. Subsequent get_instance for this
        IP will create a fresh entry.
        """
        ip_address = conn.ip_address
        if ip_address is None:
            # Worker died before connect() set ip_address; nothing to evict.
            return
        with cls._instance_lock:
            existing = cls._instances.get(ip_address)
            if existing is None or existing['connection'] is not conn:
                # Already cleaned up, or replaced by a newer entry. Either
                # way, nothing to do.
                return
            # Don't tamper with refcount — callers still holding the dead
            # ATEMConnection will see is_connected=False and either
            # reconnect (if they go through acquire_connection) or get a
            # ConnectionDeadError on send(). Just remove it from the pool
            # so subsequent get_instance creates fresh.
            timer = existing.get('disconnect_timer')
            if timer is not None:
                timer.cancel()
            del cls._instances[ip_address]
            logger.warning(
                f"ATEM instance {ip_address} evicted (worker died unexpectedly)"
            )

    @classmethod
    def list_instances(cls):
        """List all active instances with connection status and ref counts."""
        with cls._instance_lock:
            return {
                ip: {
                    'connected': instance['connection'].is_connected,
                    'ref_count': instance['ref_count'],
                    'state': instance.get('state'),
                }
                for ip, instance in cls._instances.items()
            }


# ============================================================================
# Canonical "get me a connection to this ATEM" helper
# ============================================================================


@contextmanager
def acquire_connection(ip_address: str):
    """Context manager yielding a live, ready ``ATEMConnection`` to the given IP.

    Exactly one connection story for the whole codebase. Two paths:

    - **Pool hit** (warm): the pool already has a connected instance for this
      IP. Ref count is bumped and the connection is yielded immediately. No
      handshake cost.
    - **Pool miss / stale** (cold): ``ATEMInstanceManager.get_instance`` creates
      or returns the instance, this helper calls ``connect(ip)`` on it (which
      does the full handshake + state dump), and yields on success. If the
      existing pooled connection is for a different IP (or disconnected), we
      also re-connect.

    On exit, the ref count is decremented. If this was the last ref, the pool's
    normal 2-second grace-period teardown applies — so subsequent short-lived
    operations on the same IP get the warm path for free.

    This is the only way application code (outside atemwire's own internals)
    should get a connection. Opening ``AtemProtocol(ip)`` directly is reserved
    for a dedicated bulk-upload process, which runs SEPARATELY from the
    pool's process and needs ``aggressive_drain=True`` transport
    tuning the pool can't provide. (Long-lived observers such as a media-pool
    watcher should ride THIS pooled connection via ``get_instance`` rather
    than open a second socket; every extra session costs the switcher a slot.)

    :raises RuntimeError: if the underlying connection fails to become ready
        within the connection's configured timeout.
    """
    instance = ATEMInstanceManager.get_instance(ip_address)
    conn = instance['connection']
    try:
        # is_connected reflects worker liveness post-fix — a dead worker
        # flips this to False and conn.connect() will start a fresh worker
        # (reusing the same ATEMConnection object).
        if not (conn.is_connected and conn.ip_address == ip_address):
            ok = conn.connect(ip_address)
            if not ok:
                err = getattr(conn, '_connect_error', None) or 'connect failed'
                raise RuntimeError(f"ATEM {ip_address}: {err}")
        yield conn
    finally:
        ATEMInstanceManager.release_instance(ip_address)


def close_all_sessions_at_exit():
    """Interpreter-exit safety net: send the protocol goodbye on every
    pooled connection that is still open.

    A process exit without the goodbye (deploy restart, watchmedo reload,
    SIGTERM) abandons the session on the switcher — and an abandoned
    session holds any store lock it took until the switcher reaps it, or
    forever on a corrupted session table (the 2026-07 fossil locks).
    Verified 2026-07-06: close_session frees a held lock instantly.

    Deliberately does NOT run the full ``disconnect()`` path — joining
    worker threads during interpreter shutdown is unreliable. The goodbye
    is a single direct socket write; the ATEM tears the whole session
    (and its locks) on receipt, so the dying process needs nothing else.
    """
    with ATEMInstanceManager._instance_lock:
        entries = list(ATEMInstanceManager._instances.values())
    for entry in entries:
        conn = entry.get('connection')
        if conn is None:
            continue
        proto = getattr(conn, '_protocol', None)
        if proto is None:
            continue
        try:
            proto.transport.close_session()
            logger.info(f"atexit: clean session close sent for {conn.ip_address}")
        except Exception:
            pass


atexit.register(close_all_sessions_at_exit)
