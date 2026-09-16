# SPDX-License-Identifier: LGPL-3.0-only
"""
Wait for a atemwire connection to complete its initial state dump.

"Ready" means: atemwire has fired the 'connected' event (which happens at
the END of the initial state dump — see protocol.py) AND 'video-mode' is
present in mixerstate. Almost every caller in this codebase used to open
its own socket, call protocol.connect(), then spin on this same two-part
condition with slightly different timeouts and sleep intervals.

The caller is responsible for calling ``protocol.connect()`` before
invoking ``wait_ready()``. This helper only waits; it does not open the
connection.
"""

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class WaitAborted(Exception):
    """Raised by ``wait_ready()`` when ``stop_event`` fires before the
    connection becomes ready."""


def wait_ready(
    protocol,
    *,
    timeout: float = 8.0,
    pump_interval: float = 0.01,
    extra_settle: float = 0.0,
    stop_event: Optional[threading.Event] = None,
) -> dict:
    """Block until ``protocol`` has completed the ATEM's initial state dump.

    Pumps ``protocol.loop()`` at ``pump_interval`` until the switcher has
    finished its initial state dump (``protocol.initialized`` — set when the
    ``InCm`` end-of-dump marker has been processed, the same moment the
    'connected' event fires) AND ``'video-mode'`` is present in
    ``mixerstate``, then
    optionally keeps pumping for ``extra_settle`` seconds to let late-
    arriving state fields (e.g. macro-properties) populate.

    :param protocol: an ``AtemProtocol`` whose ``connect()`` has been called.
    :param timeout: overall wait budget in seconds.
    :param pump_interval: sleep between ``protocol.loop()`` calls. Pass 0
        to busy-loop (the media pool watcher does this during connect).
    :param extra_settle: additional pumping seconds AFTER ready.
    :param stop_event: optional cancellation signal.

    :returns: ``protocol.mixerstate`` on success.
    :raises TimeoutError: if not ready within ``timeout`` seconds.
    :raises WaitAborted: if ``stop_event`` is set before ready.
    """
    # Fast path: initial state already dumped. ``protocol.connected`` is
    # NOT the test — it flips on the first data packet of the dump, so it
    # used to make this return mid-dump (with a partial mixerstate, and
    # with any lock request sent right then racing the dump's tail).
    if getattr(protocol, 'initialized', False) and 'video-mode' in protocol.mixerstate:
        if extra_settle > 0:
            _pump_for(protocol, extra_settle, pump_interval, stop_event)
        return protocol.mixerstate

    connected = {'flag': False}

    def _on_connected():
        connected['flag'] = True

    handler_id = protocol.on('connected', _on_connected)

    # The handler must come off on EVERY exit path — wait_ready runs per
    # connect attempt, and a leaked closure per attempt accumulates on
    # long-lived protocols (2026-07-06).
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                raise WaitAborted("stop_event set before ready")
            try:
                protocol.loop()
            except Exception as e:
                logger.debug("wait_ready: protocol.loop() error: %s", e)
            if (
                (connected['flag'] or getattr(protocol, 'initialized', False))
                and 'video-mode' in protocol.mixerstate
            ):
                break
            time.sleep(pump_interval)
        else:
            raise TimeoutError(f"wait_ready: not ready within {timeout}s")
    finally:
        try:
            protocol.off('connected', handler_id)
        except Exception:
            pass

    if extra_settle > 0:
        _pump_for(protocol, extra_settle, pump_interval, stop_event)

    return protocol.mixerstate


def _pump_for(
    protocol,
    duration: float,
    pump_interval: float,
    stop_event: Optional[threading.Event],
) -> None:
    """Keep calling ``protocol.loop()`` for ``duration`` seconds."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            protocol.loop()
        except Exception as e:
            logger.debug("_pump_for: protocol.loop() error: %s", e)
        time.sleep(pump_interval)


def wait_state_settled(atem, timeout: float = 3.0) -> None:
    """Wait for the ATEM's initial state dump to look COMPLETE (a stronger
    condition than ``wait_ready``).

    ``wait_ready`` returns when ``video-mode`` arrives — but the state dump
    continues for another ~1s with the per-feature packets
    (program-bus-input, aux-output-source, transition-mix, key-on-air,
    fairlight-master-properties, ...). Callers that snapshot or reconcile
    against the full state (profile save, HyperDeck binding sync) need
    them all populated.

    Strategy: poll mixerstate keys until either (a) a small set of
    indicator keys all appear or (b) the key count has stopped growing
    for ~500ms. Cap at ``timeout`` seconds.

    Accepts an ``ATEM`` facade, ``ATEMConnection``, or ``AtemProtocol``
    (anything exposing ``mixerstate``, directly or via ``.raw``).
    """
    if hasattr(atem, 'mixerstate'):
        mx = atem.mixerstate
    elif hasattr(atem, 'raw') and hasattr(atem.raw, 'mixerstate'):
        mx = atem.raw.mixerstate
    else:
        raise TypeError(
            f"wait_state_settled expected ATEM/ATEMConnection/AtemProtocol, "
            f"got {type(atem).__name__}")

    indicators = (
        'program-bus-input', 'preview-bus-input', 'transition-settings',
        'aux-output-source', 'key-on-air', 'transition-mix',
        # Fairlight packets arrive late in the dump on Constellation HD —
        # without these the master-out and per-strip sections of a profile
        # come out empty. The strip properties dict can stay empty (no
        # strips configured), but the audio-input dict at least lists the
        # available sources, and the master-properties packet is always
        # present once Fairlight is up.
        'fairlight-audio-input', 'fairlight-master-properties',
    )
    deadline = time.monotonic() + timeout
    last_count = len(mx)
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        if all(k in mx for k in indicators):
            return
        time.sleep(0.05)
        cur = len(mx)
        if cur != last_count:
            last_count = cur
            last_change = time.monotonic()
        elif time.monotonic() - last_change > 0.5:
            # No new keys for 500ms — call the dump settled.
            return
