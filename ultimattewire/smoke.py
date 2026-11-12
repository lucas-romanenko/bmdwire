# SPDX-License-Identifier: MIT
"""Read everything an Ultimatte says on TCP 9998 and print it, to check
:class:`ultimattewire.Ultimatte` against a real unit::

    python -m ultimattewire.smoke 192.0.2.21
    python -m ultimattewire.smoke 192.0.2.21 --watch 30     # then touch the panel
    python -m ultimattewire.smoke 192.0.2.21 --write        # one reversible write
    python -m ultimattewire.smoke 192.0.2.21 --image        # upload, assign, undo
    python -m ultimattewire.smoke 192.0.2.21 --sample wall  # cursor, Sample Wall, undo

Read only unless ``--write``. What it does, in order:

1. Connects and reads the status dump; prints every block, typed.
2. Compares the unit with the protocol document: controls the unit has
   that the document doesn't, documented ones it lacks, values outside
   the documented range, controls the document says this model doesn't
   have. These are the findings worth sending back.
3. Asks for each documented block again (an empty block is a status dump
   request; it changes nothing) and says which the unit ACKed or NAKed.
4. ``--watch N``: prints every update the unit pushes for N seconds.
5. ``--write``: refuses if Tally Active is on (unless ``--force``), then
   sets one control (``--control``, default Matte Density) one step away
   from its value, checks the unit's update, and sets it back, checking
   again. The original value is put back even if the check fails.
6. ``--image [FILE]``: refuses if Tally Active is on (unless
   ``--force``), uploads FILE (a PNG or JPG; or a plain grey PNG of the
   running format's size, made here) to the media pool over 9996, in
   chunks with its progress, assigns it to ``--buffer`` (BG 1), checks the
   unit's update, then puts back what that buffer had and deletes the
   image again (``--keep-image`` leaves it). Not in the document: from
   captures of Software Control.
   ``--sample wall|floor``: refuses if Tally Active is on (unless
   ``--force``), turns that cursor on, runs Sample Wall / Sample Floor and
   prints how the unit ended it (``yes`` as the document says, or the
   cursor off and CONTROL DEFAULT as a 12 4K did in a capture) and how
   long it took, then sets every control the sample changed back (one
   CONTROL block). It writes to the keyer: CONTROL DEFAULT, which the unit
   rewrites after a sample, is not put back.
7. ``--flush``: sends FLUSH SETTINGS (what Software Control does before an
   Archive: it saves the live settings on the unit) and prints whatever
   the unit answers.

Exit status 0 when everything it tried worked, 1 when a write, an image
step, the sample or an undo did not verify, 2 when it could not connect.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
import zlib

from ultimattewire import controls as C
from ultimattewire import media
from ultimattewire.client import (CURSOR_COLOR_KEYS, Ultimatte,
                                  UltimatteConnectionError,
                                  UltimatteError, UltimatteNak,
                                  UltimatteTimeout)

DUMP_HEADERS = ["PROTOCOL PREAMBLE", "IDENTITY", "NETWORK",
                "NETWORK INTERFACE 0", "VERSION", "DEVICE", "VIDEO FORMATS",
                "CONTROL", "CONTROL DEFAULT", "CURRENT FILE", "FILE LIST",
                "GPI LIST", "IMAGE LIST", "FRAME BUFFER", "CAMERACONTROL"]


def _p(out, *parts):
    print(*parts, file=out)


def report_state(u: Ultimatte, out) -> None:
    s = u.state
    _p(out, f"== {u.host}:{u.port}")
    _p(out, f"protocol      {s.protocol_version}")
    _p(out, f"model         {s.identity.model!r}  (family {s.family or 'not in the document'})")
    _p(out, f"label         {s.identity.label!r}   unique id {s.identity.unique_id}")
    v = s.version
    _p(out, f"version       product {v.product_id}  hardware {v.hardware_version}  "
            f"software {v.software_version}  release {v.software_release}")
    _p(out, f"network       {s.network}")
    for i, iface in sorted(s.interfaces.items()):
        _p(out, f"interface {i}   {iface.name!r} mac {iface.mac} dynamic {iface.dynamic} "
                f"current {iface.address}/{iface.netmask} gw {iface.gateway} "
                f"static {iface.static_address}/{iface.static_netmask} gw {iface.static_gateway}")
    d = s.device
    _p(out, f"device        running {d.video_format!r}  reference {d.reference_source!r}")
    for k, val in d.inputs.items():
        _p(out, f"  input       {k:<16} {val}")
    _p(out, f"video formats {len(s.video_formats)}: {', '.join(s.video_formats)}")
    _p(out, f"current file  {s.current_file.name!r}  status {s.current_file.status or '(none sent)'}")
    _p(out, f"file list     {s.file_list}")
    for gid, gl in sorted(s.gpi_lists.items()):
        _p(out, f"gpi {gid}         index {gl.index}  events {gl.events}")
    _p(out, f"image list    {s.image_list}  capacity {s.image_capacity}  "
            f"available {s.image_available}")
    fb = s.frame_buffer
    _p(out, f"frame buffers count {fb.count}  mix {fb.mix}  duration(ms) {fb.duration}")
    for name, b in fb.buffers.items():
        _p(out, f"  buffer      {name:<6} enable {b.enable}  index {b.index}")
    for name, img in fb.assigned.items():
        _p(out, f"  assigned    {name:<6} {img!r}")
    for n, img in sorted(fb.images.items()):
        _p(out, f"  slot        {n:<3} {img!r}")
    if fb.other:
        _p(out, f"  other       {fb.other}")
    _p(out, f"camera id     {s.camera_id}")
    _p(out, f"tally         {s.tally}")
    if s.messages:
        _p(out, f"messages      {s.messages}")
    for h, lines in s.other.items():
        _p(out, f"other block   {h} {lines}")
    _p(out, f"controls      {len(s.controls)} (defaults {len(s.control_defaults)})")
    for name, val in s.controls.items():
        c = C.lookup(name)
        doc = _doc_range(c)
        dflt = s.control_defaults.get(name, "")
        _p(out, f"  {name:<34} {str(val):<22} default {str(dflt):<14} {doc}")


def _doc_range(c):
    if c is None:
        return "(not in the document)"
    if c.kind == C.RANGE:
        hi = c.maximum if c.maximum is not None else "format"
        return f"[{c.minimum}-{hi}]"
    if c.kind == C.CHOICE:
        return "[" + "/".join(c.choices[:6]) + ("/..." if len(c.choices) > 6 else "") + "]"
    return f"[{c.kind}]"


def compare_with_document(u: Ultimatte) -> list:
    """Findings: how this unit differs from the protocol document."""
    s = u.state
    family = s.family
    ctx = u.context()
    found = []
    for name, val in s.controls.items():
        c = C.lookup(name)
        if c is None:
            found.append(f"control not in the document: {name!r} = {val!r}")
            continue
        if not c.available_on(family):
            found.append(f"the document says the {family} has no {name!r}; it sent {val!r}")
        if c.kind in (C.RANGE, C.BOOL, C.READONLY, C.FUNCTION) and isinstance(val, str):
            found.append(f"{name!r}: value {val!r} isn't the document's {c.kind}")
        elif c.kind == C.RANGE:
            hi = C.maximum_for(c, ctx)
            if val < c.minimum or (hi is not None and val > hi):
                found.append(f"{name!r} = {val} outside the document's {c.minimum}-{hi}")
        elif c.kind == C.CHOICE:
            try:
                C.format_value(c, val, C.Context(video_formats=ctx.video_formats))
            except C.ControlValueError:
                found.append(f"{name!r} = {val!r} isn't one of the document's choices")
    for c in C.CONTROLS:
        # Functions are write-only: a 12 4K (2.3) never lists them in a
        # status dump (smoke run on .81.42, 2026-11-12), so their absence
        # says nothing.
        if c.kind == C.FUNCTION:
            continue
        if c.available_on(family) and c.name not in s.controls and \
                not any(k.lower() == c.name.lower() for k in s.controls):
            if c.min_protocol and s.protocol and s.protocol < c.min_protocol:
                continue
            found.append(f"documented control missing on the unit: {c.name!r}")
    if family:
        want = set(C.model_inputs(family))
        have = set(s.device.inputs)
        norm = lambda k: k.replace(" ", "").lower()                    # noqa: E731
        missing = {k for k in want if norm(k) not in {norm(h) for h in have}}
        extra = {k for k in have if norm(k) not in {norm(w) for w in want}}
        if missing:
            found.append(f"DEVICE lacks inputs the document lists for the {family}: {sorted(missing)}")
        if extra:
            found.append(f"DEVICE has inputs the document doesn't list for the {family}: {sorted(extra)}")
    if s.current_file.status:
        found.append("CURRENT FILE has Name:/Status: lines (the document shows one bare line)")
    if not s.prelude_complete:
        found.append("no END PRELUDE")
    return found


def probe_dump_requests(u: Ultimatte, out) -> dict:
    """Ask for each documented block; record ACK / NAK / no answer."""
    result = {}
    for h in DUMP_HEADERS:
        try:
            lines = u.refresh(h)
            result[h] = f"ACK, {len([ln for ln in lines[1:] if ln.strip()])} lines"
        except UltimatteNak:
            result[h] = "NAK"
        except UltimatteTimeout as e:
            result[h] = f"no answer ({e})"
        _p(out, f"  request {h:<20} {result[h]}")
    return result


def watch(u: Ultimatte, seconds: float, out) -> int:
    n = 0

    def show(upd):
        nonlocal n
        n += 1
        _p(out, f"  {time.strftime('%H:%M:%S')} {upd.header} {upd.changes}")
    off = u.subscribe(show)
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            u.poll(min(0.5, max(0.0, end - time.monotonic())))
    finally:
        off()
    return n


def write_test(u: Ultimatte, name: str, force: bool, out) -> bool:
    """One reversible write. True when both the change and the restore
    were confirmed by the unit."""
    if u.state.tally and not force:
        _p(out, "  refused: Tally Active is on (the unit is on air); --force to override")
        return False
    c = C.lookup(name)
    original = u.get_control(name)
    if c is None or c.kind != C.RANGE or not isinstance(original, int):
        _p(out, f"  refused: {name!r} isn't a numeric control this unit reported")
        return False
    hi = C.maximum_for(c, u.context())
    step = 1 if (hi is None or original < hi) else -1
    target = original + step
    ok = False
    try:
        _p(out, f"  set {name} {original} -> {target}")
        got = u.set_control(name, target)
        ok = got == target
        _p(out, f"  unit reports {got!r} {'OK' if ok else 'MISMATCH'}")
    except UltimatteError as e:
        _p(out, f"  write failed: {e}")
    finally:
        try:
            back = u.set_control(name, original)
            restored = back == original
            _p(out, f"  restored {name} to {back!r} {'OK' if restored else 'MISMATCH: CHECK THE UNIT'}")
        except UltimatteError as e:
            restored = False
            _p(out, f"  RESTORE FAILED ({e}): set {name} back to {original} by hand")
    return ok and restored


SMOKE_IMAGE = "ultimattewire-smoke.png"


def grey_png(width: int, height: int) -> bytes:
    """A plain mid-grey 8-bit RGB PNG, made with the standard library."""
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    row = b"\0" + b"\x80" * (3 * width)
    return (media.PNG_SIGNATURE
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height, 6))
            + chunk(b"IEND", b""))


def image_test(u: Ultimatte, path, buffer: str, keep: bool, force: bool,
               out) -> bool:
    """Upload, assign, check, undo. True when every step and the undo were
    confirmed by the unit."""
    if u.state.tally and not force:
        _p(out, "  refused: Tally Active is on (the unit is on air); --force to override")
        return False
    fg = u.state.device.locked("FG")
    _p(out, f"  running format {u.state.device.video_format!r}, FG locked: {fg}")
    if path:
        name = os.path.basename(path)
        try:
            with open(path, "rb") as f:
                data = f.read(media.HEADER_LIMIT)
            length = os.path.getsize(path)
        except OSError as e:
            _p(out, f"  refused: {e}")
            return False
    else:
        size = media.format_size(u.state.device.video_format)
        if size is None:
            _p(out, "  refused: can't tell the frame size from the running format; give a PNG")
            return False
        name, data = SMOKE_IMAGE, grey_png(*size)
        length = len(data)
    if name in u.state.image_list:
        _p(out, f"  refused: {name!r} is already in the media pool; won't touch it")
        return False
    before = u.assigned_image(buffer)
    _p(out, f"  {buffer} has {before!r}; pool {u.state.image_list} "
            f"(available {u.state.image_available} of {u.state.image_capacity} bytes)")
    try:
        kind, (w, h) = media.image_size(data)
    except media.UltimatteImageError as e:
        _p(out, f"  refused: {e}")
        return False
    ok = uploaded = False
    steps = []

    def progress(sent, total):
        # a line per quarter, so a big file shows it is moving
        if not steps or sent * 4 // total > steps[-1] or sent == total:
            steps.append(sent * 4 // total)
            _p(out, f"  sent {sent} of {total} bytes")
    try:
        t0 = time.monotonic()
        u.upload_image(name, path or data, progress=progress)
        uploaded = True
        _p(out, f"  uploaded {name} ({kind} {w}x{h}, {length} bytes) in "
                f"{time.monotonic() - t0:.2f}s; pool now {u.state.image_list}")
        u.assign_image(buffer, name)
        got = u.assigned_image(buffer)
        ok = got == name
        _p(out, f"  assigned to {buffer}: unit shows {got!r} {'OK' if ok else 'MISMATCH'}")
    except UltimatteError as e:
        _p(out, f"  failed: {e}")
    finally:
        undone = True
        if uploaded and before and u.assigned_image(buffer) != before:
            try:
                u.assign_image(buffer, before)
                _p(out, f"  put {before!r} back on {buffer} OK")
            except UltimatteError as e:
                undone = False
                _p(out, f"  PUT BACK FAILED ({e}): put {before!r} on {buffer} by hand")
        if uploaded and not keep:
            try:
                u.delete_image(name)
                _p(out, f"  deleted {name}; pool {u.state.image_list}, {buffer} has "
                        f"{u.assigned_image(buffer)!r}")
            except UltimatteError as e:
                undone = False
                _p(out, f"  DELETE FAILED ({e}): delete {name} in Software Control")
    return ok and undone


def sample_test(u: Ultimatte, which: str, force: bool, out) -> bool:
    """Cursor on, Sample Wall / Sample Floor, report, put back every
    control it changed. True when the sample ended and the restore was
    confirmed by the unit."""
    if u.state.tally and not force:
        _p(out, "  refused: Tally Active is on (the unit is on air); --force to override")
        return False
    where = which.strip().title()
    function, cursor = f"Sample {where}", f"{where} Cursor Position Enable"
    before = dict(u.state.controls)
    seen = []
    off = u.subscribe(seen.append)
    ok = False
    try:
        t0 = time.monotonic()
        on = u.set_control(cursor, True)
        streamed = sum(1 for upd in seen if isinstance(upd.changes, dict)
                       and any(k.lower() in CURSOR_COLOR_KEYS for k in upd.changes))
        _p(out, f"  {cursor}: unit reports {on!r} after {time.monotonic() - t0:.2f}s "
                f"({streamed} cursor colour pushes so far)")
        mark, t0 = len(seen), time.monotonic()
        u.run_function(function)
        after = seen[mark:]
        said_yes = any(upd.header == "CONTROL:" and isinstance(upd.changes, dict)
                       and upd.changes.get(function) is True for upd in after)
        default = any(upd.header == "CONTROL DEFAULT:" for upd in after)
        _p(out, f"  {function}: ended after {time.monotonic() - t0:.2f}s by "
                f"{'yes' if said_yes else 'CONTROL DEFAULT' if default else '?'}"
                f"{'' if said_yes else ' (no yes)'}; {cursor} now {u.get_control(cursor)!r}")
        ok = True
    except UltimatteError as e:
        _p(out, f"  failed: {e}")
    finally:
        off()
    changed = {}
    for k, v in before.items():
        c = C.lookup(k)
        if (c is not None and c.kind in (C.RANGE, C.BOOL)
                and u.state.controls.get(k) != v):
            changed[k] = v
    for k, v in changed.items():
        _p(out, f"  changed {k}: {v!r} -> {u.state.controls.get(k)!r}")
    if not changed:
        return ok
    # Wall/Floor Color writes are refused with "Control Locked - Enable
    # 'Manual Color' function" while Manual Color Enable is off (smoke run
    # on .81.42, 2026-11-12), so they go back under Manual Color, which is
    # then set back to what it was.
    colours = {k: v for k, v in changed.items()
               if k.lower().startswith(("wall color", "floor color"))}
    rest = {k: v for k, v in changed.items() if k not in colours}
    restored = True
    for group in (rest, colours):
        if not group:
            continue
        manual = group is colours and not u.get_control("Manual Color Enable")
        try:
            if manual:
                u.set_control("Manual Color Enable", True)
            try:
                got = u.set_controls(group)
            finally:
                if manual:
                    u.set_control("Manual Color Enable", False)
            good = got == group
            _p(out, f"  put {len(group)} controls back"
                    f"{' (under Manual Color)' if manual else ''} "
                    f"{'OK' if good else 'MISMATCH: CHECK THE UNIT'}")
        except UltimatteError as e:
            good = False
            _p(out, f"  PUT BACK FAILED ({e}): set these by hand: {group}")
        restored = restored and good
    return ok and restored


def flush_test(u: Ultimatte, out) -> bool:
    seen = []
    off = u.subscribe(lambda upd: seen.append(upd))
    try:
        u.flush_settings()
        _p(out, "  ACK, no refusal")
        return True
    except UltimatteError as e:
        _p(out, f"  refused: {e}")
        return False
    finally:
        off()
        for upd in seen:
            _p(out, f"  unit sent {upd.header} {upd.changes}")


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="python -m ultimattewire.smoke",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=9998)
    ap.add_argument("--settings-port", type=int, default=9996,
                    help=argparse.SUPPRESS)
    ap.add_argument("--idle-timeout", type=float, default=3.0,
                    help="seconds of silence from the unit before giving up")
    ap.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                    help="print pushed updates for this long")
    ap.add_argument("--write", action="store_true",
                    help="one reversible control write (off by default)")
    ap.add_argument("--control", default="Matte Density",
                    help="the control --write nudges by one step")
    ap.add_argument("--force", action="store_true",
                    help="--write / --image / --sample even while Tally Active is on")
    ap.add_argument("--image", nargs="?", const="", default=None, metavar="FILE",
                    help="upload a PNG or JPG (default: a grey PNG made here), "
                         "assign it, undo")
    ap.add_argument("--buffer", default="BG 1",
                    help="the frame buffer --image assigns to")
    ap.add_argument("--keep-image", action="store_true",
                    help="leave the --image upload in the media pool")
    ap.add_argument("--sample", choices=("wall", "floor"),
                    help="cursor on, Sample Wall / Floor, put the controls back "
                         "(writes to the keyer)")
    ap.add_argument("--flush", action="store_true",
                    help="send FLUSH SETTINGS and print what the unit says")
    ap.add_argument("--json", metavar="FILE",
                    help="also save the state as JSON")
    args = ap.parse_args(argv)

    u = Ultimatte(args.host, args.port, idle_timeout=args.idle_timeout,
                  settings_port=args.settings_port)
    t0 = time.monotonic()
    try:
        u.connect()
    except UltimatteError as e:
        _p(out, f"could not connect: {e}")
        return 2
    status = 0
    try:
        _p(out, f"connected, status dump read in {time.monotonic() - t0:.2f}s")
        report_state(u, out)
        _p(out, "== differences from the protocol document")
        findings = compare_with_document(u)
        for f in findings:
            _p(out, f"  - {f}")
        if not findings:
            _p(out, "  none")
        _p(out, "== status dump requests (read only)")
        probe_dump_requests(u, out)
        if args.watch > 0:
            _p(out, f"== watching for {args.watch:g}s (change something on the unit)")
            n = watch(u, args.watch, out)
            _p(out, f"  {n} updates")
        if args.write:
            _p(out, "== write test")
            if not write_test(u, args.control, args.force, out):
                status = 1
        if args.image is not None:
            _p(out, "== image test (upload over 9996, assign, undo)")
            if not image_test(u, args.image, args.buffer, args.keep_image,
                              args.force, out):
                status = 1
        if args.sample:
            _p(out, f"== sample test (Sample {args.sample.title()}, then put back)")
            if not sample_test(u, args.sample, args.force, out):
                status = 1
        if args.flush:
            _p(out, "== flush settings")
            if not flush_test(u, out):
                status = 1
        if args.json:
            with open(args.json, "w") as f:
                json.dump(u.state.to_dict(), f, indent=2, default=str)
            _p(out, f"state saved to {args.json}")
    except UltimatteConnectionError as e:
        _p(out, f"connection lost: {e}")
        status = 2
    finally:
        u.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
