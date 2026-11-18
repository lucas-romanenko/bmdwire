#!/usr/bin/env python3
"""Read-only: dump every op in one macro slot with BOTH the decoded
attributes AND the raw opcode payload bytes.

General-purpose macro op-raw probe. For each op it prints the frame header
(``[u16le op_len][u16le op_type]``), the decoded id (or ``Unknown_0xNNNN``
when the op code isn't in ``macrotransfer._KNOWN_OPS``), the decoded attrs
(``rawParams`` hex for unknowns), and the raw payload bytes. Use it to
capture an unmapped SET command's opcode + wire layout: record a macro that
performs the action in ASC (e.g. set master compressor / limiter), then run
this on that slot and read the ``Unknown_0xNNNN`` op's payload bytes.

As a bonus it also cross-checks the ``TransitionDVEPattern`` op (opcode
0x0034): it isolates payload[1] (the pattern byte; payload[0] is the M/E
index) and compares it to the live ``transition-dve[0].style``.

This issues NO state-changing commands. It downloads the macro bytecode
over the file-transfer READ path (the same path Profile.from_atem uses for
macro export) and reads mixerstate — nothing is written to the switcher.

Usage:
    python tools/macro_dve_probe.py <ATEM_IP> [SLOT]

SLOT defaults to 10.
"""

import os
import sys

# From a checkout (python tools/<script>.py) the packages beside tools/
# import without an install.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from atemwire import ATEM
from atemwire.macrotransfer import decode_macro_bytecode, download_macro_bytecode
from atemwire.profile._common import (
    _resolve_mixerstate,
    _resolve_protocol_or_none,
)
from atemwire.ready import wait_state_settled

_DVE_PATTERN_OP = 0x0034


def _hex(b):
    if not b:
        return '<empty>'
    return ' '.join(f'{x:02x}' for x in b)


def _u16le(b, o):
    return b[o] | (b[o + 1] << 8)


def _frames(raw):
    """Yield (offset, op_len, op_type, payload_bytes) for each op frame.
    Mirrors decode_macro_bytecode's framing: [u16le op_len][u16le op_type]
    then op_len-4 payload bytes (op_len counts the 4-byte header)."""
    o, n = 0, len(raw)
    while o + 4 <= n:
        op_len = _u16le(raw, o)
        op_type = _u16le(raw, o + 2)
        if op_len < 4 or o + op_len > n:
            break
        yield o, op_len, op_type, raw[o + 4:o + op_len]
        o += op_len


def main():
    if len(sys.argv) < 2:
        print('usage: macro_dve_probe.py <ATEM_IP> [SLOT]')
        sys.exit(2)
    ip = sys.argv[1]
    slot = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    print(f'# macro_dve_probe — read-only — target {ip} slot {slot}')
    with ATEM(ip) as atem:
        if not atem.connected:
            print('FAIL: failed to connect')
            sys.exit(1)
        wait_state_settled(atem)
        mx = _resolve_mixerstate(atem)
        protocol = _resolve_protocol_or_none(atem)
        if protocol is None:
            print('FAIL: no protocol on this connection')
            sys.exit(1)

        # Live DVE transition style for the cross-check (raw wire int).
        dve = (mx.get('transition-dve', {}) or {}).get(0)
        live_style = getattr(dve, 'style', None)

        raw = download_macro_bytecode(protocol, slot)

    print(f'\nlive transition-dve[0].style = {live_style}')

    if not raw:
        print(f'\nslot {slot}: empty / no bytecode downloaded '
              '(is the slot recorded?)')
        print('\n# done — no writes issued')
        return

    print(f'\nslot {slot} bytecode: {len(raw)} bytes')
    print(f'full hex: {_hex(raw)}')

    decoded = decode_macro_bytecode(raw, mx)
    frames = list(_frames(raw))
    print(f'\n{len(frames)} op frame(s) (decoded list has {len(decoded)}):')

    for i, (off, op_len, op_type, params) in enumerate(frames):
        dec = decoded[i] if i < len(decoded) else {}
        attrs = {k: v for k, v in dec.items() if k != 'id'}
        print(f'\n[{i}] offset={off} op_len={op_len} '
              f'op_type=0x{op_type:04x}  id={dec.get("id", "?")}')
        print(f'    decoded attrs : {attrs}')
        print(f'    raw payload [{len(params)}B]: {_hex(params)}')

        if op_type == _DVE_PATTERN_OP:
            me = params[0] if len(params) > 0 else None
            pat_byte = params[1] if len(params) > 1 else None
            print('    ---- TransitionDVEPattern (0x0034) ----')
            print(f'      payload[0] (M/E index)   = {me}')
            pat_show = (f'{pat_byte}  (0x{pat_byte:02x})'
                        if pat_byte is not None else '<missing>')
            print(f'      payload[1] (pattern byte) = {pat_show}')
            print(f'      decoded pattern=          = '
                  f'{dec.get("pattern")!r}  (via current macro table)')
            print(f'      live transition-dve.style = {live_style}')
            if pat_byte is not None and live_style is not None:
                if pat_byte == live_style:
                    print('      => SHARED numbering: macro byte == live '
                          'style. The macro table is STALE wherever its name '
                          f'for {pat_byte} differs from the profile enum.')
                else:
                    print('      => DISTINCT encoding: macro byte != live '
                          'style. Both tables can be correct in their own '
                          'context (document, do not reconcile).')

    print('\n# done — no writes issued')


if __name__ == '__main__':
    # A signal unwinds instead of killing, so every session gets its goodbye.
    import _exit_signals
    _exit_signals.install()
    main()
