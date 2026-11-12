# SPDX-License-Identifier: MIT
"""A fake Ultimatte 12 4K that does what a real one (software 2.3) did in
captures of Software Control on 2026-11-11 and 2026-11-12, where that
differs from the document: FRAME BUFFER by image name (``Background 1:
<image>``), IMAGE LIST with ``Capacity:`` / ``Available:`` (in 4 KiB
blocks), ``Image:`` / ``Delete:``, ``CONTROL:`` / ``FLUSH SETTINGS: YES``,
CURRENT FILE as ``Name:`` / ``Status:``, and a 9996 listener taking the
0x0101 image write of a PNG or a JPG.

From the 2026-11-12 captures:
- every CONTROL write is echoed, the same value too, and the first one
  makes CURRENT FILE ``Modified``;
- ``Wall`` / ``Floor Cursor Position Enable: on`` is ACKed, then a colour
  push, then the echo (``cursor_echo_delay`` later; 550 ms on the unit);
  while a cursor is on the colour under it streams as ``Wall Color
  Blue/Green/Red`` (``Floor Color`` for the floor cursor), one block each,
  every ``stream_interval`` (40 ms on the unit);
- ``Sample Wall`` / ``Sample Floor``: ACK, nothing at all for
  ``sample_silence`` (4 s on the unit; blocks sent meanwhile wait), then
  the cursor enable ``off``, the new colours and a whole CONTROL DEFAULT;
  never ``yes``;
- a cursor move with its cursor off: ACK, then ``MESSAGE: Control Locked -
  Enable 'Cursor' function`` (seen for ``Cursor X`` with the wall cursor
  off and ``Cursor 2 X`` with the floor cursor off; that ``Cursor 2`` goes
  with the floor cursor, and absolute moves too, is the fake's reading);
- assigning an image to a background or a mask: FRAME BUFFER, then the
  unit sets ``BM`` / ``GM`` / ``HM Input Offset: 10000`` and ``Input
  Level: 6878`` with DEVICE and CONTROL DEFAULT around them (layers were
  not captured: nothing extra).

Refusals are the captured texts: ACK, then
``MESSAGE: Mismatched formats: Image format is WxH and video format is
WxH`` or ``MESSAGE: Invalid operation - No Foreground source present``.
What the unit sends after a flush that works was never seen; the fake
sends nothing.
"""

from __future__ import annotations

import socket
import socketserver
import struct
import threading
import time
from typing import Dict, List



def _base():
    # by file path, as conftest loads the fakes (a wheel has no tests package)
    import importlib.util
    from pathlib import Path
    path = Path(__file__).with_name("fake_ultimatte.py")
    spec = importlib.util.spec_from_file_location("_ultimatte_fake_unit_base", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_B = _base()
FakeUltimatte, _render = _B.FakeUltimatte, _B._render

NAMED = ("Background 1", "Background 2", "Layer 1", "Layer 2",
         "Garbage Matte", "Holdout Matte")
CAPACITY = 6736052224


BLOCK = 4096                    # the pool counts space in 4 KiB blocks
CURSORS = {"Wall": ("Cursor X", "Cursor Y"), "Floor": ("Cursor 2 X", "Cursor 2 Y")}
LOCKED = "Control Locked - Enable 'Cursor' function"
INPUT_OF = {"Background 1": "BM", "Background 2": "BM",
            "Garbage Matte": "GM", "Holdout Matte": "HM"}


def jpg(width: int, height: int, app_bytes: int = 0) -> bytes:
    """A JPEG header (JFIF APP0, an optional padding APP1, SOF0) the unit
    could read the size from, then an end."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0"
    app1 = (b"\xff\xe1" + struct.pack(">H", app_bytes + 2) + b"\0" * app_bytes
            if app_bytes else b"")
    sof = (b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3)
           + b"\x01\x22\0\x02\x11\x01\x03\x11\x01")
    return b"\xff\xd8" + app0 + app1 + sof + b"\xff\xd9"


def _size_of(data: bytes):
    """(w, h) of a PNG or JPEG, read here without ultimattewire.media."""
    if data.startswith(b"\x89PNG"):
        return struct.unpack(">II", data[16:24])
    i = 2
    while data.startswith(b"\xff\xd8") and i + 9 <= len(data):
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xC3:
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None


def png(width: int, height: int) -> bytes:
    """A PNG header the unit (and media.png_size) can read the size from."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
            + b"\0\0\0\0" + b"\0\0\0\0IEND\xaeB`\x82")


class FakeCaptured4K(FakeUltimatte):
    def __init__(self):
        super().__init__("4K")
        self.video_format = "1080p25"
        self.fg_locked = True
        self.pool: Dict[str, tuple] = {}            # name -> (w, h, bytes)
        self.named = {k: "" for k in NAMED}
        self.flushes = 0
        self.uploads: List[bytes] = []               # raw 9996 frames
        self._media = None
        self.file_status = "Clean"
        self.controls.update({"Matte Density": "2500", "Cursor X": "192",
                              "Cursor Y": "108", "Cursor 2 X": "1728",
                              "Cursor 2 Y": "972"})
        self.defaults = dict(self.controls)
        self.stream_interval = 0.04
        self.cursor_echo_delay = 0.05
        self.sample_silence = 0.3
        self.silent_controls: set = set()          # ACKed, never echoed
        self._sampling = False
        self._stop = threading.Event()

    # -- the dump: the 12 4K's form of DEVICE, IMAGE LIST, FRAME BUFFER --

    def block(self, header: str) -> str:
        if header == "DEVICE":
            body = [f"Video Format: {self.video_format}",
                    "Reference Source: Not Locked",
                    f"FG In: {'Locked' if self.fg_locked else 'Not Locked'}",
                    "BG In: Not Locked"]
            return _render(header, body)
        if header == "CURRENT FILE":
            return _render(header, [f"Name: {self.current_file}",
                                    f"Status: {self.file_status}"])
        if header == "IMAGE LIST":
            used = sum(-(-len(v[2]) // BLOCK) * BLOCK for v in self.pool.values())
            return _render(header, [f"Capacity: {CAPACITY}",
                                    f"Available: {CAPACITY - 13287424 - used}"]
                           + list(self.pool))
        if header == "FRAME BUFFER":
            return _render(header, [f"{k}: {v}" for k, v in self.named.items()]
                           + ["Background Mix: 0", "Layer Mix: 0",
                              "Background Transition Duration: 0",
                              "Layer Transition Duration: 0"])
        return super().block(header)

    # -- 9996: the image write -------------------------------------------

    def start(self):
        super().start()
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                fake._media_session(self.request)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._media = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._media.serve_forever,
                         kwargs={"poll_interval": 0.02}, daemon=True).start()
        threading.Thread(target=self._stream, daemon=True).start()
        return self

    @property
    def media_port(self) -> int:
        return self._media.server_address[1]

    def stop(self) -> None:
        self._stop.set()
        if self._media:
            self._media.shutdown()
            self._media.server_close()
        super().stop()

    def _media_session(self, sock: socket.socket) -> None:
        def exact(n):
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    raise EOFError
                buf += chunk
            return buf
        try:
            op, = struct.unpack(">H", exact(2))
            name = exact(struct.unpack(">I", exact(4))[0]).decode()
            data = exact(struct.unpack(">I", exact(4))[0])
            tail = exact(1)
        except EOFError:
            return
        self.uploads.append(struct.pack(">H", op) + name.encode() + data + tail)
        if op != 0x0101 or tail != b"\0" or _size_of(data) is None:
            return sock.close()
        w, h = _size_of(data)
        with self.lock:
            self.pool[name] = (w, h, data)
            sock.sendall(b"\0")                       # one byte, as captured
            sock.close()
            self.broadcast(self.block("IMAGE LIST"))

    # -- 9998 ------------------------------------------------------------

    def _handle(self, sock, lines: List[str]) -> None:
        header = lines[0].strip().rstrip(":").upper()
        body = [ln for ln in lines[1:] if ln.strip()]
        items = [(k.strip(), v.strip()) for k, _, v in
                 (ln.partition(":") for ln in body)]
        if header == "IMAGE" and body:
            with self.lock:
                self.received.append("\n".join(lines))
                return self._image(sock, items)
        if header == "FRAME BUFFER" and body:
            with self.lock:
                self.received.append("\n".join(lines))
                return self._named_frame_buffer(sock, items)
        if header == "CONTROL" and items and items[0][0].upper() != "FLUSH SETTINGS":
            return self._control_4k(sock, lines, items)
        if header == "CONTROL" and items and items[0][0].upper() == "FLUSH SETTINGS":
            with self.lock:
                self.received.append("\n".join(lines))
                self._send(sock, "ACK\n\n")
                if not self.fg_locked:
                    return self._send(sock, _render(
                        "MESSAGE", ["Invalid operation - No Foreground source present"]))
                self.flushes += 1
                return
        return super()._handle(sock, lines)

    def _image(self, sock, items) -> None:
        kv = dict((k.lower(), v) for k, v in items)
        if "delete" not in kv:
            return self._send(sock, "NAK\n\n")
        self._send(sock, "ACK\n\n")
        name = kv["delete"]
        self.pool.pop(name, None)
        for k, v in self.named.items():
            if v == name:
                self.named[k] = ""
        self.broadcast(self.block("FRAME BUFFER"))
        self.broadcast(self.block("IMAGE LIST"))

    def _named_frame_buffer(self, sock, items) -> None:
        for key, value in items:
            if key not in self.named:
                return self._send(sock, "NAK\n\n")
        self._send(sock, "ACK\n\n")
        for key, value in items:
            if value and value in self.pool:
                w, h = self.pool[value][:2]
                if (w, h) != (1920, 1080):
                    return self._send(sock, _render("MESSAGE", [
                        f"Mismatched formats: Image format is {w}x{h} and "
                        f"video format is 1920x1080"]))
                if not self.fg_locked:
                    return self._send(sock, _render(
                        "MESSAGE", ["Invalid operation - No Foreground source present"]))
            self.named[key] = value if value in self.pool else ""
        self.broadcast(_render("FRAME BUFFER",
                               [f"{k}: {self.named[k]}" for k, _ in items]))
        for key, value in items:
            prefix = INPUT_OF.get(key)
            if not (prefix and self.named[key]):
                continue
            self.broadcast(self.block("CONTROL DEFAULT"))
            for name, v in ((f"{prefix} Input Offset", "10000"),
                            (f"{prefix} Input Level", "6878")):
                self.controls[name] = v
                self.broadcast(_render("CONTROL", [f"{name}: {v}"]))
            self.broadcast(self.block("DEVICE"))
            self.broadcast(self.block("CONTROL DEFAULT"))

    # -- CONTROL as the 12 4K did it (2026-11-12) -----------------------

    def _control_4k(self, sock, lines, items) -> None:
        names = [k[7:] if k.lower().startswith("offset ") else k for k, _ in items]
        for where in ("Wall", "Floor"):
            if f"Sample {where}".lower() in (n.lower() for n in names):
                with self.lock:
                    self.received.append("\n".join(lines))
                return self._sample(sock, where)
        with self.lock:
            for where, keys in CURSORS.items():
                if (any(n in keys for n in names) and self.controls.get(
                        f"{where} Cursor Position Enable") != "on"):
                    self.received.append("\n".join(lines))
                    self._send(sock, "ACK\n\n")
                    return self._send(sock, _render("MESSAGE", [LOCKED]))
            if len(items) == 1 and items[0][0] in (
                    "Wall Cursor Position Enable", "Floor Cursor Position Enable"):
                self.received.append("\n".join(lines))
                return self._cursor_enable(sock, *items[0])
            if any(n in self.silent_controls for n in names):
                self.received.append("\n".join(lines))
                return self._send(sock, "ACK\n\n")
            super()._handle(sock, lines)
            if self.file_status == "Clean":
                self.file_status = "Modified"
                self.broadcast(self.block("CURRENT FILE"))

    def _cursor_enable(self, sock, key, value) -> None:
        on = value.strip().lower() in ("on", "true", "yes")
        self._send(sock, "ACK\n\n")
        if not on:
            self.controls[key] = "off"
            return self.broadcast(_render("CONTROL", [f"{key}: off"]))
        where = key.split()[0]
        threading.Timer(self.cursor_echo_delay, self._cursor_on, (key, where)).start()

    def _cursor_on(self, key, where) -> None:
        with self.lock:
            self._push_colour(where)
            self.controls[key] = "on"
            self.broadcast(_render("CONTROL", [f"{key}: on"]))

    def _push_colour(self, where) -> None:
        for c, v in (("Blue", "2956"), ("Green", "3744"), ("Red", "5490")):
            self.broadcast(_render("CONTROL", [f"{where} Color {c}: {v}"]))

    def _stream(self) -> None:
        while not self._stop.wait(self.stream_interval):
            with self.lock:
                if self._sampling:
                    continue
                for where in CURSORS:
                    if self.controls.get(f"{where} Cursor Position Enable") == "on":
                        self._push_colour(where)

    def _sample(self, sock, where) -> None:
        # Runs in this client's handler thread, so whatever it sends
        # meanwhile waits, as on the unit.
        self._send(sock, "ACK\n\n")
        self._sampling = True
        try:
            time.sleep(self.sample_silence)
            with self.lock:
                key = f"{where} Cursor Position Enable"
                self.controls[key] = "off"
                self.broadcast(_render("CONTROL", [f"{key}: off"]))
                new = {"Floor Color Blue": "5958", "Floor Color Green": "6757",
                       "Floor Color Red": "8333"} if where == "Wall" else {}
                new.update({"Direct Light Blue": "10000",
                            "Direct Light Green": "3253",
                            "Direct Light Red": "10000"})
                for k, v in new.items():
                    self.controls[k] = v
                    self.broadcast(_render("CONTROL", [f"{k}: {v}"]))
                self.defaults.update(new)
                self.broadcast(self.block("CONTROL DEFAULT"))
        finally:
            self._sampling = False
