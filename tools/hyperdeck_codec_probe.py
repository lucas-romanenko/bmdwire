#!/usr/bin/env python3
"""Probe a HyperDeck for its configured codec and the clips on disk.

Answers two questions about a networked HyperDeck:
  1. What codec / file format is the deck currently set to (the
     ``file format`` configuration value, e.g. ``DNxHD220x``).
  2. The full file name of every clip on the active disk.

Read-only: connects over the 9993 control protocol, sends ``configuration``
and ``disk list``, prints, disconnects. Makes no transport or config changes.

Usage:

    python tools/hyperdeck_codec_probe.py <hyperdeck-ip> [--slot N] [--full]

``--slot`` lists a specific
slot instead of the active one. ``--full`` also dumps the complete
configuration block.
"""
import argparse
import os
import sys

# From a checkout (python tools/<script>.py) the packages beside tools/
# import without an install.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from hyperdeckwire import Hyperdeck, HyperdeckError  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('ip', help='HyperDeck IP')
    p.add_argument('--slot', type=int, default=None,
                   help='Slot id to list (default: active slot)')
    p.add_argument('--full', action='store_true',
                   help='Also print the full configuration block')
    args = p.parse_args(argv)

    try:
        with Hyperdeck(args.ip) as hd:
            cfg = hd.configuration() or {}
            clips = hd.disk_list(slot_id=args.slot)
    except HyperdeckError as e:
        print(f'ERROR: HyperDeck at {args.ip} returned an error: {e}',
              file=sys.stderr)
        return 2
    except OSError as e:
        print(f'ERROR: could not reach HyperDeck at {args.ip}: {e}',
              file=sys.stderr)
        return 2

    file_format = (cfg.get('file format') or '<unknown>').strip()
    standard = (cfg.get('default standard')
                or cfg.get('video format') or '').strip()

    print(f'HyperDeck {args.ip}')
    print(f'  Codec (file format) : {file_format}')
    if standard:
        print(f'  Video standard      : {standard}')

    slot_note = '' if args.slot is None else f' (slot {args.slot})'
    print(f'  Clips on disk{slot_note}: {len(clips)}')
    if not clips:
        print('    (none)')
    for c in clips:
        fmt = ' / '.join(x for x in (c.file_format, c.video_format) if x)
        fmt_note = f'  [{fmt}]' if fmt else ''
        dur_note = f'  ({c.duration})' if c.duration else ''
        print(f'    #{c.clip_id}: {c.name}{dur_note}{fmt_note}')

    if args.full:
        print('  Full configuration:')
        for k in sorted(cfg):
            print(f'    {k}: {cfg[k]}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
