# SPDX-License-Identifier: LGPL-3.0-only
"""Unit tests for atemwire.connection.ATEMConnection.

Focus: the threading contract and the dead-worker detection.

The historical bug these tests defend against:

  * The worker thread parks on ``thread_recv_queue.get()`` — a blocking
    call with no timeout. ``_stop_event.set()`` does NOT wake it.
    ``worker.join(timeout=2.0)`` exhausts; the worker is orphaned.
  * Callers calling ``send()`` on a connection whose worker is dead
    silently queue commands that nobody drains. This used to manifest
    as 10-second capture timeouts whose error message was "ATEM did
    not populate a new media pool slot. Is the media pool full?" — a
    completely misleading diagnosis.

The fix verified here:

  * ``_disconnect_locked`` puts None on ``thread_recv_queue`` to wake
    the worker BEFORE joining. Disconnect completes in milliseconds.
  * ``is_connected`` reflects worker liveness (worker.is_alive() AND
    handshake_done).
  * ``send()`` raises ``ConnectionDeadError`` instead of silently
    queuing.
  * The ``on_died`` callback fires once on unexpected worker exit.
"""

import time

import pytest

from atemwire.connection import (
    ATEMConnection,
    ConnectionDeadError,
)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_freshly_constructed_is_not_connected(reset_pool, fake_protocol_factory):
    fake_protocol_factory()
    conn = ATEMConnection('atem_test')
    assert conn.is_connected is False
    assert conn.ip_address is None
    assert conn._worker is None


# ---------------------------------------------------------------------------
# Happy-path connect / disconnect
# ---------------------------------------------------------------------------


def test_connect_succeeds_and_is_connected(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    fake_protocol_factory()
    conn = ATEMConnection('atem_test')
    ok = conn.connect('1.2.3.4')
    try:
        assert ok is True
        assert conn.is_connected is True
        assert conn.ip_address == '1.2.3.4'
        assert conn._worker is not None
        assert conn._worker.is_alive()
    finally:
        conn.disconnect()


def test_disconnect_completes_quickly_after_connect(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    """The wake-then-join fix: disconnect completes in much less than the
    DISCONNECT_JOIN_TIMEOUT budget. Historically this took 2.0s because the
    join exhausted; with the fix it should be sub-100ms."""
    fake_protocol_factory()
    conn = ATEMConnection('atem_test')
    assert conn.connect('1.2.3.4')

    t0 = time.monotonic()
    conn.disconnect()
    elapsed = time.monotonic() - t0

    # Generous bound — real should be ~5-30ms.
    assert elapsed < 0.3, (
        f"disconnect took {elapsed:.3f}s — fix is regressed; the worker "
        f"is not being woken before join"
    )
    assert conn.is_connected is False


def test_disconnect_is_idempotent(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    fake_protocol_factory()
    conn = ATEMConnection('atem_test')
    assert conn.connect('1.2.3.4')
    conn.disconnect()
    conn.disconnect()  # second call must not raise
    assert conn.is_connected is False


def test_reconnect_to_same_ip_returns_quickly(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    fake_protocol_factory()
    conn = ATEMConnection('atem_test')
    assert conn.connect('1.2.3.4')
    t0 = time.monotonic()
    assert conn.connect('1.2.3.4') is True  # warm path
    elapsed = time.monotonic() - t0
    try:
        # Should be near-instant, not start a new worker.
        assert elapsed < 0.05
    finally:
        conn.disconnect()


def test_reconnect_to_different_ip_swaps_workers(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    fake_protocol_factory()
    conn = ATEMConnection('atem_test')
    assert conn.connect('1.2.3.4')
    first_worker = conn._worker

    assert conn.connect('5.6.7.8')
    second_worker = conn._worker
    try:
        assert second_worker is not first_worker
        assert not first_worker.is_alive()
        assert second_worker.is_alive()
        assert conn.ip_address == '5.6.7.8'
    finally:
        conn.disconnect()


# ---------------------------------------------------------------------------
# Dead-worker detection
# ---------------------------------------------------------------------------


def test_send_raises_when_not_connected(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    fake_protocol_factory()
    conn = ATEMConnection('atem_test')
    # Never connected.
    with pytest.raises(ConnectionDeadError):
        conn.send(_FakeCommand())


def test_send_raises_after_disconnect(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    fake_protocol_factory()
    conn = ATEMConnection('atem_test')
    assert conn.connect('1.2.3.4')
    conn.disconnect()
    with pytest.raises(ConnectionDeadError):
        conn.send(_FakeCommand())


def test_is_connected_flips_to_false_when_worker_dies(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    """Simulate the worker dying for an unrelated reason (e.g. an
    exception escaping the inner catch). ``is_connected`` must reflect
    this without anyone calling disconnect()."""
    # SystemExit is a BaseException, escapes _pump_loop_once's
    # `except Exception` and exits the worker via the outer except.
    #
    # TEST-1 de-flake: passing fail_loop_after=0 up front raced the handshake —
    # the worker's first loop() pump could raise BEFORE connect() read
    # is_connected, so connect() itself intermittently returned False. Instead
    # connect CLEANLY, then inject the failure: with loop_blocks=True (default)
    # the worker parks in loop()'s queue.get() after connecting, so we set the
    # failure and wake it (a non-None packet) to make its next loop() raise —
    # fully deterministic.
    instances = fake_protocol_factory(fail_loop_exception=SystemExit)
    conn = ATEMConnection('atem_test')
    assert conn.connect('1.2.3.4')

    fake = instances[-1]
    fake._fail_loop_after = 0                        # next loop() pump raises
    fake.transport.thread_recv_queue.put(object())   # wake the parked worker

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not conn.is_connected:
            break
        time.sleep(0.01)
    try:
        assert conn.is_connected is False, (
            "worker should have died and is_connected should reflect it"
        )
    finally:
        conn.disconnect()


def test_send_raises_after_worker_dies_unexpectedly(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    """The end-to-end contract: if the worker dies for any reason and a
    caller tries to send, they get a clear error — NOT a silent queue
    accumulation that leads to a 10s timeout downstream.

    Use fail_loop_after=1 so the worker survives long enough for connect()
    to return True (otherwise we'd race connect() against worker exit and
    connect() might see the post-death _handshake_done=False).
    """
    fake_protocol_factory(fail_loop_after=1, fail_loop_exception=SystemExit)
    conn = ATEMConnection('atem_test')
    assert conn.connect('1.2.3.4')

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and conn.is_connected:
        time.sleep(0.01)
    assert not conn.is_connected

    with pytest.raises(ConnectionDeadError):
        conn.send(_FakeCommand())


def test_on_died_fires_for_unexpected_exit(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    fake_protocol_factory(fail_loop_after=1, fail_loop_exception=SystemExit)
    conn = ATEMConnection('atem_test')
    died_calls = []
    conn.set_on_died(lambda c: died_calls.append(c))

    assert conn.connect('1.2.3.4')

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not died_calls:
        time.sleep(0.01)

    try:
        assert len(died_calls) == 1
        assert died_calls[0] is conn
    finally:
        conn.disconnect()


def test_send_wakes_worker_so_cmd_is_drained_promptly(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    """The ``send()`` → wake-worker contract.

    Historical bug: ``send()`` only put the command on ``_cmd_queue`` and
    relied on the next ``protocol.loop()`` return to trigger
    ``_drain_cmd_queue``. But ``loop()`` blocks indefinitely inside
    ``transport.receive_packet`` while ATEM is sending only PINGs
    (length-12 packets that never break out of the inner ``while True``).
    So a queued ``CaptureStillCommand`` could sit unsent for tens of
    seconds — observed as "Phase 1 timeout" because ATEM never received
    the ``Capt`` we thought we sent.

    Fix: ``send()`` now also puts a ``Wakeup`` sentinel on
    ``thread_recv_queue`` so ``loop()`` returns within microseconds and
    ``_drain_cmd_queue()`` ships the command immediately.
    """
    instances = fake_protocol_factory()
    conn = ATEMConnection('atem_test')
    assert conn.connect('1.2.3.4')
    fake = instances[-1]

    # Drain the 'CONNECTED_PACKET' that connect() puts on the queue so the
    # worker is now genuinely parked on .get() — i.e. mirroring the real
    # "ATEM is sending only PINGs, nothing data-bearing" state.
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if fake.transport.thread_recv_queue.empty():
            break
        time.sleep(0.005)

    # Capture the baseline before send().
    baseline_count = len(fake.send_log)

    cmd = _FakeCommand()
    t0 = time.monotonic()
    conn.send(cmd)

    # The wake should make the worker return from loop(), then drain the
    # cmd queue, then call send_commands. All in well under 100ms even on
    # slow CI. Without the fix, this test hangs until the worker dies via
    # disconnect at end-of-test.
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if len(fake.send_log) > baseline_count:
            break
        time.sleep(0.005)
    elapsed = time.monotonic() - t0

    try:
        assert len(fake.send_log) > baseline_count, (
            "send() did not wake the worker — cmd_queue is not being drained "
            "while loop() is parked on thread_recv_queue"
        )
        sent = fake.send_log[-1][0]
        assert sent is cmd
        assert elapsed < 0.3, (
            f"send() → drain took {elapsed:.3f}s — wake mechanism is regressed"
        )
    finally:
        conn.disconnect()


def test_on_died_does_NOT_fire_for_deliberate_disconnect(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    """Disconnect via .disconnect() is not "unexpected". The callback must
    not fire — pool would otherwise evict an entry it just deliberately
    tore down."""
    fake_protocol_factory()
    conn = ATEMConnection('atem_test')
    died_calls = []
    conn.set_on_died(lambda c: died_calls.append(c))

    assert conn.connect('1.2.3.4')
    conn.disconnect()

    # Give the worker a chance to fire on_died (it shouldn't).
    time.sleep(0.1)
    assert died_calls == []


def test_on_died_fires_at_most_once(
    reset_pool, fake_protocol_factory, short_connect_timeout,
):
    fake_protocol_factory(fail_loop_after=1, fail_loop_exception=SystemExit)
    conn = ATEMConnection('atem_test')
    died_calls = []
    conn.set_on_died(lambda c: died_calls.append(c))

    assert conn.connect('1.2.3.4')

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not died_calls:
        time.sleep(0.01)

    try:
        time.sleep(0.1)  # extra time for any second fire to happen
        assert len(died_calls) == 1
    finally:
        conn.disconnect()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeCommand:
    """Stand-in for a atemwire.command.Command. send_commands() doesn't
    actually serialize — we just need an object."""

    def get_command(self):
        return b''
