# SPDX-License-Identifier: MIT
"""A fake Ultimatte on TCP, speaking the text protocol as Blackmagic's
"Ultimatte 12 Ethernet Protocol" v2.0 (September 2022) describes it, and
nothing more.

It listens on 127.0.0.1 (an ephemeral port), sends the documented status
dump in the documented order on every connection, and answers every
documented block: status dump requests, CONTROL (set, Offset, functions,
the read-only Tally Active), FILE (Load, Save, Delete, Rename/To), GPI
(Insert/At, Remove, Index), FRAME BUFFER, CAMERACONTROL (HD Mini only)
and NETWORK INTERFACE n. ACK or NAK first, then the update, sent to every
connected client ("whether the change originated from the front panel, or
from any other connected client"). :meth:`FakeUltimatte.panel` changes
things as the front panel would.

Where the document is silent the fake picks something and says so:
- a value it won't take is a NAK (the document: NAK "indicate[s] that the
  request was not understood");
- a FILE or GPI command it can't carry out answers ACK then a MESSAGE
  block, as the document says (its message texts other than "Event limit
  exceeded" are made up here);
- values go out in lower case, as an Ultimatte 12 4K sends them.

Nothing here is the unit; it's the document, so the tests prove the client
follows the document, not that the document is right.
"""

from __future__ import annotations

import socket
import socketserver
import threading
from typing import Dict, List, Optional

from ultimattewire import controls as C

MODELS = {
    "8K": C.MODEL_8K, "4K": C.MODEL_4K, "HD": C.MODEL_HD,
    "HD Mini": C.MODEL_HD_MINI,
}

# DEVICE lines per model, from the document's note (the 8K example has all
# nine; the document spells the reference "REFIn" in its example).
DEVICE_INPUTS = {
    C.MODEL_HD_MINI: ["FG In", "BG In"],
    C.MODEL_HD: ["FG In", "BG In", "MONITOR In", "G MATTE In", "H MATTE In",
                 "REFIn"],
}
ALL_INPUTS = ["FG In", "BG In", "MONITOR In", "G MATTE In", "H MATTE In",
              "REFIn", "BG MATTE In", "LAYER In", "LAYER MATTE In"]

GPI_EVENT_LIMIT = 4         # the fake's; the document only names the warning


def _default(control: C.Control) -> str:
    if control.kind == C.RANGE:
        lo = control.minimum or 0
        return str(lo)
    if control.kind in (C.BOOL, C.READONLY):
        return "off"
    if control.kind == C.FUNCTION:
        return "no"
    return control.choices[0].lower()


class FakeUltimatte:
    """One fake unit. ``model``: ``"8K"``, ``"4K"``, ``"HD"`` or ``"HD Mini"``.

    ``start()`` / ``stop()``, or use it as a context manager. ``received``
    lists every block a client sent, as text."""

    def __init__(self, model: str = "8K", protocol: str = "2.0"):
        self.model = MODELS[model]
        self.protocol = protocol
        self.lock = threading.RLock()
        self.clients: List[socket.socket] = []
        self.received: List[str] = []
        self.label = f"{self.model}"
        self.video_format = "1080p60" if self.model != C.MODEL_8K else "2160p60"
        self.controls: Dict[str, str] = {}
        for c in C.CONTROLS:
            if not c.available_on(self.model):
                continue
            if c.min_protocol and tuple(int(x) for x in protocol.split(".")) < c.min_protocol:
                continue
            self.controls[c.name] = _default(c)
        self.controls["Matte Density"] = "5000"
        self.controls["Backing Color"] = "green"
        self.controls["Video Format"] = "auto detect"
        self.controls["Monitor Out"] = "program"
        self.defaults = dict(self.controls)
        self.current_file = "Ultimatte Defaults"
        self.files = ["Studio A", "Studio B"]
        self.gpis: Dict[int, dict] = (
            {} if self.model == C.MODEL_HD_MINI
            else {i: {"index": 0, "events": []} for i in range(1, 6)})
        if self.gpis:
            self.gpis[1]["events"] = ["Studio A", "Studio B"]
        self.images = ["Blue Sky", "Newsroom"]
        self.fb_count = 46
        self.fb = {}
        for name in ("BG 1", "LY 1", "BG 2", "LY 2", "GM", "HM"):
            self.fb[f"{name} Frame Buffer Enable"] = "off"
            self.fb[f"{name} Frame Buffer Index"] = "0"
        self.fb.update({"BG Frame Buffer Mix": "0", "LY Frame Buffer Mix": "0",
                        "BG Transition Duration": "0",
                        "LY Transition Duration": "0",
                        "Frame Buffer 1": "Blue Sky",
                        "Frame Buffer 2": "Newsroom"})
        self.camera_id = "1"
        self.inputs = DEVICE_INPUTS.get(self.model, ALL_INPUTS)
        self.close_on_connect = False
        self.stall_after: Optional[int] = None    # send this many prelude bytes, then hang
        self.mute = False                         # take blocks, answer nothing
        self.function_silent = False              # ACK a function, never say Yes
        self._server = None
        self._thread = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "FakeUltimatte":
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                fake._session(self.request)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.02},
                                        daemon=True)
        self._thread.start()
        return self

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def stop(self) -> None:
        with self.lock:
            for c in list(self.clients):
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                c.close()
            self.clients.clear()
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- the dump ----------------------------------------------------------

    def block(self, header: str) -> str:
        """The full status block ``header`` as the unit would send it."""
        h = header
        if h == "PROTOCOL PREAMBLE":
            body = [f"Version: {self.protocol}"]
        elif h == "IDENTITY":
            body = [f"Model: {self.model}", f"Label: {self.label}",
                    "Unique ID: 12345678"]
        elif h == "NETWORK":
            body = ["Interface count: 1", "Default Interface: 0",
                    "Static DNS Servers: 8.8.8.8, 8.8.4.4",
                    "Current DNS Servers: 8.8.8.8, 8.8.4.4"]
        elif h == "NETWORK INTERFACE 0":
            body = ["Name: Cadence GigE Ethernet MAC", "Priority: 0",
                    "MAC Address: 7c:2e:0d:00:00:01", "DynamicIP: false",
                    "Current Addresses: 10.0.0.2/255.255.255.0",
                    "Current Gateway: 10.0.0.1",
                    "Static Addresses: 10.0.0.2/255.255.255.0",
                    "Static Gateway: 10.0.0.1"]
        elif h == "VERSION":
            body = ["Product ID: BE84", "Hardware Version: 0100",
                    "Software Version: 09A89B7A",
                    f"Software Release: {self.protocol}"]
        elif h == "DEVICE":
            body = [f"Video Format: {self.video_format}",
                    "Reference Source: Foreground"]
            body += [f"{i}: Locked" for i in self.inputs]
        elif h == "VIDEO FORMATS":
            body = [f.lower() for f in C.video_formats_for(
                C.Context(family=self.model))]
        elif h == "CONTROL":
            body = [f"{k}: {v}" for k, v in self.controls.items()]
        elif h == "CONTROL DEFAULT":
            body = [f"{k}: {v}" for k, v in self.defaults.items()]
        elif h == "CURRENT FILE":
            body = [self.current_file]
        elif h == "FILE LIST":
            body = list(self.files)
        elif h == "GPI LIST":
            return "".join(self._gpi_block(i) for i in sorted(self.gpis))
        elif h == "IMAGE LIST":
            body = list(self.images)
        elif h == "FRAME BUFFER":
            body = [f"Number Of Frame Buffers: {self.fb_count}"]
            body += [f"{k}: {v}" for k, v in self.fb.items()]
        elif h == "CAMERACONTROL":
            body = [f"Camera Id: {self.camera_id}"]
        else:
            raise KeyError(h)
        return _render(h, body)

    def _gpi_block(self, i: int) -> str:
        g = self.gpis[i]
        return _render("GPI LIST", [f"ID: {i}", f"Index: {g['index']}"]
                       + list(g["events"]))

    def prelude(self) -> str:
        order = ["PROTOCOL PREAMBLE", "IDENTITY", "NETWORK",
                 "NETWORK INTERFACE 0", "VERSION", "DEVICE", "VIDEO FORMATS",
                 "CONTROL", "CONTROL DEFAULT", "CURRENT FILE", "FILE LIST"]
        if self.gpis:
            order.append("GPI LIST")
        order += ["IMAGE LIST", "FRAME BUFFER"]
        if self.model == C.MODEL_HD_MINI:
            order.append("CAMERACONTROL")
        return "".join(self.block(h) for h in order) + "END PRELUDE:\n\n"

    # -- sessions ----------------------------------------------------------

    def _session(self, sock: socket.socket) -> None:
        if self.close_on_connect:
            sock.close()
            return
        with self.lock:
            dump = self.prelude().encode()
            if self.stall_after is not None:
                sock.sendall(dump[:self.stall_after])
                threading.Event().wait(30)
                return
            sock.sendall(dump)
            self.clients.append(sock)
        buf = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk.replace(b"\r\n", b"\n")
                while b"\n\n" in buf:
                    raw, _, buf = buf.partition(b"\n\n")
                    text = raw.decode().lstrip("\n")
                    if text:
                        self._handle(sock, text.split("\n"))
        except OSError:
            pass
        finally:
            with self.lock:
                if sock in self.clients:
                    self.clients.remove(sock)

    def _send(self, sock, text: str) -> None:
        try:
            sock.sendall(text.encode())
        except OSError:
            pass

    def broadcast(self, text: str) -> None:
        with self.lock:
            for c in list(self.clients):
                self._send(c, text)

    def panel(self, header: str, items: Dict[str, str]) -> None:
        """A change made on the front panel: apply and push to everyone."""
        with self.lock:
            if header == "CONTROL":
                self.controls.update(items)
            elif header == "FRAME BUFFER":
                self.fb.update(items)
            self.broadcast(_render(header, [f"{k}: {v}" for k, v in items.items()]))

    # -- commands ----------------------------------------------------------

    def _handle(self, sock, lines: List[str]) -> None:
        with self.lock:
            self.received.append("\n".join(lines))
            if self.mute:
                return
            header = lines[0].strip()
            body = [ln for ln in lines[1:] if ln.strip()]
            if not header.endswith(":"):
                return self._send(sock, "NAK\n\n")
            name = header[:-1].strip().upper()
            if not body:
                return self._dump_request(sock, name)
            handler = {
                "CONTROL": self._control, "FILE": self._file, "GPI": self._gpi,
                "FRAME BUFFER": self._frame_buffer,
                "CAMERACONTROL": self._camera,
                "NETWORK INTERFACE 0": self._network,
            }.get(name)
            if handler is None:
                return self._send(sock, "NAK\n\n")
            handler(sock, _kv(body))

    def _dump_request(self, sock, name: str) -> None:
        try:
            if name == "CAMERACONTROL" and self.model != C.MODEL_HD_MINI:
                raise KeyError(name)
            if name == "GPI LIST" and not self.gpis:
                raise KeyError(name)
            text = self.block(name)
        except KeyError:
            return self._send(sock, "NAK\n\n")
        self._send(sock, "ACK\n\n" + text)

    def _control(self, sock, items: List[tuple]) -> None:
        changes = {}
        functions = []
        for key, value in items:
            offset = key.lower().startswith("offset ")
            name = key[7:] if offset else key
            control = C.lookup(name)
            if control is None or control.name not in self.controls:
                return self._send(sock, "NAK\n\n")
            if offset:
                if control.kind != C.RANGE:
                    return self._send(sock, "NAK\n\n")
                d = C.parse_int(value)
                if d is None:
                    return self._send(sock, "NAK\n\n")
                cur = int(self.controls[control.name])
                hi = control.maximum if control.maximum is not None else 10 ** 6
                changes[control.name] = str(max(control.minimum, min(hi, cur + d)))
                continue
            if control.kind == C.FUNCTION:
                if C.parse_bool(value) is not True:
                    return self._send(sock, "NAK\n\n")
                functions.append(control.name)
                continue
            try:
                text = C.format_value(control, value, C.Context(
                    family=self.model, video_format=self.video_format))
            except ValueError:
                return self._send(sock, "NAK\n\n")
            changes[control.name] = text
        self._send(sock, "ACK\n\n")
        if changes:
            self.controls.update(changes)
            self.broadcast(_render("CONTROL", [f"{k}: {v}" for k, v in changes.items()]))
        for f in functions:
            if f == "Factory Defaults":
                diff = {k: v for k, v in self.defaults.items()
                        if self.controls.get(k) != v}
                self.controls.update(diff)
                if diff:
                    self.broadcast(_render("CONTROL", [f"{k}: {v}" for k, v in diff.items()]))
            if not self.function_silent:
                self.broadcast(_render("CONTROL", [f"{f}: yes"]))

    def _file(self, sock, items: List[tuple]) -> None:
        kv = dict((k.lower(), v) for k, v in items)
        self._send(sock, "ACK\n\n")
        if "load" in kv:
            if kv["load"] not in self.files:
                return self._send(sock, _render("MESSAGE", ["Error: File not found"]))
            self.current_file = kv["load"]
            return self.broadcast(self.block("CURRENT FILE"))
        if "save" in kv:
            if kv["save"] not in self.files:
                self.files.append(kv["save"])
        elif "delete" in kv:
            if kv["delete"] not in self.files:
                return self._send(sock, _render("MESSAGE", ["Error: File not found"]))
            self.files.remove(kv["delete"])
        elif "rename" in kv and "to" in kv:
            if kv["rename"] not in self.files:
                return self._send(sock, _render("MESSAGE", ["Error: File not found"]))
            self.files[self.files.index(kv["rename"])] = kv["to"]
        else:
            return self._send(sock, _render("MESSAGE", ["Error: Bad file command"]))
        self.broadcast(self.block("FILE LIST"))

    def _gpi(self, sock, items: List[tuple]) -> None:
        kv = dict((k.lower(), v) for k, v in items)
        gid = C.parse_int(kv.get("id", ""))
        if not self.gpis or gid not in self.gpis:
            return self._send(sock, "NAK\n\n")
        g = self.gpis[gid]
        self._send(sock, "ACK\n\n")
        if "insert" in kv:
            if len(g["events"]) >= GPI_EVENT_LIMIT:
                return self._send(sock, _render("MESSAGE", ["Warning: Event limit exceeded"]))
            at = C.parse_int(kv.get("at", "-1"))
            if at is None or at < 0 or at > len(g["events"]):
                g["events"].append(kv["insert"])
            else:
                g["events"].insert(at, kv["insert"])
        elif "remove" in kv:
            n = C.parse_int(kv["remove"])
            if n == 0:
                g["events"] = []
                g["index"] = 0
            elif n is None or not 1 <= n <= len(g["events"]):
                return self._send(sock, _render("MESSAGE", ["Error: No such event"]))
            else:
                del g["events"][n - 1]
        elif "index" in kv:
            n = C.parse_int(kv["index"])
            if n is None or n < 0:
                return self._send(sock, _render("MESSAGE", ["Error: No such event"]))
            g["index"] = n
        self.broadcast(self._gpi_block(gid))

    def _frame_buffer(self, sock, items: List[tuple]) -> None:
        changes = {}
        for key, value in items:
            match = next((k for k in self.fb if k.lower() == key.lower()), None)
            if match is None or match.startswith("Frame Buffer "):
                return self._send(sock, "NAK\n\n")
            if match.endswith("Enable"):
                b = C.parse_bool(value)
                if b is None:
                    return self._send(sock, "NAK\n\n")
                value = "on" if b else "off"
            else:
                n = C.parse_int(value)
                if n is None or n < 0 or (match.endswith("Index") and n > self.fb_count):
                    return self._send(sock, "NAK\n\n")
                value = str(n)
            changes[match] = value
        self._send(sock, "ACK\n\n")
        self.fb.update(changes)
        self.broadcast(_render("FRAME BUFFER", [f"{k}: {v}" for k, v in changes.items()]))

    def _camera(self, sock, items: List[tuple]) -> None:
        kv = dict((k.lower(), v) for k, v in items)
        if self.model != C.MODEL_HD_MINI or C.parse_int(kv.get("camera id", "")) is None:
            return self._send(sock, "NAK\n\n")
        self.camera_id = str(C.parse_int(kv["camera id"]))
        self._send(sock, "ACK\n\n")
        self.broadcast(self.block("CAMERACONTROL"))

    def _network(self, sock, items: List[tuple]) -> None:
        # Only the echo; the network tests own the network behaviour.
        self._send(sock, "ACK\n\n" + _render("NETWORK INTERFACE 0",
                                            [f"{k}: {v}" for k, v in items]))


def _render(header: str, body: List[str]) -> str:
    return f"{header}:\n" + "".join(f"{ln}\n" for ln in body) + "\n"


def _kv(body: List[str]) -> List[tuple]:
    out = []
    for ln in body:
        k, sep, v = ln.partition(":")
        if sep:
            out.append((k.strip(), v.strip()))
    return out
