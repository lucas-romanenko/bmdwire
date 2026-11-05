# SPDX-License-Identifier: MIT
"""ultimattewire.client — the Ultimatte 12 Ethernet Protocol over TCP 9998.

Blackmagic documents this one: "Blackmagic Ultimatte 12 Ethernet
Protocol, Version 2.0" (Developer Information, September 2022). It is the
same block protocol as the Videohub's: a block is a CAPS header ending in
``:``, ``key: value`` (or bare) lines, and a blank line; the blank line is
what submits a block, in either direction::

    CONTROL:
    Matte Density: 100
    <blank>

On connect the unit sends its whole state (PROTOCOL PREAMBLE, IDENTITY,
NETWORK, NETWORK INTERFACE n, VERSION, DEVICE, VIDEO FORMATS, CONTROL,
CONTROL DEFAULT, CURRENT FILE, FILE LIST, GPI LIST per GPI, IMAGE LIST,
FRAME BUFFER, END PRELUDE), then pushes the changed items of a block
whenever anything changes, from the front panel or any other client. A
block the client sends gets ``ACK`` or ``NAK`` (each a block of its own),
then the update; files and GPI events can get a ``MESSAGE:`` block
instead.

This client is synchronous and single-socket, like videohubwire's: no
thread, no timer. Nothing happens on the wire unless a method is called,
and pushed updates are read (and handed to subscribers) whenever the
client reads, or when the caller asks with :meth:`Ultimatte.poll`::

    with Ultimatte('192.0.2.21') as u:
        print(u.state.identity.model, u.state.controls['Matte Density'])
        u.subscribe(lambda upd: print(upd.header, upd.changes))
        u.set_control('Matte Density', 4200)    # waits for the unit's echo
        u.poll(1.0)                              # anything else that changed

Every wait is a no-progress wait: it ends when the unit has sent nothing
for ``idle_timeout`` seconds, however long the unit keeps talking before
that. Errors are named (:class:`UltimatteConnectionError`,
:class:`UltimatteTimeout`, :class:`UltimatteNak`,
:class:`UltimatteMessageError`; bad values are refused before they reach
the wire with :class:`~ultimattewire.controls.ControlValueError` or
:class:`~ultimattewire.controls.UnsupportedError`).

What is verified on hardware and what is only the document is in the
README ("The text protocol (9998)"). Where an Ultimatte 12 4K was seen to
differ from the document (CURRENT FILE as ``Name:``/``Status:`` lines,
``Dynamic IP`` with a space, values in lower case) both forms are read.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ultimattewire import controls as C
from ultimattewire._errors import UltimatteError
from ultimattewire._protocol import CONTROL_PORT
from ultimattewire.network import NetworkInterface, parse_interface

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 5.0
# The longest the unit may stay silent while we wait on it: during the
# prelude, for an ACK, for an update. Counted from the last byte received,
# never from the start of the wait.
DEFAULT_IDLE_TIMEOUT = 3.0
# Functions (Auto Screen Sample, Factory Defaults, ...) answer "Yes" when
# done; how long the unit may stay silent before that.
DEFAULT_FUNCTION_IDLE_TIMEOUT = 10.0
CONNECT_RETRY_DELAY_S = 0.25        # one retry: a cold unit drops the first SYN

PRELUDE_END = "END PRELUDE:"
_BLOCK_END = re.compile(rb"\n\r?\n")
_END_PRELUDE_LINE = PRELUDE_END.encode()

# Frame buffers named by the document's FRAME BUFFER example.
FRAME_BUFFERS = ("BG 1", "LY 1", "BG 2", "LY 2", "GM", "HM")
FRAME_BUFFER_LAYERS = ("BG", "LY")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class UltimatteConnectionError(UltimatteError, ConnectionError):
    """No session: the unit refused the connection, or closed it."""


class UltimatteTimeout(UltimatteError, TimeoutError):
    """The unit went quiet for ``idle_timeout`` while it owed us something."""


class UltimatteNak(UltimatteError):
    """The unit answered NAK: "the request was not understood"."""

    def __init__(self, block: str):
        self.block = block
        super().__init__(f"unit refused the block (NAK):\n{block.strip()}")


class UltimatteMessageError(UltimatteError):
    """The unit answered a FILE or GPI command with a MESSAGE block
    (``Warning: Event limit exceeded``) instead of the update."""

    def __init__(self, message: Dict[str, str], block: str):
        self.message = message
        self.block = block
        text = "; ".join(f"{k}: {v}" if k else v for k, v in message.items())
        super().__init__(f"unit answered with a message: {text}")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

@dataclass
class Identity:
    model: str = ""
    label: str = ""
    unique_id: str = ""


@dataclass
class VersionInfo:
    product_id: str = ""
    hardware_version: str = ""
    software_version: str = ""
    software_release: str = ""


@dataclass
class DeviceInfo:
    """The DEVICE block: the format the unit is RUNNING (the CONTROL
    block's ``Video Format`` is the setting, often ``auto detect``), the
    reference, and each input's lock state as sent (``Locked``, ...)."""
    video_format: str = ""
    reference_source: str = ""
    inputs: Dict[str, str] = field(default_factory=dict)

    def input_status(self, name: str) -> Optional[str]:
        """The lock state of input ``name`` (``"FG"``, ``"FG In"``,
        ``"REF"``; the document spells it ``REFIn`` and ``REF In``)."""
        want = _input_key(name)
        for k, v in self.inputs.items():
            if _input_key(k) == want:
                return v
        return None

    def locked(self, name: str) -> Optional[bool]:
        s = self.input_status(name)
        return None if s is None else s.strip().lower() == "locked"


def _input_key(name: str) -> str:
    k = re.sub(r"\s+", "", name or "").lower()
    return k[:-2] if k.endswith("in") else k


@dataclass
class CurrentFile:
    """CURRENT FILE. The document shows one bare line, the file name; an
    Ultimatte 12 4K on 2.3 sends ``Name:`` and ``Status:`` (``Clean``, or
    ``Modified`` once settings changed on the unit and weren't saved)."""
    name: str = ""
    status: str = ""

    @property
    def modified(self) -> Optional[bool]:
        if not self.status:
            return None
        return self.status.strip().lower() == "modified"


@dataclass
class GpiList:
    """One GPI LIST block: GPI ``id``, its current event ``index``, and its
    events (file names) in order."""
    id: int
    index: Optional[int] = None
    events: List[str] = field(default_factory=list)


@dataclass
class FrameBuffer:
    name: str
    enable: Optional[bool] = None
    index: Optional[int] = None


@dataclass
class FrameBufferState:
    """FRAME BUFFER. ``buffers`` by name (``BG 1``, ``LY 1``, ``BG 2``,
    ``LY 2``, ``GM``, ``HM``); ``mix`` and ``duration`` (milliseconds) by
    layer (``BG``, ``LY``); ``images`` maps a frame buffer number to the
    image loaded in it (``Frame Buffer 1: Image 1``)."""
    count: Optional[int] = None
    buffers: Dict[str, FrameBuffer] = field(default_factory=dict)
    mix: Dict[str, int] = field(default_factory=dict)
    duration: Dict[str, int] = field(default_factory=dict)
    images: Dict[int, str] = field(default_factory=dict)
    other: Dict[str, str] = field(default_factory=dict)

    def image_number(self, image: str) -> Optional[int]:
        """The frame buffer number holding ``image`` (exact name, then any
        case), or None."""
        for n, name in sorted(self.images.items()):
            if name == image:
                return n
        for n, name in sorted(self.images.items()):
            if name.lower() == (image or "").lower():
                return n
        return None


@dataclass
class UltimatteState:
    """Everything the unit has told this session, kept current by its
    pushes. Values are typed by :mod:`ultimattewire.controls`; a value or
    a control the table doesn't know stays the string the unit sent."""
    protocol_version: str = ""
    identity: Identity = field(default_factory=Identity)
    network: Dict[str, str] = field(default_factory=dict)
    interfaces: Dict[int, NetworkInterface] = field(default_factory=dict)
    version: VersionInfo = field(default_factory=VersionInfo)
    device: DeviceInfo = field(default_factory=DeviceInfo)
    video_formats: List[str] = field(default_factory=list)
    controls: Dict[str, object] = field(default_factory=dict)
    control_defaults: Dict[str, object] = field(default_factory=dict)
    current_file: CurrentFile = field(default_factory=CurrentFile)
    file_list: List[str] = field(default_factory=list)
    gpi_lists: Dict[int, GpiList] = field(default_factory=dict)
    image_list: List[str] = field(default_factory=list)
    frame_buffer: FrameBufferState = field(default_factory=FrameBufferState)
    camera_id: Optional[int] = None
    messages: List[Dict[str, str]] = field(default_factory=list)
    other: Dict[str, List[str]] = field(default_factory=dict)
    prelude_complete: bool = False

    @property
    def family(self) -> str:
        """The model as a :mod:`ultimattewire.controls` MODEL_* constant
        (``""`` if the IDENTITY model isn't one the document names)."""
        return C.model_family(self.identity.model)

    @property
    def protocol(self) -> Optional[Tuple[int, int]]:
        m = re.match(r"\s*(\d+)\.(\d+)", self.protocol_version or "")
        return (int(m.group(1)), int(m.group(2))) if m else None

    @property
    def tally(self) -> Optional[bool]:
        """``Tally Active`` (read only): is the unit's output on air."""
        v = self.controls.get("Tally Active")
        return v if isinstance(v, bool) else None

    def to_dict(self) -> dict:
        """Plain data (JSON-ready) for a UI."""
        d = asdict(self)
        d["family"] = self.family
        d["tally"] = self.tally
        d["interfaces"] = {str(k): {kk: vv for kk, vv in v.items() if kk != "raw"}
                           for k, v in d["interfaces"].items()}
        d["gpi_lists"] = {str(k): v for k, v in d["gpi_lists"].items()}
        d["frame_buffer"]["images"] = {
            str(k): v for k, v in d["frame_buffer"]["images"].items()}
        return d


@dataclass
class Update:
    """One block the unit sent, as handed to subscribers.

    ``changes`` is what the block said, typed: for CONTROL / CONTROL
    DEFAULT / FRAME BUFFER / DEVICE and the other key-value blocks a dict
    of the items in the block; for FILE LIST / IMAGE LIST / VIDEO FORMATS
    the new list; for GPI LIST the :class:`GpiList`; for MESSAGE the
    message's items."""
    header: str
    changes: object
    lines: List[str]
    state: UltimatteState


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class Ultimatte:
    """One session on an Ultimatte's TCP 9998. Context manager::

        with Ultimatte(ip) as u:
            ...

    ``socket_factory`` is a hook for tests, with the signature of
    :func:`socket.create_connection`.
    """

    def __init__(self, host: str, port: int = CONTROL_PORT, *,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
                 function_idle_timeout: float = DEFAULT_FUNCTION_IDLE_TIMEOUT,
                 socket_factory: Optional[Callable] = None):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.function_idle_timeout = function_idle_timeout
        self._socket_factory = socket_factory or socket.create_connection
        self._sock = None
        self._buf = b""
        self._subscribers: List[Tuple[Callable, Optional[frozenset]]] = []
        self.state = UltimatteState()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> "Ultimatte":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> UltimatteState:
        """Open the session and read the unit's state up to END PRELUDE."""
        if self._sock is not None:
            self.close()
        self.state = UltimatteState()
        self._buf = b""
        where = f"{self.host}:{self.port}"
        try:
            try:
                self._sock = self._socket_factory(
                    (self.host, self.port), timeout=self.connect_timeout)
            except OSError:
                time.sleep(CONNECT_RETRY_DELAY_S)
                self._sock = self._socket_factory(
                    (self.host, self.port), timeout=self.connect_timeout)
        except ConnectionRefusedError as e:
            raise UltimatteConnectionError(
                f"{where} refused the connection (nothing listening on the "
                f"Ultimatte text port: wrong address, or not an Ultimatte)") from e
        except socket.timeout as e:
            raise UltimatteConnectionError(
                f"{where} did not answer within {self.connect_timeout:g}s "
                f"(off, unplugged, or not reachable from here)") from e
        except OSError as e:
            raise UltimatteConnectionError(f"{where}: {e}") from e
        try:
            self._read_prelude()
        except Exception:
            self.close()
            raise
        return self.state

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    def _read_prelude(self) -> None:
        got_any = False
        while True:
            try:
                block = self._recv_block(self.idle_timeout)
            except UltimatteConnectionError:
                if got_any:
                    raise UltimatteConnectionError(
                        f"{self.host}:{self.port} closed the connection in the "
                        f"middle of its status dump (before END PRELUDE)")
                raise UltimatteConnectionError(
                    f"{self.host}:{self.port} accepted the connection and "
                    f"closed it without a word (no free connection on the "
                    f"unit, or not an Ultimatte)")
            if block is None:
                raise UltimatteTimeout(
                    f"{self.host}:{self.port}: status dump stalled, nothing "
                    f"for {self.idle_timeout:g}s before END PRELUDE"
                    + ("" if got_any else " (nothing at all: not an Ultimatte?)"))
            got_any = True
            header = self._apply(block)
            if header == PRELUDE_END:
                self.state.prelude_complete = True
                return

    # ------------------------------------------------------------------
    # Subscriptions and pushed updates
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[Update], None],
                  headers: Optional[Iterable[str]] = None) -> Callable[[], None]:
        """Call ``callback(update)`` for every block the unit sends from now
        on (only blocks with one of ``headers``, given without the colon,
        if set). Returns a function that unsubscribes.

        Callbacks run in the caller's thread, inside whichever method is
        reading at the time (:meth:`poll`, or a command waiting for its
        answer); there is no thread of our own. An exception in a callback
        is logged, not raised, so it can't break a command half way."""
        want = (frozenset(h.rstrip(":").strip().upper() for h in headers)
                if headers is not None else None)
        entry = (callback, want)
        self._subscribers.append(entry)

        def unsubscribe():
            try:
                self._subscribers.remove(entry)
            except ValueError:
                pass
        return unsubscribe

    def poll(self, timeout: float = 0.0) -> List[Update]:
        """Read whatever the unit pushes: wait up to ``timeout`` seconds of
        silence, apply every block that arrives (subscribers are called)
        and return them. ``poll(0)`` takes only what is already waiting."""
        self._require()
        out: List[Update] = []
        while True:
            block = self._recv_block(0.0 if out else max(timeout, 0.0))
            if block is None:
                return out
            header = self._apply(block)
            out.append(self._last_update or Update(header, None, block, self.state))

    def wait_for(self, predicate: Callable[[UltimatteState], bool],
                 idle_timeout: Optional[float] = None) -> UltimatteState:
        """Read pushes until ``predicate(state)`` holds; raise
        :class:`UltimatteTimeout` if the unit goes quiet first. For checking
        after a change that the unit really holds it."""
        self._require()
        idle = self.idle_timeout if idle_timeout is None else idle_timeout
        while not predicate(self.state):
            block = self._recv_block(idle)
            if block is None:
                raise UltimatteTimeout(
                    f"{self.host}: the expected state did not arrive "
                    f"(nothing from the unit for {idle:g}s)")
            self._apply(block)
        return self.state

    # ------------------------------------------------------------------
    # Status dumps
    # ------------------------------------------------------------------

    def refresh(self, header: str) -> List[str]:
        """Ask the unit to resend a whole block (``refresh('CONTROL')``,
        ``refresh('NETWORK INTERFACE 0')``) and return its lines once it
        has been applied. A header with several blocks (GPI LIST) returns
        the first; the rest are applied as they arrive."""
        header = header.rstrip(":").strip().upper()
        return self._command(header, [], expect=lambda h, b: h == header + ":")

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------

    def get_control(self, name: str):
        """A control's current value from state (no wire traffic)."""
        return self.state.controls.get(self._wire_name(name))

    def set_control(self, name: str, value, *, validate: bool = True,
                    wait: bool = True):
        """Set one control and return the value the unit reports back.

        ``value`` is checked against the document's range or choices, the
        model's footnotes and the protocol version before anything is sent
        (``validate=False`` sends it as given). With ``wait`` (the default)
        this returns once the unit's CONTROL update carrying the control
        arrives; the document says to trust that update, not the ACK."""
        return self.set_controls({name: value}, validate=validate,
                                 wait=wait)[self._wire_name(name)]

    def set_controls(self, values: Dict[str, object], *, validate: bool = True,
                     wait: bool = True) -> Dict[str, object]:
        """Set several controls in one CONTROL block. Every value is checked
        first, so a bad one sends nothing. Returns the values as the unit
        reported them (as sent, when ``wait`` is False)."""
        if not values:
            raise ValueError("nothing to set")
        lines = []
        names = []
        for name, value in values.items():
            wire = self._wire_name(name)
            text = self._format(wire, value, validate)
            lines.append(f"{wire}: {text}")
            names.append(wire)
        if not wait:
            self._command("CONTROL", lines)
            return {n: C.parse_value(n, ln.partition(": ")[2])
                    for n, ln in zip(names, lines)}
        seen = set()

        def done(h, block):
            if h == "CONTROL:":
                for ln in block[1:]:
                    k = ln.partition(":")[0].strip().lower()
                    seen.add(k)
            return all(n.lower() in seen for n in names)
        self._command("CONTROL", lines, expect=done)
        return {n: self.state.controls.get(n) for n in names}

    def offset_control(self, name: str, delta: int, *, validate: bool = True,
                       wait: bool = True):
        """Change a numeric control by ``delta`` (``Offset <name>: <delta>``)
        and return the value the unit reports. "Only controls with
        numerical ranges support this relative mode." """
        wire = self._wire_name(name)
        control = C.lookup(wire)
        if validate:
            if control is None:
                raise C.ControlValueError(f"no control {name!r} in the document")
            text = C.format_offset(control, delta, self.context())
        else:
            text = str(delta)
        line = f"Offset {wire}: {text}"
        if not wait:
            self._command("CONTROL", [line])
            return None
        self._command("CONTROL", [line], expect=_has_key("CONTROL:", wire))
        return self.state.controls.get(wire)

    def run_function(self, name: str, *, idle_timeout: Optional[float] = None):
        """Run a function control (``Factory Defaults``, ``Auto Screen
        Sample``, ``Matte Reset``, ...): send ``yes``, and return once the
        unit answers ``yes`` ("to indicate the function is complete")."""
        wire = self._wire_name(name)
        control = C.lookup(wire)
        if control is None or control.kind != C.FUNCTION:
            raise C.ControlValueError(f"{name!r} is not a function control")
        C.format_value(control, True, self.context())

        def done(h, block):
            if h != "CONTROL:":
                return False
            for ln in block[1:]:
                k, _, v = ln.partition(":")
                if k.strip().lower() == wire.lower() and C.parse_bool(v):
                    return True
            return False
        self._command("CONTROL", [f"{wire}: yes"], expect=done,
                      idle_timeout=(self.function_idle_timeout
                                    if idle_timeout is None else idle_timeout))

    def _wire_name(self, name: str) -> str:
        """The name as the unit spells it: the unit's own key if it sent
        one, else the document's."""
        key = " ".join((name or "").split())
        for k in self.state.controls:
            if k.lower() == key.lower():
                return k
        control = C.lookup(key)
        return control.name if control else key

    def _format(self, wire: str, value, validate: bool) -> str:
        control = C.lookup(wire)
        if not validate or control is None:
            if control is None and validate and wire not in self.state.controls:
                raise C.ControlValueError(
                    f"no control {wire!r} in the document or on this unit "
                    f"(validate=False sends it anyway)")
            if isinstance(value, bool):
                return "on" if value else "off"
            text = str(value)
            if "\n" in text or "\r" in text:
                raise C.ControlValueError("a value can't contain a line break")
            return text
        return C.format_value(control, value, self.context())

    def context(self) -> C.Context:
        """What value checks know about this unit: model, protocol, the
        running format, its VIDEO FORMATS list."""
        s = self.state
        return C.Context(family=s.family, protocol=s.protocol,
                         video_format=s.device.video_format,
                         video_formats=tuple(s.video_formats))

    # ------------------------------------------------------------------
    # Files (presets)
    # ------------------------------------------------------------------

    def load_file(self, name: str) -> CurrentFile:
        """Load a saved file (preset). Returns CURRENT FILE as the unit
        reports it after; a MESSAGE answer raises UltimatteMessageError."""
        self._command("FILE", [f"Load: {_one_line(name)}"],
                      expect=lambda h, b: h == "CURRENT FILE:", messages=True)
        return self.state.current_file

    def save_file(self, name: str) -> List[str]:
        """Save the live settings as file ``name``; returns the FILE LIST."""
        return self._file_op([f"Save: {_one_line(name)}"])

    def delete_file(self, name: str) -> List[str]:
        return self._file_op([f"Delete: {_one_line(name)}"])

    def rename_file(self, name: str, new_name: str) -> List[str]:
        return self._file_op([f"Rename: {_one_line(name)}",
                              f"To: {_one_line(new_name)}"])

    def _file_op(self, lines: List[str]) -> List[str]:
        self._command("FILE", lines, expect=lambda h, b: h == "FILE LIST:",
                      messages=True)
        return self.state.file_list

    # ------------------------------------------------------------------
    # GPI event lists
    # ------------------------------------------------------------------

    def gpi_insert(self, gpi: int, filename: str, at: int = -1) -> GpiList:
        """Insert ``filename`` into GPI ``gpi``'s event list at index
        ``at`` (-1: the end). Returns the GPI LIST the unit sends after."""
        at = int(at)
        if at < -1:
            raise C.ControlValueError("At is an index, or -1 for the end")
        return self._gpi(gpi, [f"Insert: {_one_line(filename)}", f"At: {at}"])

    def gpi_remove(self, gpi: int, index: int) -> GpiList:
        """Remove event ``index`` from GPI ``gpi``'s list; 0 removes all
        ("A '0' will delete all events")."""
        index = int(index)
        if index < 0:
            raise C.ControlValueError("Remove takes an event index, or 0 for all")
        return self._gpi(gpi, [f"Remove: {index}"])

    def gpi_set_index(self, gpi: int, index: int) -> GpiList:
        """Set GPI ``gpi``'s current event index."""
        index = int(index)
        if index < 0:
            raise C.ControlValueError("an event index can't be negative")
        return self._gpi(gpi, [f"Index: {index}"])

    def _gpi(self, gpi: int, lines: List[str]) -> GpiList:
        family = self.state.family
        if not C.has_gpi(family):
            raise C.UnsupportedError(
                f"the {family} has no GPI inputs (no GPI LIST)")
        gpi = int(gpi)
        if gpi < 1:
            raise C.ControlValueError("GPI IDs start at 1")
        self._command("GPI", [f"ID: {gpi}"] + lines,
                      expect=lambda h, b: h == "GPI LIST:" and _gpi_id(b) == gpi,
                      messages=True)
        return self.state.gpi_lists.get(gpi, GpiList(gpi))

    # ------------------------------------------------------------------
    # Frame buffers (media pool stills on the BG / LY / GM / HM inputs)
    # ------------------------------------------------------------------

    def set_frame_buffer(self, buffer: str, *, index: Optional[int] = None,
                         enable: Optional[bool] = None) -> FrameBuffer:
        """Assign frame buffer ``index`` (an image slot, see
        ``state.frame_buffer.images``) to ``buffer`` (``BG 1``, ``LY 1``,
        ``BG 2``, ``LY 2``, ``GM``, ``HM``) and/or enable it. One block,
        index first, as the document's example."""
        name = self._buffer_name(buffer)
        lines = []
        if index is not None:
            index = int(index)
            count = self.state.frame_buffer.count
            if index < 0 or (count is not None and index > count):
                raise C.ControlValueError(
                    f"frame buffer index {index} outside 0-{count}")
            lines.append(f"{name} Frame Buffer Index: {index}")
        if enable is not None:
            lines.append(f"{name} Frame Buffer Enable: {'on' if enable else 'off'}")
        if not lines:
            raise ValueError("nothing to set")
        keys = [ln.partition(":")[0] for ln in lines]
        self._command("FRAME BUFFER", lines,
                      expect=_has_keys("FRAME BUFFER:", keys))
        return self.state.frame_buffer.buffers.get(name, FrameBuffer(name))

    def assign_image(self, buffer: str, image: str,
                     enable: bool = True) -> FrameBuffer:
        """Put the image called ``image`` (as listed in FRAME BUFFER's
        ``Frame Buffer n:`` lines) on ``buffer`` and enable it."""
        n = self.state.frame_buffer.image_number(image)
        if n is None:
            raise C.ControlValueError(
                f"no frame buffer holds an image called {image!r}; loading "
                f"images onto the unit isn't in the text protocol")
        return self.set_frame_buffer(buffer, index=n, enable=enable)

    def set_frame_buffer_mix(self, layer: str, value: int) -> Optional[int]:
        """``BG Frame Buffer Mix`` / ``LY Frame Buffer Mix``. The document
        gives no range; only a negative value is refused here."""
        return self._fb_layer(layer, "Frame Buffer Mix", value, "mix")

    def set_transition_duration(self, layer: str, ms: int) -> Optional[int]:
        """``BG Transition Duration`` / ``LY Transition Duration``, in
        milliseconds."""
        return self._fb_layer(layer, "Transition Duration", ms, "duration")

    def _fb_layer(self, layer, what, value, attr):
        layer = (layer or "").strip().upper()
        if layer not in FRAME_BUFFER_LAYERS:
            raise C.ControlValueError(
                f"frame buffer layer is BG or LY, not {layer!r}")
        if isinstance(value, bool) or int(value) < 0:
            raise C.ControlValueError(f"{layer} {what} can't be {value!r}")
        key = f"{layer} {what}"
        self._command("FRAME BUFFER", [f"{key}: {int(value)}"],
                      expect=_has_key("FRAME BUFFER:", key))
        return getattr(self.state.frame_buffer, attr).get(layer)

    def _buffer_name(self, buffer: str) -> str:
        b = " ".join((buffer or "").split()).upper()
        known = set(FRAME_BUFFERS) | set(self.state.frame_buffer.buffers)
        for k in known:
            if k.upper() == b:
                return k
        raise C.ControlValueError(
            f"no frame buffer {buffer!r} (the document's are "
            f"{', '.join(FRAME_BUFFERS)})")

    # ------------------------------------------------------------------
    # Camera control (HD Mini)
    # ------------------------------------------------------------------

    def set_camera_id(self, camera_id: int) -> Optional[int]:
        """CAMERACONTROL ``Camera Id``: the camera this unit's SDI camera
        control addresses. "only available on the Ultimatte 12 HD Mini"."""
        family = self.state.family
        if family and not C.has_camera_control(family):
            raise C.UnsupportedError(
                f"CAMERACONTROL is only on the {C.MODEL_HD_MINI}, "
                f"not the {family}")
        if isinstance(camera_id, bool) or int(camera_id) < 0:
            raise C.ControlValueError(f"camera id can't be {camera_id!r}")
        self._command("CAMERACONTROL", [f"Camera Id: {int(camera_id)}"],
                      expect=_has_key("CAMERACONTROL:", "Camera Id"))
        return self.state.camera_id

    # ------------------------------------------------------------------
    # Raw blocks
    # ------------------------------------------------------------------

    def send_block(self, header: str, lines: Iterable[str] = (), *,
                   expect: Optional[str] = None) -> Optional[List[str]]:
        """Send any block and wait for ACK (and, with ``expect``, for the
        first block with that header). For what the document doesn't list."""
        header = header.rstrip(":").strip().upper()
        want = None if expect is None else expect.rstrip(":").strip().upper() + ":"
        return self._command(header, [_one_line(ln) for ln in lines],
                             expect=None if want is None
                             else (lambda h, b: h == want))

    # ------------------------------------------------------------------
    # Request / response
    # ------------------------------------------------------------------

    def _command(self, header: str, lines: List[str], *,
                 expect: Optional[Callable[[str, List[str]], bool]] = None,
                 messages: bool = False,
                 idle_timeout: Optional[float] = None) -> Optional[List[str]]:
        """Send ``header`` + ``lines`` + blank line; wait for ACK/NAK, then
        (if ``expect``) for the block ``expect(header, lines)`` accepts.
        Every other block on the way is applied. With ``messages``, a
        MESSAGE block in place of the expected one raises."""
        self._require()
        text = header + ":\n" + "".join(ln + "\n" for ln in lines) + "\n"
        idle = self.idle_timeout if idle_timeout is None else idle_timeout
        try:
            self._sock.settimeout(max(idle, 0.001))
            self._sock.sendall(text.encode("utf-8"))
        except OSError as e:
            self.close()
            raise UltimatteConnectionError(
                f"{self.host}: connection lost sending {header} ({e})") from e
        acked = False
        matched = None
        while True:
            block = self._recv_block(idle)
            if block is None:
                # A late ACK would be taken as the next command's: the
                # session can't be trusted any more, so it ends here.
                self.close()
                what = "ACK" if not acked else "update after the ACK"
                raise UltimatteTimeout(
                    f"{self.host}: no {what} for {header} (nothing from the "
                    f"unit for {idle:g}s); session closed, connect again")
            h = block[0].strip()
            if h == "ACK":
                acked = True
            elif h == "NAK":
                raise UltimatteNak(text)
            else:
                self._apply(block)
                if h == "MESSAGE:" and messages and acked:
                    raise UltimatteMessageError(_kv_dict(block[1:]), text)
                if expect is not None and matched is None and expect(h, block):
                    matched = block
            if acked and (expect is None or matched is not None):
                return matched

    def _require(self) -> None:
        if self._sock is None:
            raise UltimatteConnectionError("not connected")

    # ------------------------------------------------------------------
    # Wire
    # ------------------------------------------------------------------

    def _recv_block(self, idle: float) -> Optional[List[str]]:
        """One blank-line-terminated block (lines, header first), or None
        once the unit has been silent for ``idle`` seconds. Bytes arriving
        restart the wait: a long dump that keeps coming never times out.
        EOF raises UltimatteConnectionError."""
        while True:
            self._buf = self._buf.lstrip(b"\r\n")     # blank lines between blocks
            if self._buf.startswith(_END_PRELUDE_LINE):
                # The document ends END PRELUDE with a blank line; don't
                # depend on it, so a unit that sends only the line ends the
                # dump too (the older readers here never needed it).
                nl = self._buf.find(b"\n")
                if nl >= 0:
                    self._buf = self._buf[nl + 1:]
                    return [PRELUDE_END]
            m = _BLOCK_END.search(self._buf)
            if m is not None:
                raw, self._buf = self._buf[:m.start()], self._buf[m.end():]
                lines = raw.decode("utf-8", "replace").split("\n")
                return [ln.rstrip("\r") for ln in lines]
            # idle 0: take only what is already here (non-blocking)
            self._sock.settimeout(idle if idle > 0 else 0.0)
            try:
                chunk = self._sock.recv(65536)
            except (socket.timeout, BlockingIOError):
                return None
            except OSError as e:
                self.close()
                raise UltimatteConnectionError(
                    f"{self.host}: connection lost ({e})") from e
            if not chunk:
                self.close()
                raise UltimatteConnectionError(
                    f"{self.host}: the unit closed the connection")
            self._buf += chunk

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    _last_update: Optional[Update] = None

    def _apply(self, lines: List[str]) -> str:
        """Apply one block to state, tell subscribers; returns the header."""
        header = lines[0].strip()
        body = [ln for ln in lines[1:] if ln.strip()]
        name = header[:-1] if header.endswith(":") else header
        changes = self._apply_body(name.upper(), header, body)
        update = Update(header, changes, lines, self.state)
        self._last_update = update
        for callback, want in list(self._subscribers):
            if want is not None and name.upper() not in want:
                continue
            try:
                callback(update)
            except Exception:                                    # noqa: BLE001
                logger.exception("ultimattewire: subscriber failed on %s", header)
        return header

    def _apply_body(self, name: str, header: str, body: List[str]):
        s = self.state
        if name == "PROTOCOL PREAMBLE":
            kv = _kv_dict(body)
            s.protocol_version = kv.get("Version", s.protocol_version)
            return kv
        if name == "IDENTITY":
            kv = _kv_dict(body)
            _set_attrs(s.identity, kv, {"Model": "model", "Label": "label",
                                        "Unique ID": "unique_id"})
            return kv
        if name == "VERSION":
            kv = _kv_dict(body)
            _set_attrs(s.version, kv, {"Product ID": "product_id",
                                       "Hardware Version": "hardware_version",
                                       "Software Version": "software_version",
                                       "Software Release": "software_release"})
            return kv
        if name == "NETWORK":
            kv = _kv_dict(body)
            s.network.update(kv)
            return kv
        m = re.match(r"NETWORK INTERFACE (\d+)$", name)
        if m:
            index = int(m.group(1))
            kv = _kv_dict(body)
            iface = s.interfaces.get(index)
            raw = ((iface.raw.split("\n", 1)[1] if iface and iface.raw else "")
                   if iface else "")
            # a push carries only what changed: merge into what we have
            merged = _merge_kv_text(raw, body)
            s.interfaces[index] = parse_interface(f"{header}\n{merged}", index)
            return kv
        if name == "DEVICE":
            kv = _kv_dict(body)
            for k, v in kv.items():
                if k.lower() == "video format":
                    s.device.video_format = v
                elif k.lower() == "reference source":
                    s.device.reference_source = v
                else:
                    s.device.inputs[k] = v
            return kv
        if name == "VIDEO FORMATS":
            s.video_formats = [ln.strip() for ln in body]
            return list(s.video_formats)
        if name in ("CONTROL", "CONTROL DEFAULT"):
            target = s.controls if name == "CONTROL" else s.control_defaults
            changes = {}
            for k, v in _kv_pairs(body):
                changes[k] = target[k] = C.parse_value(k, v)
            return changes
        if name == "CURRENT FILE":
            kv = _kv_dict(body)
            if "Name" in kv or "Status" in kv:
                if "Name" in kv:
                    s.current_file.name = kv["Name"]
                if "Status" in kv:
                    s.current_file.status = kv["Status"]
            elif body:
                s.current_file = CurrentFile(name=body[0].strip())
            return CurrentFile(s.current_file.name, s.current_file.status)
        if name == "FILE LIST":
            s.file_list = [ln.strip() for ln in body]
            return list(s.file_list)
        if name == "IMAGE LIST":
            s.image_list = [ln.strip() for ln in body]
            return list(s.image_list)
        if name == "GPI LIST":
            return self._apply_gpi(body)
        if name == "FRAME BUFFER":
            return self._apply_frame_buffer(body)
        if name == "CAMERACONTROL":
            kv = _kv_dict(body)
            for k, v in kv.items():
                if k.lower() == "camera id":
                    s.camera_id = C.parse_int(v)
            return kv
        if name == "MESSAGE":
            kv = _kv_dict(body, keep_bare=True)
            s.messages.append(kv)
            del s.messages[:-50]
            return kv
        if name in ("END PRELUDE", "ACK", "NAK"):
            return None
        # "clients should ignore blocks they do not recognize"; kept raw.
        s.other[header] = list(body)
        return list(body)

    def _apply_gpi(self, body: List[str]) -> Optional[GpiList]:
        """GPI LIST: ``ID:`` and ``Index:`` lines, then one event per line.
        Every GPI LIST the document shows is the whole list, so a block
        replaces the events of its GPI (unverified on hardware)."""
        gid = _gpi_id(["GPI LIST:"] + body)
        if gid is None:
            return None
        gl = GpiList(gid)
        for ln in body:
            k, sep, v = ln.partition(":")
            key = k.strip().lower()
            if sep and key == "id" and C.parse_int(v) is not None:
                continue
            if sep and key == "index" and C.parse_int(v) is not None:
                gl.index = C.parse_int(v)
                continue
            gl.events.append(ln.strip())
        self.state.gpi_lists[gid] = gl
        return gl

    def _apply_frame_buffer(self, body: List[str]) -> Dict[str, object]:
        fb = self.state.frame_buffer
        changes: Dict[str, object] = {}
        for k, v in _kv_pairs(body):
            m = re.match(r"(?i)^(.+?) Frame Buffer (Enable|Index)$", k)
            if k.lower() == "number of frame buffers":
                fb.count = C.parse_int(v)
                changes[k] = fb.count
            elif re.match(r"(?i)^(BG|LY) Frame Buffer Mix$", k):
                changes[k] = fb.mix[k[:2].upper()] = _int_or(v)
            elif re.match(r"(?i)^(BG|LY) Transition Duration$", k):
                changes[k] = fb.duration[k[:2].upper()] = _int_or(v)
            elif re.match(r"(?i)^Frame Buffer (\d+)$", k):
                n = int(k.split()[-1])
                fb.images[n] = v
                changes[k] = v
            elif m:
                name = m.group(1)
                buf = fb.buffers.setdefault(name, FrameBuffer(name))
                if m.group(2).lower() == "enable":
                    buf.enable = C.parse_bool(v)
                    changes[k] = buf.enable
                else:
                    buf.index = C.parse_int(v)
                    changes[k] = buf.index
            else:
                fb.other[k] = v
                changes[k] = v
        return changes


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _kv_pairs(body: List[str]):
    for ln in body:
        k, sep, v = ln.partition(":")
        if sep:
            yield k.strip(), v.strip()


def _kv_dict(body: List[str], keep_bare: bool = False) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for ln in body:
        k, sep, v = ln.partition(":")
        if sep:
            out[k.strip()] = v.strip()
        elif keep_bare and ln.strip():
            out[""] = ln.strip()
    return out


def _merge_kv_text(old: str, body: List[str]) -> str:
    """Merge a partial ``key: value`` push into a block's previous lines."""
    lines = [ln for ln in old.split("\n") if ln.strip()]
    keys = {ln.partition(":")[0].strip(): i for i, ln in enumerate(lines)}
    for ln in body:
        k = ln.partition(":")[0].strip()
        if k in keys:
            lines[keys[k]] = ln
        else:
            keys[k] = len(lines)
            lines.append(ln)
    return "\n".join(lines) + "\n"


def _set_attrs(obj, kv: Dict[str, str], names: Dict[str, str]) -> None:
    for wire, attr in names.items():
        if wire in kv:
            setattr(obj, attr, kv[wire])


def _int_or(v: str):
    n = C.parse_int(v)
    return v if n is None else n


def _gpi_id(block: List[str]) -> Optional[int]:
    for ln in block[1:]:
        k, sep, v = ln.partition(":")
        if sep and k.strip().lower() == "id":
            return C.parse_int(v)
    return None


def _has_key(header: str, key: str):
    return _has_keys(header, [key])


def _has_keys(header: str, keys: List[str]):
    want = {k.strip().lower() for k in keys}
    seen = set()

    def check(h, block):
        if h == header:
            for ln in block[1:]:
                seen.add(ln.partition(":")[0].strip().lower())
        return want <= seen
    return check


def _one_line(text) -> str:
    t = str(text)
    if "\n" in t or "\r" in t:
        raise C.ControlValueError("a name or value can't contain a line break")
    return t
