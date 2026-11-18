#!/usr/bin/env python3
"""Delete one or all clips off a HyperDeck — test/setup helper for
runs that need a clean slot.

Usage:

    python tools/hyperdeck_delete_clip.py <hyperdeck-ip> [options]

Actions (mutually exclusive):
    --list                List clips on the deck and exit.
    --name <file>         Delete only this clip (case-insensitive match
                          against the filename on disk).
    --all                 Delete every clip on the deck. Confirmation
                          prompt unless --yes is also passed.

Common flags:
    --slot <id>           Force a specific slot dir (e.g. ``1``, ``sd1``).
                          Default: auto-detect first numeric dir, then
                          first sd/ssd/usb/nas mount.
    --yes                 Skip the --all confirmation prompt.
    --no-clear            Don't call ``clips_clear`` on the deck after
                          delete (by default we drop the timeline so the
                          deck doesn't keep a stale playback reference
                          to the deleted clip).

Examples:
    Just list:           hyperdeck_delete_clip.py 192.0.2.20 --list
    One file:            hyperdeck_delete_clip.py 192.0.2.20 --name spin.mp4
    Wipe everything:     hyperdeck_delete_clip.py 192.0.2.20 --all --yes

Pure FTP DELE for file removal; HyperDeck 9993 control protocol's
``clips_clear`` is called afterwards to reset the timeline. No tally
interaction — caller is responsible for confirming the deck isn't on
air before deleting clips on a live setup.
"""
import argparse
import ftplib
import logging
import os
import sys
from typing import List, Tuple

# From a checkout (python tools/<script>.py) the packages beside tools/
# import without an install.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from hyperdeckwire.upload import resolve_slot_dir, DEFAULT_FTP_TIMEOUT  # noqa: E402
from hyperdeckwire.client import Hyperdeck  # noqa: E402

logger = logging.getLogger('hyperdeck_delete_clip')


def _connect_ftp(host: str, timeout: float) -> ftplib.FTP:
    ftp = ftplib.FTP(host, timeout=timeout)
    # L18 FIX (2026-07-06): if the anonymous-login fallback also fails, close
    # the connected control socket instead of leaking it until GC.
    try:
        try:
            ftp.login('', '')
        except ftplib.error_perm:
            ftp.login('anonymous', '')
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass
        raise
    return ftp


def list_clips(host: str, slot, timeout: float) -> Tuple[str, List[Tuple[str, int]]]:
    """Return (slot_dir, [(name, size_bytes), ...])."""
    with _connect_ftp(host, timeout) as ftp:
        slot_dir = resolve_slot_dir(ftp, slot)
        ftp.cwd(slot_dir)
        entries = []
        # MLSD gives us facts (type, size). Fall back to LIST + nlst if not supported.
        try:
            for name, facts in ftp.mlsd():
                if facts.get('type') == 'file':
                    entries.append((name, int(facts.get('size', 0))))
        except (ftplib.error_perm, ftplib.error_proto, AttributeError):
            for name in ftp.nlst():
                # Skip dirs (best effort — bare nlst doesn't tell us type).
                try:
                    ftp.cwd(name)
                    ftp.cwd('..')
                    continue  # it's a dir
                except ftplib.error_perm:
                    entries.append((name, 0))
        return slot_dir, sorted(entries)


def delete_one(host: str, slot, name: str, timeout: float) -> bool:
    """Delete a single file by name (case-insensitive). Return True on success."""
    with _connect_ftp(host, timeout) as ftp:
        slot_dir = resolve_slot_dir(ftp, slot)
        ftp.cwd(slot_dir)
        # Resolve target with case-insensitive match against the actual listing.
        all_names = ftp.nlst()
        target = next((n for n in all_names if n.lower() == name.lower()), None)
        if target is None:
            print(f'  not found on deck (in /{slot_dir}/): {name}')
            return False
        try:
            ftp.delete(target)
            print(f'  deleted: {target}')
            return True
        except ftplib.all_errors as e:
            print(f'  FAILED to delete {target}: {e}')
            return False


def delete_all(host: str, slot, timeout: float) -> Tuple[int, int]:
    """Delete every file in the resolved slot dir. Returns (deleted, failed)."""
    slot_dir, entries = list_clips(host, slot, timeout)
    if not entries:
        print(f'  nothing to delete in /{slot_dir}/')
        return 0, 0
    deleted = failed = 0
    with _connect_ftp(host, timeout) as ftp:
        ftp.cwd(slot_dir)
        for name, _size in entries:
            try:
                ftp.delete(name)
                print(f'  deleted: {name}')
                deleted += 1
            except ftplib.all_errors as e:
                print(f'  FAILED: {name}: {e}')
                failed += 1
    return deleted, failed


def clear_timeline(host: str) -> None:
    """Drop the deck's playback timeline so it doesn't keep a stale ref
    to a clip we just removed. Best-effort — log and continue on error."""
    try:
        with Hyperdeck(host) as hd:
            hd.clips_clear()
        print('  timeline cleared (clips_clear OK)')
    except Exception as e:
        print(f'  warning: clips_clear failed ({e}) — '
              f'reconnect to the deck if it shows a stale clip')


def main():
    ap = argparse.ArgumentParser(
        description='Delete clip(s) off a HyperDeck via FTP.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument('host', help='HyperDeck IP or hostname')
    action = ap.add_mutually_exclusive_group(required=True)
    action.add_argument('--list', action='store_true', help='List clips and exit')
    action.add_argument('--name', help='Delete this clip name (case-insensitive)')
    action.add_argument('--all', action='store_true',
                        dest='delete_all', help='Delete every clip on the deck')
    ap.add_argument('--slot', default=None,
                    help='Force a specific slot dir (int or name like sd1)')
    ap.add_argument('--yes', action='store_true',
                    help="Skip the --all confirmation prompt")
    ap.add_argument('--no-clear', action='store_true',
                    help="Don't call clips_clear after delete")
    ap.add_argument('--timeout', type=float, default=DEFAULT_FTP_TIMEOUT,
                    help='FTP socket timeout in seconds')
    args = ap.parse_args()

    # Coerce slot to int when it looks like one (matches upload_clip's API).
    slot_arg = args.slot
    if slot_arg is not None and slot_arg.isdigit():
        slot_arg = int(slot_arg)

    print(f'Connecting to HyperDeck at {args.host}…')
    if args.list:
        slot_dir, entries = list_clips(args.host, slot_arg, args.timeout)
        print(f'Slot dir: /{slot_dir}/')
        if not entries:
            print('  (no clips)')
            return 0
        for name, size in entries:
            print(f'  {name}  ({size:>12,} bytes)')
        return 0

    if args.delete_all:
        slot_dir, entries = list_clips(args.host, slot_arg, args.timeout)
        if not entries:
            print(f'No clips in /{slot_dir}/. Nothing to do.')
            return 0
        print(f'About to delete {len(entries)} clip(s) from /{slot_dir}/:')
        for name, _ in entries:
            print(f'  - {name}')
        if not args.yes:
            confirm = input('Type YES to confirm: ').strip()
            if confirm != 'YES':
                print('Aborted.')
                return 1
        deleted, failed = delete_all(args.host, slot_arg, args.timeout)
        print(f'Done — {deleted} deleted, {failed} failed.')
        if not args.no_clear and deleted:
            clear_timeline(args.host)
        return 0 if failed == 0 else 1

    # --name path
    ok = delete_one(args.host, slot_arg, args.name, args.timeout)
    if ok and not args.no_clear:
        clear_timeline(args.host)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
