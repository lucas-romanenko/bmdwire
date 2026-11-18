#!/usr/bin/env python3
"""Macro save/restore round-trip validator (TEST ATEM ONLY).

Profile save/restore handles macros on a *separate* path from the rest of
switcher state: SAVE downloads each macro's opcode bytecode and decodes it to
XML ``<Op>`` actions; RESTORE encodes those actions back to bytecode and
uploads it. This proves every ACTION inside a macro survives that round-trip
intact — not just that the macro uploads.

Flow:
  1. Record a macro full of diverse common actions into a scratch slot.
  2. SAVE  — download its bytecode, decode to an op list (ground truth).
  3. RESTORE — re-encode those ops and upload to a second scratch slot
     (mirrors what Profile.apply does on restore).
  4. SAVE again — download the restored slot, decode.
  5. Compare:
       a. bytecode_A  vs re-encoded   — does decode→encode reproduce the
          original bytes (the restore encode step is lossless)?
       b. re-encoded  vs bytecode_B   — does the switcher upload/download
          the bytes faithfully?
       c. ops_A       vs ops_B        — per-ACTION, human-readable.
  6. Report which actions survived; erase both scratch slots.

  *** DESTRUCTIVE while recording — program/preview/USK/DSK/aux actually move
  as the macro records them. TEST ATEM ONLY. Scratch slots are erased. ***

Usage:
    python tools/macro_roundtrip_smoke.py <ATEM_IP> [--force]
"""

import argparse
import logging
import os
import sys
import time

logging.basicConfig(level=logging.WARNING)
logging.getLogger('atemwire').setLevel(logging.WARNING)

# From a checkout (python tools/<script>.py) the packages beside tools/
# import without an install.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from atemwire.pool import acquire_connection                         # noqa: E402
from atemwire.ready import wait_state_settled                       # noqa: E402
from atemwire.macrotransfer import (                                 # noqa: E402
    decode_macro_bytecode, download_macro_bytecode, encode_single_op,
    upload_macro_bytecode,
)
from atemwire.messages.macros import (                               # noqa: E402
    MacroActionCommand, MacroRecordCommand,
)
from atemwire.messages.switching import (                            # noqa: E402
    set_aux_output, set_preview, set_program,
)
from atemwire.messages.upstream_keyer import (                       # noqa: E402
    set_usk_fill_source, set_usk_on_air, set_usk_type,
)
from atemwire.messages.downstream_keyer import (                     # noqa: E402
    set_dsk_on_air, set_dsk_tie,
)
from atemwire.messages.transition import set_transition_style        # noqa: E402
from atemwire.messages.color_generator import (                      # noqa: E402
    set_color_generator_hue, set_color_generator_luma,
    set_color_generator_saturation,
)
from atemwire.messages.media import set_media_player_still           # noqa: E402
from atemwire.messages.fade_to_black import set_ftb_rate             # noqa: E402
from atemwire.profile._enums import TRANS_STYLE_TO_INT, USK_TYPE_TO_INT  # noqa: E402

REF_SLOT = 99   # where the diverse macro is recorded
DST_SLOT = 98   # where the re-encoded macro is restored


# Diverse common macro actions. Each tuple is (label, fn) — fn(conn) sends the
# control command, which the ATEM captures as a macro op while recording.
def _actions():
    return [
        ('program',            lambda c: set_program(c, source=2001, me=0)),
        ('preview',            lambda c: set_preview(c, source=2002, me=0)),
        ('usk0.type',          lambda c: set_usk_type(c, keyer=0, key_type=USK_TYPE_TO_INT['Luma'], me=0)),
        ('usk0.fill',          lambda c: set_usk_fill_source(c, keyer=0, source=2001, me=0)),
        ('usk0.onAir',         lambda c: set_usk_on_air(c, keyer=0, enabled=True, me=0)),
        ('dsk0.onAir',         lambda c: set_dsk_on_air(c, enabled=True, dsk_idx=0)),
        ('dsk0.tie',           lambda c: set_dsk_tie(c, tie=True, dsk_idx=0)),
        ('transition.style',   lambda c: set_transition_style(c, style=TRANS_STYLE_TO_INT['Wipe'], me=0)),
        ('colorgen0.hue',      lambda c: set_color_generator_hue(c, 0, 137.0)),
        ('colorgen0.sat',      lambda c: set_color_generator_saturation(c, 0, 64.0)),
        ('colorgen0.luma',     lambda c: set_color_generator_luma(c, 0, 41.0)),
        ('aux0',               lambda c: set_aux_output(c, aux=0, source=2001)),
        ('mediaplayer0',       lambda c: set_media_player_still(c, player=0, slot=1)),
        ('ftb.rate',           lambda c: set_ftb_rate(c, rate_str='30', me=0)),
    ]


def _stop(conn):
    conn.send(MacroActionCommand(index=0xFFFF, action=2))  # action 2 = stop


def _erase(conn, slot):
    conn.send(MacroRecordCommand(index=slot, name='', description=''))
    time.sleep(0.3)
    _stop(conn)
    time.sleep(0.4)


def _record_macro(conn):
    print(f'[1/5] recording {len(_actions())} actions into slot {REF_SLOT} ...')
    conn.send(MacroRecordCommand(index=REF_SLOT, name='RT_VERIFY',
                                 description='macro roundtrip test'))
    time.sleep(0.5)
    for label, fn in _actions():
        fn(conn)
        time.sleep(0.18)
    time.sleep(0.3)
    _stop(conn)
    time.sleep(1.5)


def _opkey(op):
    """Comparable form of one op: id + sorted attrs (everything decode emits)."""
    return (op.get('id'), tuple(sorted(
        (k, v) for k, v in op.items() if k != 'id')))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('ip')
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()

    if not args.force:
        print('\n' + '!' * 70)
        print(f'  DESTRUCTIVE macro recording against ATEM {args.ip} — TEST ATEM ONLY.')
        print(f'  Records into slots {REF_SLOT}/{DST_SLOT} and erases them after.')
        print('!' * 70)
        if input(f"\nType 'yes' to proceed against {args.ip}: ").strip().lower() != 'yes':
            print('Aborted.')
            sys.exit(1)

    rc = 0
    with acquire_connection(args.ip) as conn:
        wait_state_settled(conn, timeout=8.0)
        proto = conn.protocol
        try:
            _record_macro(conn)

            print(f'[2/5] saving slot {REF_SLOT} (download + decode) ...')
            bytecode_a = download_macro_bytecode(proto, REF_SLOT)
            ops_a = decode_macro_bytecode(bytecode_a, conn.mixerstate)
            print(f'      recorded {len(ops_a)} action(s): '
                  f'{", ".join(o["id"] for o in ops_a)}')

            print('[3/5] restoring (re-encode + upload) to slot '
                  f'{DST_SLOT} ...')
            chunks, dropped = [], []
            for op in ops_a:
                try:
                    chunks.append(encode_single_op(op))
                except ValueError as e:
                    dropped.append((op['id'], str(e)))
            reencoded = b''.join(chunks)
            upload_macro_bytecode(proto, DST_SLOT, 'RT_RESTORED',
                                  'restored', reencoded, timeout=10.0)
            time.sleep(1.0)

            print(f'[4/5] saving slot {DST_SLOT} (download + decode) ...')
            bytecode_b = download_macro_bytecode(proto, DST_SLOT)
            ops_b = decode_macro_bytecode(bytecode_b, conn.mixerstate)

            print('[5/5] comparing ...')
            # The round-trip guarantee = every ACTION is preserved through
            # save -> restore -> save:
            #   (b) hw_faithful : the switcher returns exactly the bytes we
            #       uploaded (upload/download is lossless).
            #   (c) per_action_ok: re-saving the restored macro decodes to the
            #       identical action list. (b)+(c) together prove the restore
            #       encode is lossless for every recorded action.
            # (a) below is INFORMATIONAL only: whether pyatem's canonical
            #     re-encode reproduces the SWITCHER's recording bytes. They can
            #     legitimately differ (same actions, different byte layout) — so
            #     it does NOT gate pass/fail.
            hw_faithful = (reencoded == bytecode_b)
            survivors_expected = [o for o in ops_a
                                  if o['id'] not in {d[0] for d in dropped}]
            a_keys = [_opkey(o) for o in survivors_expected]
            b_keys = [_opkey(o) for o in ops_b]
            per_action_ok = (a_keys == b_keys)
            same_recording_bytes = (not dropped) and (reencoded == bytecode_a)
            # Unknown ops survive byte-for-byte via rawParams but aren't
            # semantically named in the saved XML — worth flagging for coverage.
            unmapped = [o['id'] for o in ops_a if o['id'].startswith('Unknown_')]

            print('\n' + '=' * 70)
            print('MACRO ROUND-TRIP RESULTS')
            print('=' * 70)
            print(f'  actions recorded                 : {len(ops_a)}')
            print(f'  dropped on restore (no codec)    : {len(dropped)}')
            for oid, why in dropped:
                print(f'      ! {oid}: {why}')
            print(f'  switcher byte-faithful (upload)  : {hw_faithful}'
                  f'  ({len(bytecode_b)} B)')
            print(f'  PER-ACTION preserved             : {per_action_ok}'
                  f'  ({len(b_keys)}/{len(a_keys)})')
            print(f'  (info) re-encode == recording    : {same_recording_bytes}'
                  '  [differing is fine: switcher vs pyatem layout]')
            if unmapped:
                print(f'  (note) preserved via raw bytes but not decoded to a '
                      f'named op: {", ".join(sorted(set(unmapped)))}')

            if not per_action_ok:
                rc = 1
                print('\n  PER-ACTION MISMATCHES:')
                for i in range(max(len(a_keys), len(b_keys))):
                    ak = a_keys[i] if i < len(a_keys) else None
                    bk = b_keys[i] if i < len(b_keys) else None
                    if ak != bk:
                        av = survivors_expected[i] if i < len(survivors_expected) else None
                        bv = ops_b[i] if i < len(ops_b) else None
                        print(f'      [{i}] saved : {av}')
                        print(f'          restored: {bv}')

            if dropped:
                rc = 1
            if hw_faithful and per_action_ok and not dropped:
                print('\nPASS — every recorded macro action survived '
                      'save->restore intact.')
            else:
                print('\nFAIL — see above; some actions did not round-trip.')
        finally:
            print(f'\n[cleanup] erasing slots {REF_SLOT}, {DST_SLOT} ...')
            _erase(conn, REF_SLOT)
            _erase(conn, DST_SLOT)

    sys.exit(rc)


if __name__ == '__main__':
    # A signal unwinds instead of killing, so every session gets its goodbye.
    import _exit_signals
    _exit_signals.install()
    main()
