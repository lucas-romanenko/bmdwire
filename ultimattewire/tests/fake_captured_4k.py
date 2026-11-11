# SPDX-License-Identifier: MIT
"""A fake Ultimatte 12 4K that does what a real one (software 2.3) did in
two captures of Software Control on 2026-11-11, where that differs from
the document: FRAME BUFFER by image name (``Background 1: <image>``),
IMAGE LIST with ``Capacity:`` / ``Available:``, ``Image:`` /
``Delete:``, ``CONTROL:`` / ``FLUSH SETTINGS: YES``, and a 9996 listener
taking the 0x0101 image write.

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

    # -- the dump: the 12 4K's form of DEVICE, IMAGE LIST, FRAME BUFFER --

    def block(self, header: str) -> str:
        if header == "DEVICE":
            body = [f"Video Format: {self.video_format}",
                    "Reference Source: Not Locked",
                    f"FG In: {'Locked' if self.fg_locked else 'Not Locked'}",
                    "BG In: Not Locked"]
            return _render(header, body)
        if header == "IMAGE LIST":
            used = sum(len(v[2]) for v in self.pool.values())
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
        return self

    @property
    def media_port(self) -> int:
        return self._media.server_address[1]

    def stop(self) -> None:
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
        if op != 0x0101 or tail != b"\0":
            return sock.close()
        w, h = struct.unpack(">II", data[16:24])
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
