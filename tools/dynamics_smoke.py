"""Smoke test for the new Fairlight dynamics ops (CICP/CILP/CIXP).

Connects to a live ATEM, reads compressor/limiter/expander state for
Camera 1 (source=1), and bounces the threshold by 0.1 dB to verify
the wire format is accepted. Reads it back to confirm.

Usage:
    python tools/dynamics_smoke.py 192.0.2.10
"""
import sys
import time

from atemwire import ATEM


def fmt_block(name, block):
    if not block:
        return f"  {name}: <not yet received>"
    keys = ['enabled', 'threshold_db', 'ratio', 'range_db',
            'attack_ms', 'hold_ms', 'release_ms']
    parts = ', '.join(f"{k}={block[k]}" for k in keys if k in block)
    return f"  {name}: {parts}"


def main(ip):
    print(f"Connecting to {ip}...")
    with ATEM(ip) as atem:
        if not atem.connected:
            print("FAIL: not connected")
            return 1
        # Wait a moment for the Fairlight state dump to arrive (it
        # trails the rest of the handshake).
        time.sleep(2.0)

        from atemwire.messages.fairlight import (fairlight_compressor,
                                                 fairlight_limiter,
                                                 fairlight_expander)
        mx = atem.raw.mixerstate

        comps = fairlight_compressor(mx)
        lims = fairlight_limiter(mx)
        exps = fairlight_expander(mx)

        print("Source 1 strip dynamics (current state):")
        print(fmt_block('compressor', comps.get('1.0', {})))
        print(fmt_block('limiter', lims.get('1.0', {})))
        print(fmt_block('expander', exps.get('1.0', {})))

        # Round-trip test: bump compressor threshold by 0.1 dB then back.
        cur = comps.get('1.0', {})
        if not cur:
            print("\nNo AICP for source 1 yet — skipping round-trip.")
            return 0
        original = cur.get('threshold_db', -20.0)
        bumped = round(original + 0.1, 2)
        print(f"\nRound-trip: setting compressor threshold to {bumped} dB...")
        atem.set_fairlight_compressor(source=1, threshold_db=bumped)
        time.sleep(1.0)

        comps2 = fairlight_compressor(atem.raw.mixerstate)
        new_val = comps2.get('1.0', {}).get('threshold_db')
        print(f"Echoed back: {new_val} dB  (expected ~{bumped})")

        # Restore
        print(f"Restoring threshold to {original} dB...")
        atem.set_fairlight_compressor(source=1, threshold_db=original)
        time.sleep(0.5)

        comps3 = fairlight_compressor(atem.raw.mixerstate)
        final = comps3.get('1.0', {}).get('threshold_db')
        print(f"Final: {final} dB")

        ok = abs(new_val - bumped) < 0.05 and abs(final - original) < 0.05
        print(f"\n{'PASS' if ok else 'FAIL'}: threshold round-trip "
              f"{'OK' if ok else 'broken'}")
        return 0 if ok else 1


if __name__ == '__main__':
    # A signal unwinds instead of killing, so every session gets its goodbye.
    import _exit_signals
    _exit_signals.install()
    if len(sys.argv) < 2:
        print('Usage: dynamics_smoke.py <ip>', file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
