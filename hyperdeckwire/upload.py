# SPDX-License-Identifier: MIT
"""hyperdeckwire.upload — FTP file upload to a HyperDeck Studio HD-class unit.

The HTTP REST API for clip upload landed in firmware 8.x but only on
the Plus/Pro/HDR/Shuttle SKUs — the Studio HD Mini doesn't have it
(verified against firmware 8.1.1, port 80 closed). Every networked
HyperDeck has FTP though, so that's the path this module takes.

Wire details:
  - Anonymous FTP (no auth required on stock firmware).
  - Top-level dirs are storage volumes. Studio HD Mini names them
    numerically per slot id (``/1/``, ``/2/``, ``/3/``); other models
    (Studio HD, 4K) name them by medium (``sd1``, ``ssd1``, ``usb``).
    Default behaviour: auto-detect a storage volume at root (numeric
    first, else a known media prefix, SD preferred) and CWD into it.
  - STOR at root is rejected (550 file unavailable); STOR inside a
    slot dir works. The auto-detect handles that automatically.
  - The Hyperdeck filesystem index updates live: after a successful
    STOR the new clip is immediately visible via the 9993 ``disk list``
    command, no rescan or slot-reselect required.
"""

from __future__ import annotations

import ftplib
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_FTP_TIMEOUT = 120.0   # large clips can take a minute+

# How often ``should_cancel`` is asked during a transfer. A chunk is 8 KiB, so
# asking per chunk would mean ~170,000 calls for a 1.4 GB clip.
CANCEL_POLL_SECONDS = 1.0

# How long to wait for the deck's reply to a stopped STOR, and for the DELE
# that removes the partial file, before giving up on the cleanup.
CANCEL_CLEANUP_TIMEOUT = 10.0


@dataclass(frozen=True)
class UploadResult:
    """Successful upload outcome — returned by :func:`upload_clip`."""

    name: str               # the remote filename as written (basename of source)
    size: int               # bytes written
    duration_seconds: float # wall-clock duration of the STOR
    slot_dir: str           # FTP working directory used (e.g. "1")

    @property
    def throughput_mb_s(self) -> float:
        return self.size / max(self.duration_seconds, 0.001) / 1e6


class UploadCancelled(Exception):
    """``should_cancel`` returned True during :func:`upload_clip`.

    The transfer was stopped and the partial file deleted from the deck.
    ``partial_removed`` is False only when the deck still lists the file
    afterwards (or could not be asked), so the caller can say so."""

    def __init__(self, name: str, bytes_sent: int, partial_removed: bool):
        self.name = name
        self.bytes_sent = bytes_sent
        self.partial_removed = partial_removed
        left = '' if partial_removed else '; the partial file is still on the deck'
        super().__init__(f'upload of {name} cancelled after {bytes_sent:,} bytes{left}')


class _Stop(Exception):
    """Raised from the chunk callback to end ``storbinary`` early."""


def upload_clip(host: str,
                local_path: str,
                *,
                slot: Optional[int] = None,
                login_user: str = '',
                login_pass: str = '',
                timeout: float = DEFAULT_FTP_TIMEOUT,
                progress_callback=None,
                should_cancel=None) -> UploadResult:
    """Upload ``local_path`` to a HyperDeck at ``host`` over FTP.

    :param host: HyperDeck IP or hostname.
    :param local_path: absolute path to the source file. Basename
        becomes the remote name (preserved as-is, spaces and all —
        the Hyperdeck handles those fine on both FTP and 9993 sides).
    :param slot: numeric slot id to upload into. ``None`` (default)
        auto-detects the first numeric subdirectory at the FTP root.
    :param login_user / login_pass: FTP credentials. Empty defaults
        attempt anonymous login (works on stock firmware); the
        anonymous-with-empty-fields retry handles servers that reject
        the bare ``USER`` form.
    :param timeout: FTP socket timeout in seconds.
    :param progress_callback: optional callable taking the running
        byte count; called after each chunk for big-file UX.
    :param should_cancel: optional callable returning True to stop the
        transfer. Asked about once every ``CANCEL_POLL_SECONDS`` while the
        file is sent (an exception from it counts as False). On True the
        data connection is closed, the partial file is deleted from the deck
        and :class:`UploadCancelled` is raised.

    :raises FileNotFoundError: ``local_path`` doesn't exist.
    :raises UploadCancelled: ``should_cancel`` returned True.
    :raises ftplib.all_errors: anything FTP-level (login, CWD, STOR).
    """
    if not os.path.exists(local_path):
        raise FileNotFoundError(local_path)

    size = os.path.getsize(local_path)
    name = os.path.basename(local_path)
    logger.info('hyperdeckwire: starting upload %s -> %s:21 (%d bytes)',
                local_path, host, size)

    t0 = time.monotonic()
    with ftplib.FTP(host, timeout=timeout) as ftp:
        try:
            ftp.login(login_user, login_pass)
        except ftplib.error_perm:
            # Some FTP servers reject the bare anonymous form; retry
            # with the canonical ``anonymous`` user.
            ftp.login('anonymous', '')

        slot_dir = _resolve_slot_dir(ftp, slot)
        ftp.cwd(slot_dir)

        bytes_sent = [0]
        last_asked = [time.monotonic()]

        def _on_chunk(chunk: bytes) -> None:
            bytes_sent[0] += len(chunk)
            if progress_callback is not None:
                try:
                    progress_callback(bytes_sent[0])
                except Exception:
                    logger.exception('upload progress_callback raised')
            if should_cancel is None:
                return
            now = time.monotonic()
            if now - last_asked[0] < CANCEL_POLL_SECONDS:
                return
            last_asked[0] = now
            try:
                stop = should_cancel()
            except Exception:
                logger.exception('upload should_cancel raised')
                stop = False
            if stop:
                raise _Stop

        with open(local_path, 'rb') as f:
            try:
                ftp.storbinary(f'STOR {name}', f, callback=_on_chunk)
            except _Stop:
                removed = _discard_partial(ftp, name)
                logger.info('hyperdeckwire: upload of %s cancelled after %d bytes '
                            '(partial removed: %s)', name, bytes_sent[0], removed)
                raise UploadCancelled(name, bytes_sent[0], removed) from None

    dt = time.monotonic() - t0
    result = UploadResult(name=name, size=size,
                          duration_seconds=dt, slot_dir=slot_dir)
    logger.info('hyperdeckwire: upload OK %s (%.1fs, %.1f MB/s)',
                name, dt, result.throughput_mb_s)
    return result


# Storage-volume name prefixes HyperDeck firmware presents at FTP root, in
# preference order. Studio HD Mini exposes numeric dirs (``/1/``, ``/2/``);
# other models (e.g. Studio HD, 4K) expose named mounts like ``sd1`` /
# ``ssd1`` / ``usb``. SD is preferred since that's the usual record/playback
# medium. ``System Volume Information`` / ``.Trashes`` are never matched.
_MEDIA_DIR_PREFIXES = ('sd', 'ssd', 'usb', 'nas')


def _discard_partial(ftp: ftplib.FTP, name: str) -> bool:
    """After a STOR was stopped (``storbinary`` has closed the data
    connection): read the deck's reply to the stopped transfer, delete the
    partial file, and return True when the deck no longer lists it."""
    try:
        if ftp.sock is not None:
            ftp.sock.settimeout(CANCEL_CLEANUP_TIMEOUT)
        try:
            ftp.getresp()          # 426 / 451 for the stopped STOR, or 226
        except ftplib.all_errors:
            pass
        try:
            ftp.delete(name)
        except ftplib.error_perm:
            pass                   # 550: the deck already dropped it
        return name not in ftp.nlst()
    except ftplib.all_errors:
        logger.exception('hyperdeckwire: could not remove the partial %s', name)
        return False


def _resolve_slot_dir(ftp: ftplib.FTP, slot=None) -> str:
    """Return the slot dir to CWD into. A caller-supplied ``slot`` wins
    (an int slot id like ``2`` or a literal dir name like ``'sd1'``);
    otherwise list root and pick a storage volume.

    Numeric slot dirs win when present (legacy firmware). Otherwise the
    first directory matching a known media prefix (sd/ssd/usb/nas) is
    used, so newer models that name their mounts (``['sd1', 'usb']``)
    work too."""
    if slot is not None:
        return str(slot)
    entries = ftp.nlst()
    numeric = sorted([e for e in entries if e.isdigit()], key=int)
    if numeric:
        return numeric[0]
    for prefix in _MEDIA_DIR_PREFIXES:
        named = sorted([e for e in entries if e.lower().startswith(prefix)])
        if named:
            return named[0]
    raise OSError(
        f'no slot dir found at FTP root (entries: {entries!r})')
