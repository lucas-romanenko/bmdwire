#!/usr/bin/env python3
"""Live verification for the C1 fix — confirm Profile.from_atem decodes
all macro ops without producing Unknown_0x... artifacts.

Reads every used macro slot via Profile.from_atem, scans the resulting
XML's <MacroPool> for any unmapped op-codes, and prints a per-slot
report.

Usage:
    python tools/profile_macro_verify.py 192.0.2.10
"""
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logging.getLogger('atemwire').setLevel(logging.WARNING)

# From a checkout (python tools/<script>.py) the packages beside tools/
# import without an install.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from atemwire import ATEM, Profile


def main():
    if len(sys.argv) < 2:
        print("Usage: profile_macro_verify.py <ip>", file=sys.stderr)
        sys.exit(2)
    ip = sys.argv[1]

    print(f"Connecting to {ip}...")
    with ATEM(ip) as atem:
        if not atem.connected:
            print("FAIL: failed to connect"); sys.exit(1)
        print("Connected. Building profile...")
        profile = Profile.from_atem(atem)
        profile.to_xml()

    macros = profile.macros()
    print(f"\nFound {len(macros)} used macro slot(s):")
    total_ops = 0
    unknown_ops = 0
    op_id_counts: dict = {}
    for m in macros:
        n_ops = len(m['ops'])
        total_ops += n_ops
        unknown_in_slot = [op for op in m['ops']
                           if op['id'].startswith('Unknown_')]
        unknown_ops += len(unknown_in_slot)
        for op in m['ops']:
            op_id_counts[op['id']] = op_id_counts.get(op['id'], 0) + 1
        marker = '⚠ UNKNOWN' if unknown_in_slot else 'ok'
        print(f"  slot {m['index']:>3}  {m['name']!r:30}  "
              f"ops={n_ops:>3}  {marker}")
        if unknown_in_slot:
            for op in unknown_in_slot:
                print(f"        {op}")

    print(f"\nTotals: {total_ops} ops across {len(macros)} slots.")
    print(f"Unknown_0x... artifacts: {unknown_ops}")

    if op_id_counts:
        print("\nOp-id histogram:")
        for op_id in sorted(op_id_counts.keys()):
            count = op_id_counts[op_id]
            new_marker = ''
            if op_id in {'DVEAndFlyKeyYSize', 'DVEAndFlyKeyXPosition',
                         'DVEAndFlyKeyYPosition', 'DVEKeyMaskEnable',
                         'DVEKeyMaskTop', 'DVEKeyMaskBottom',
                         'DVEKeyMaskLeft', 'DVEKeyMaskRight',
                         'DownstreamKeyMaskEnable',
                         'DownstreamKeyPreMultiply', 'DownstreamKeyGain'}:
                new_marker = '  ← new decoder (2026-04-29)'
            print(f"  {op_id:36}  {count:>4}{new_marker}")

    # Final check
    if unknown_ops:
        print("\nFAIL: Unknown_0x... ops present in saved profile.")
        sys.exit(1)
    print("\nPASS: every macro op decoded with a known XML id.")


if __name__ == '__main__':
    # A signal unwinds instead of killing, so every session gets its goodbye.
    import _exit_signals
    _exit_signals.install()
    main()
