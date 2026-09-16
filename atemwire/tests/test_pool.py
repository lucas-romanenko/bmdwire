# SPDX-License-Identifier: LGPL-3.0-only
"""Unit tests for atemwire.pool.ATEMInstanceManager.

These exercise the full lifecycle:

  ABSENT → ACTIVE → DRAINING → TEARING_DOWN → ABSENT

with focus on the historical race: TEARING_DOWN must complete before a
concurrent get_instance can either reacquire (cancelling the timer) or
create a fresh entry (after the entry has been deleted). The fix to
ATEMConnection._disconnect_locked makes TEARING_DOWN brief, so the
race window that produced silent zombie connections is eliminated.
"""

import threading
import time

import pytest

from atemwire.pool import ATEMInstanceManager, acquire_connection


# ---------------------------------------------------------------------------
# Basic refcounting
# ---------------------------------------------------------------------------


def test_get_instance_creates_entry(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    fake_protocol_factory()
    inst = ATEMInstanceManager.get_instance('1.2.3.4')
    try:
        assert inst['ref_count'] == 1
        assert inst['state'] == 'active'
        assert '1.2.3.4' in ATEMInstanceManager._instances
    finally:
        ATEMInstanceManager.release_instance('1.2.3.4')


def test_get_instance_increments_refcount(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    fake_protocol_factory()
    a = ATEMInstanceManager.get_instance('1.2.3.4')
    b = ATEMInstanceManager.get_instance('1.2.3.4')
    try:
        assert a is b
        assert a['ref_count'] == 2
    finally:
        ATEMInstanceManager.release_instance('1.2.3.4')
        ATEMInstanceManager.release_instance('1.2.3.4')


def test_release_below_zero_logs_warning(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
    caplog,
):
    fake_protocol_factory()
    # Releasing a non-existent IP must not raise.
    ATEMInstanceManager.release_instance('9.9.9.9')
    # And must log a warning so we notice misuse.
    assert any('non-existent' in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Grace-period timer
# ---------------------------------------------------------------------------


def test_release_arms_timer(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    fake_protocol_factory()
    inst = ATEMInstanceManager.get_instance('1.2.3.4')
    ATEMInstanceManager.release_instance('1.2.3.4')
    # Entry still in dict, in DRAINING state.
    with ATEMInstanceManager._instance_lock:
        entry = ATEMInstanceManager._instances.get('1.2.3.4')
        assert entry is not None
        assert entry['state'] == 'draining'
        assert entry['disconnect_timer'] is not None


def test_warm_reacquire_within_grace(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    """The user's "page transition" scenario: release + re-acquire within
    the grace window must reuse the existing connection."""
    fake_protocol_factory()
    a = ATEMInstanceManager.get_instance('1.2.3.4')
    ATEMInstanceManager.release_instance('1.2.3.4')
    # short_grace = 0.1s, sleep 0.05 — well inside.
    time.sleep(0.05)
    b = ATEMInstanceManager.get_instance('1.2.3.4')
    try:
        assert a is b
        assert b['state'] == 'active'
        assert b['disconnect_timer'] is None
        assert b['ref_count'] == 1
    finally:
        ATEMInstanceManager.release_instance('1.2.3.4')


def test_post_grace_reacquire_is_fresh(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    """The user's actual failing scenario: release, wait for grace to fully
    expire and tear down, then reacquire. Must get a fresh ATEMConnection."""
    fake_protocol_factory()
    a = ATEMInstanceManager.get_instance('1.2.3.4')
    a_conn = a['connection']
    ATEMInstanceManager.release_instance('1.2.3.4')
    # short_grace = 0.1s; wait > 0.5s to ensure timer fires + teardown done.
    time.sleep(0.5)
    b = ATEMInstanceManager.get_instance('1.2.3.4')
    try:
        assert a is not b
        assert b['connection'] is not a_conn
        assert b['ref_count'] == 1
        assert b['state'] == 'active'
    finally:
        ATEMInstanceManager.release_instance('1.2.3.4')


# ---------------------------------------------------------------------------
# The race: post-grace reacquire MUST get a connection whose worker is
# alive — the historical bug returned an entry whose worker was orphaned.
# ---------------------------------------------------------------------------


def test_post_grace_reacquire_worker_is_alive(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    """The fix being verified: after grace + teardown, the next acquire
    must return a connection whose worker is alive AND is_connected
    reports True. This is the exact scenario that produced silent
    zombie connections in production."""
    fake_protocol_factory()
    inst = ATEMInstanceManager.get_instance('1.2.3.4')
    # Need to do conn.connect() to actually start the worker.
    conn = inst['connection']
    assert conn.connect('1.2.3.4')
    ATEMInstanceManager.release_instance('1.2.3.4')

    # Wait for grace + teardown to complete.
    time.sleep(0.5)

    inst2 = ATEMInstanceManager.get_instance('1.2.3.4')
    try:
        conn2 = inst2['connection']
        # Connect the new instance.
        assert conn2.connect('1.2.3.4')
        assert conn2.is_connected is True
        assert conn2._worker is not None and conn2._worker.is_alive()
    finally:
        ATEMInstanceManager.release_instance('1.2.3.4')


def test_acquire_release_acquire_cycle(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    """Five back-to-back drop+open cycles. The user's specific verification
    request."""
    fake_protocol_factory()
    for i in range(5):
        with acquire_connection('1.2.3.4') as conn:
            assert conn.is_connected
        # Wait for grace + teardown.
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# Dead-worker eviction
# ---------------------------------------------------------------------------


def test_get_instance_evicts_entry_with_dead_alive_worker(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    """Defensive case: an entry's worker existed, was active, and is now
    dead — but on_died didn't fire (e.g. it was unregistered). The next
    get_instance must detect this and create fresh.

    Constructs the scenario by patching out on_died after the worker is
    already running, then triggering an unexpected exit via SystemExit.
    """
    fake_protocol_factory(fail_loop_after=1, fail_loop_exception=SystemExit)
    inst = ATEMInstanceManager.get_instance('1.2.3.4')
    conn = inst['connection']
    # Disable on_died so the entry stays in the pool when the worker dies.
    conn.set_on_died(None)
    assert conn.connect('1.2.3.4')

    # Wait for the worker to die.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and conn.is_connected:
        time.sleep(0.01)
    assert not conn.is_connected
    assert conn._worker is not None and not conn._worker.is_alive()

    # Defence in depth: get_instance for this IP should evict and create fresh.
    inst2 = ATEMInstanceManager.get_instance('1.2.3.4')
    try:
        assert inst2['connection'] is not conn
        assert inst2['ref_count'] == 1
    finally:
        ATEMInstanceManager.release_instance('1.2.3.4')


def test_on_died_evicts_entry_from_pool(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    """When a worker dies unexpectedly (not via deliberate disconnect),
    on_died should fire and evict the entry from the pool."""
    fake_protocol_factory(fail_loop_after=1, fail_loop_exception=SystemExit)
    inst = ATEMInstanceManager.get_instance('1.2.3.4')
    conn = inst['connection']
    assert conn.connect('1.2.3.4')

    # Wait for the worker to die from the simulated loop() failure.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        with ATEMInstanceManager._instance_lock:
            if '1.2.3.4' not in ATEMInstanceManager._instances:
                break
        time.sleep(0.01)

    with ATEMInstanceManager._instance_lock:
        assert '1.2.3.4' not in ATEMInstanceManager._instances


# ---------------------------------------------------------------------------
# Concurrent acquire / release
# ---------------------------------------------------------------------------


def test_concurrent_get_and_release_no_corruption(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    """N threads each do get / release in a tight loop. Ref count must stay
    consistent (no negative values, no orphan entries)."""
    fake_protocol_factory()
    barrier = threading.Barrier(8)
    errors = []

    def worker():
        try:
            barrier.wait()
            for _ in range(50):
                ATEMInstanceManager.get_instance('1.2.3.4')
                ATEMInstanceManager.release_instance('1.2.3.4')
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # Eventually the entry should be torn down (refcount went to 0 at some
    # point and no one re-acquired). Wait for grace period.
    time.sleep(0.5)
    with ATEMInstanceManager._instance_lock:
        # The entry might or might not still be present (last-release
        # winner gets the timer). Worst case the timer is still pending
        # but the refcount is 0.
        entry = ATEMInstanceManager._instances.get('1.2.3.4')
        if entry is not None:
            assert entry['ref_count'] == 0


# ---------------------------------------------------------------------------
# acquire_connection helper
# ---------------------------------------------------------------------------


def test_acquire_connection_yields_connected(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    fake_protocol_factory()
    with acquire_connection('1.2.3.4') as conn:
        assert conn.is_connected
        assert conn.ip_address == '1.2.3.4'


def test_acquire_connection_releases_on_exception(
    reset_pool, fake_protocol_factory, short_connect_timeout, short_grace,
):
    fake_protocol_factory()
    with pytest.raises(RuntimeError, match='boom'):
        with acquire_connection('1.2.3.4'):
            raise RuntimeError('boom')
    # Ref must be released. Wait for grace teardown.
    time.sleep(0.5)
    with ATEMInstanceManager._instance_lock:
        assert '1.2.3.4' not in ATEMInstanceManager._instances
