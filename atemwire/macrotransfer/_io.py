# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""Macro file-transfer I/O — download from + upload to the ATEM's macro store.

Two notable wire-format quirks distinguish the macro store from the
still store:

1. **No locking.** A still download starts with PLCK and waits for
   LKOB; a macro download skips both and goes straight to FTSU. This
   matches what ATEM Software Control does — its Wireshark capture for
   "Save As → Macros only" shows only FTSU/FTUA from client to switcher.

2. **Mode bits in FTSD** (upload only): the still store accepts mode
   0x0001 (Write RLE), the macro store requires 0x0300 (Write
   uncompressed + Pre-erase). The wrong mode is rejected with FTDE
   code 2 ("not-found"). Driven by upstream
   ``protocol._transfer_trigger`` based on store id.

Both directions ride the native file-transfer state machine
(``protocol.download`` / ``protocol.upload``) — transfers interleave
with control traffic on the connection's normal packet loop (the old
direct-socket takeover is gone, ASC-style step 6, 2026-07-02). The
FTDC handler RLE-decodes store 0 only, so macro downloads arrive as
the raw bytes ``decode_macro_bytecode`` expects.
"""

import logging
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


# Wire constant. ``store=0xffff`` is the macro-store identifier the
# ATEM expects for FTSU/FTSD; ``TransferDownloadRequestCommand``
# already special-cases this to set the trailing magic byte to 0x03
# so the packet matches what Software Control sends.
MACRO_STORE = 0xFFFF


def download_macro_bytecode(
    protocol,
    slot: int,
    *,
    timeout: float = 10.0,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> bytes:
    """Download raw macro bytecode for ``slot`` via the native transfer
    machinery, PUMPING ``protocol.loop()`` on the calling thread until the
    transfer completes.

    For BARE-protocol callers only (dev tools, smoke scripts — contexts
    where nothing else pumps the loop). On a pooled/worker-pumped
    connection use ``ATEMConnection.download_macro`` instead: pumping here
    would race the worker thread on the receive queue.

    Returns empty bytes for slots the ATEM reports as empty (FTDC
    immediately after FTSU, no FTDa).

    :raises RuntimeError: protocol not connected.
    :raises TimeoutError: transfer didn't complete within ``timeout``.
    """
    if protocol is None or not protocol.connected:
        raise RuntimeError("protocol not connected")

    done: List[bytes] = []

    def _on_done(store, slot_idx, data):
        if store == MACRO_STORE and slot_idx == slot:
            done.append(data)

    def _on_progress(store, slot_idx, fraction):
        if progress_callback is not None and store == MACRO_STORE and slot_idx == slot:
            try:
                # Historical callback shape was (bytes_so_far, chunks) —
                # the native machinery reports a fraction; callers only
                # use it for logging cadence.
                progress_callback(int(fraction * 100), 0)
            except Exception:
                pass

    done_id = protocol.on('download-done', _on_done)
    prog_id = protocol.on('transfer-progress', _on_progress)
    try:
        protocol.download(MACRO_STORE, slot)
        deadline = time.monotonic() + timeout
        while not done and time.monotonic() < deadline:
            protocol.loop()
        if not done:
            try:
                protocol.abort_transfers()
            except Exception:
                pass
            raise TimeoutError(f"Macro download timed out (slot={slot})")
        return done[0]
    finally:
        try:
            protocol.off('download-done', done_id)
            protocol.off('transfer-progress', prog_id)
        except Exception:
            pass


def upload_macro_bytecode(
    protocol,
    slot: int,
    name: str,
    description: str,
    bytecode: bytes,
    *,
    timeout: float = 10.0,
) -> None:
    """Upload macro ``bytecode`` to ``slot`` with ``name`` and
    ``description`` via upstream's file-transfer state machine.

    Wire flow (driven by ``protocol.upload`` → ``_transfer_trigger`` →
    ``_queue_chunks`` → ``_queue_flushed``):

      client → ATEM:  FTSD (start: store=0xFFFF, slot, len, mode=0x0300)
      ATEM   → client: FTCD (chunk size, count)
      client → ATEM:  FTDa (one or more chunks of bytecode)
      ATEM   → client: FTUA (one ack per chunk)
      client → ATEM:  FTFD (name + description + MD5 of bytecode)
      ATEM   → client: FTDC (transfer complete)

    Mode bits in FTSD (per OpenSwitcher / upstream
    ``TransferUploadRequestCommand``): bit 0 = Write RLE, bit 1 = Clear,
    bit 8 = Write uncompressed, bit 9 = Pre-erase. The macro store
    requires ``0x0300`` (uncompressed + pre-erase) — what Software
    Control's "Restore from State" sends, verified empirically against
    a Wireshark capture of it. ``0x0001`` (Write RLE), which the still
    store accepts, is rejected by the macro store with ``FTDE`` code 2
    ("not-found"). The mode is selected by upstream
    ``protocol._transfer_trigger`` based on store id.

    The macro store does NOT require explicit PLCK/LKOB locking before
    upload (verified by the same capture, which goes straight to FTSD).
    This is the asymmetry with the still store (which does require
    locks).

    No live ATEM state changes during the transfer — the bytecode lands
    directly in the macro slot.

    Caller contract: someone must be pumping ``protocol.loop()`` while
    this function blocks. In production that's the ``ATEMConnection``
    worker thread; in tests/scripts that's a manual loop on the calling
    thread. This function does NOT pump the loop itself, because pumping
    would race with the worker when one exists.

    :param protocol: a connected ``AtemProtocol``.
    :param slot: macro slot index, 0-based.
    :param name: macro name (utf-8, truncated to 63 bytes + NUL).
    :param description: description (utf-8, truncated to 127 bytes + NUL).
    :param bytecode: the encoded ops payload from
        ``encode_macro_bytecode``.
    :param timeout: max wait for the upload-done callback.

    :raises RuntimeError: protocol not connected or ATEM error.
    :raises TimeoutError: upload didn't complete within ``timeout``.
    """
    if protocol is None or not protocol.connected:
        raise RuntimeError("protocol not connected")

    done = threading.Event()
    result = {'error': None}

    def _on_done(store, slt):
        if store == MACRO_STORE and slt == slot:
            done.set()

    def _on_error(field):
        # Whichever transfer is active right now — the next-in-queue
        # is the one we just submitted (transfers are serialised in
        # the queue, so this is unambiguous in practice).
        result['error'] = field
        done.set()

    done_id = protocol.on('upload-done', _on_done)
    err_id = protocol.on('file-transfer-error', _on_error)
    try:
        protocol.upload(MACRO_STORE, slot, bytecode,
                        compress=False,
                        name=name, description=description)
        if not done.wait(timeout):
            raise TimeoutError(
                f"upload slot={slot}: no upload-done within {timeout}s")
        if result['error'] is not None:
            err = result['error']
            status = getattr(err, 'status', '?')
            transfer = getattr(err, 'transfer', '?')
            raise RuntimeError(
                f"ATEM rejected macro upload (transfer={transfer}, "
                f"status={status})")
    finally:
        protocol.off('upload-done', done_id)
        protocol.off('file-transfer-error', err_id)
