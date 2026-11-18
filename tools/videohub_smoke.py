"""Read-only Videohub smoke test.

Connects, parses the state preamble, and prints a summary — device
info, labels, routing, locks. READ-ONLY by default: safe to run against
a hub with live destinations (a state read is what every BMD panel does
on connect).

Usage:
    python tools/videohub_smoke.py <ip>
    python tools/videohub_smoke.py <ip> --route DEST SRC --confirm

The optional --route test changes REAL routing on the hub and therefore
requires --confirm. Destination/source are 1-based here (matching the
labels printed below and the web UI); remember to route back afterwards.
"""

import argparse
import os
import sys

# From a checkout (python tools/<script>.py) the packages beside tools/
# import without an install.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from videohubwire import Videohub, VideohubError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('ip')
    ap.add_argument('--route', nargs=2, type=int, metavar=('DEST', 'SRC'),
                    help='1-based destination + source to route (WRITE!)')
    ap.add_argument('--confirm', action='store_true',
                    help='required with --route')
    args = ap.parse_args()

    with Videohub(args.ip) as vh:
        snap = vh.state()
        dev = snap['device']
        print(f"Model:    {dev['model_name']}")
        print(f"Name:     {dev['friendly_name']}")
        print(f"Size:     {dev['video_inputs']} x {dev['video_outputs']}")
        print(f"Protocol: {dev['protocol_version']}")
        print()
        print(f"{'DEST':>4}  {'LOCK':4}  {'LABEL':<24} <- SOURCE")
        for o in snap['outputs']:
            src = next((i for i in snap['inputs'] if i['index'] == o['source']),
                       None)
            src_label = src['label'] if src else f"?{o['source']}"
            print(f"{o['index'] + 1:>4}  {o['lock']:4}  {o['label']:<24} "
                  f"<- {src_label}")

        if args.route:
            dest, src = args.route[0] - 1, args.route[1] - 1
            if not args.confirm:
                print('\n--route requires --confirm (this changes live '
                      'routing). No route sent.')
                return 2
            print(f'\nRouting dest {dest + 1} <- src {src + 1} ...')
            try:
                vh.route(dest, src)
                print('ACK — route applied. Remember to route back.')
            except VideohubError as e:
                print(f'FAILED: {e}')
                return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
