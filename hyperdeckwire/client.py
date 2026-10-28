# SPDX-License-Identifier: MIT
"""hyperdeckwire.client — HyperDeck Ethernet Protocol client.

Text-based, line-oriented protocol over TCP 9993. Documented in
``HyperDeckEthernetProtocol.pdf`` from Blackmagic (Dec 2024).

Response codes:
  100-199 : failure (raised as :class:`HyperdeckError`)
  200     : simple ack
  201-299 : success with parameters (multi-line, blank-line terminated)
  500-599 : asynchronous notification (skipped by default during a
            blocking ``request``; arrives interleaved with normal traffic)

Public surface::

    from hyperdeckwire import Hyperdeck

    with Hyperdeck('192.0.2.11') as hd:
        info = hd.device_info()
        for clip in hd.disk_list():
            print(clip.clip_id, clip.name, clip.duration)
        hd.stop()
        hd.clips_clear()
        hd.clips_add('my-clip.mp4')
        hd.play(loop=True, single_clip=True)
        state = hd.transport_info()

Why a single client file (no per-feature split): the protocol vocabulary
is small (~30 commands), the wire format is uniform (request line in,
response block out), and there's no switcher-style state event stream
to fan out. Splitting would add ceremony without payoff.
If the vocabulary doubles, revisit.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)


DEFAULT_PORT = 9993
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 5.0
# Notifications kept for read_notifications(); past this many unread, the
# oldest go.
NOTIFICATION_BACKLOG = 1000

# HH:MM:SS:FF, or HH:MM:SS;FF (drop frame): hours under 24, minutes and
# seconds under 60.
_TIMECODE = re.compile(r'([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9][:;][0-9]{2}')


class HyperdeckError(Exception):
    """The HyperDeck rejected a command (response code 100-199).

    ``code`` and ``text`` are the raw protocol code + text so callers
    can distinguish "remote control disabled" (111) from "clip not
    found" (112) etc. — see the PDF for the full list.
    """

    def __init__(self, code: int, text: str):
        self.code = code
        self.text = text
        super().__init__(f'[{code}] {text}')


@dataclass(frozen=True)
class Response:
    """One response block parsed off the wire."""

    code: int
    text: str
    lines: List[str]

    def is_ok(self) -> bool:
        return 200 <= self.code < 300

    def is_error(self) -> bool:
        return 100 <= self.code < 200

    def is_async(self) -> bool:
        return 500 <= self.code < 600

    def params(self) -> Dict[str, str]:
        """Parse the response body as ``key: value`` pairs.

        Multi-line responses (codes 201-299, 500-599) carry an indented
        block of ``key: value`` lines before the blank terminator. This
        flattens them to a plain dict.
        """
        out: Dict[str, str] = {}
        for line in self.lines:
            stripped = line.strip()
            if not stripped or ':' not in stripped:
                continue
            key, _, val = stripped.partition(':')
            out[key.strip()] = val.strip()
        return out


@dataclass(frozen=True)
class Clip:
    """A clip on the timeline or on disk.

    Fields vary by source: ``disk_list`` populates ``file_format`` and
    ``video_format``; ``clips_get`` (default v1) populates ``start``.
    The intersection is ``clip_id``, ``name``, ``duration`` — those
    three are always set.
    """

    clip_id: int
    name: str
    duration: str                      # timecode HH:MM:SS:FF
    start: Optional[str] = None        # clips_get v1 timeline start
    file_format: Optional[str] = None  # disk_list, e.g. "H.264"
    video_format: Optional[str] = None # disk_list, e.g. "1080p25"


# ---------------------------------------------------------------------------
# Hyperdeck client
# ---------------------------------------------------------------------------

class Hyperdeck:
    """Client for the HyperDeck Ethernet Protocol over TCP 9993.

    Connection lifecycle is explicit: :meth:`connect` opens the socket
    and drains the greeting; :meth:`close` shuts down. Use as a context
    manager to get both for free::

        with Hyperdeck(ip) as hd:
            ...

    Read-only commands (``device_info``, ``disk_list``, ``clips_get``,
    ``transport_info``, ``configuration``, ``slot_info``) work without
    setup. Write commands (``stop``, ``play``, ``record``,
    ``clips_clear``, ``clips_add``, ``goto_clip``) need the unit's
    "remote control" flag enabled. On HD-class units that defaults to
    True; query with :meth:`remote_info` if unsure.

    ``socket_factory`` is a hook for tests: pass a callable that
    returns an object with ``sendall``, ``recv``, ``settimeout``,
    ``close``, ``shutdown``. Default is :func:`socket.create_connection`.
    """

    def __init__(self,
                 host: str,
                 port: int = DEFAULT_PORT,
                 *,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 read_timeout: float = DEFAULT_READ_TIMEOUT,
                 socket_factory: Optional[Callable] = None):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._socket_factory = socket_factory or socket.create_connection
        self._sock = None
        self._buf = b''
        self._greeting: Optional[Response] = None
        self._notifications: Deque[Response] = deque(maxlen=NOTIFICATION_BACKLOG)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> 'Hyperdeck':
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def connect(self) -> None:
        """Open the TCP connection and drain the greeting (500 connection info).

        A deck that refuses the connection answers ``120 connection
        failed`` and closes the socket; that raises :class:`HyperdeckError`,
        as does any other 1xx greeting. A greeting that is neither 1xx nor
        500 raises ``OSError``. Either way the socket is closed.
        """
        # connect() over an already-connected
        # instance must not overwrite _sock and leak the old socket.
        if self._sock is not None:
            self.close()
        self._sock = self._socket_factory((self.host, self.port),
                                          timeout=self.connect_timeout)
        self._sock.settimeout(self.read_timeout)
        self._buf = b''
        self._greeting = None
        self._notifications.clear()
        # The unit sends a 500 connection info: block immediately on
        # accept. It IS a 5xx code, but it's a one-time synchronous
        # greeting, not the kind of asynchronous notification we
        # normally want to skip during a request — read it with
        # skip_async=False so it doesn't get drained into the void.
        try:
            greeting = self._read_response(skip_async=False)
            # A refused connection used to "connect", with an empty model
            # and protocol version, and fail on the first call with a
            # ConnectionError that said nothing about why (bmdwire#21).
            if greeting.is_error():
                raise HyperdeckError(greeting.code, greeting.text)
            if greeting.code != 500:
                raise OSError(f'unexpected greeting: {greeting!r}')
            self._greeting = greeting
        except Exception:
            # A failed greeting read must not leak
            # the just-opened socket until GC — close + clear before
            # re-raising so the instance is cleanly reconnectable.
            self.close()
            raise

    def close(self) -> None:
        """Send ``quit`` (best-effort) and close the socket."""
        if self._sock is None:
            return
        try:
            self._send_line('quit')
        except OSError:
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    # ------------------------------------------------------------------
    # Greeting accessors
    # ------------------------------------------------------------------

    @property
    def model(self) -> str:
        """Model name from the connection greeting (e.g. 'HyperDeck Studio HD Mini')."""
        return (self._greeting.params().get('model', '')
                if self._greeting else '')

    @property
    def protocol_version(self) -> str:
        """Protocol version string from the connection greeting (e.g. '1.13')."""
        return (self._greeting.params().get('protocol version', '')
                if self._greeting else '')

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    def _send_line(self, line: str) -> None:
        # The deck ends a command at the line break, so a value carrying
        # one (a clip name, a record prefix) was sent as a second command:
        # 'clips add: name: a.mov\nclips clear' cleared the timeline. No
        # value in the protocol can hold one (bmdwire#20).
        if '\n' in line or '\r' in line:
            raise ValueError(f'line break in a HyperDeck command: {line!r}')
        if self._sock is None:
            raise OSError('not connected')
        self._sock.sendall((line + '\n').encode('utf-8'))

    def _recv_line(self, deadline: float) -> str:
        """Read one CRLF-terminated line. Strips trailing \\r\\n."""
        while b'\n' not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('no line within deadline')
            self._sock.settimeout(min(remaining, 0.5))
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError('peer closed connection')
            self._buf += chunk
        line, _, rest = self._buf.partition(b'\n')
        self._buf = rest
        return line.rstrip(b'\r').decode('utf-8', errors='replace')

    def _read_response(self, timeout: Optional[float] = None,
                       skip_async: bool = True) -> Response:
        """Read one full response block off the wire.

        Multi-line detection: a response is multi-line iff the headline
        ends with a colon. Continuation lines are then read until a
        blank-line terminator.

        Asynchronous 5xx messages can arrive interleaved. By default
        they're kept for :meth:`read_notifications` and skipped here, so a
        caller waiting on the actual response doesn't see them. Set
        ``skip_async=False`` to return them through.
        """
        timeout = timeout if timeout is not None else self.read_timeout
        deadline = time.monotonic() + timeout
        while True:
            head = self._recv_line(deadline)
            if not head:
                continue
            code, _, text = head.partition(' ')
            try:
                code_int = int(code)
            except ValueError:
                raise OSError(f'malformed response head: {head!r}')

            lines: List[str] = []
            if text.endswith(':'):
                text = text[:-1]
                while True:
                    cont = self._recv_line(deadline)
                    if cont == '':
                        break
                    lines.append(cont)

            resp = Response(code=code_int, text=text, lines=lines)
            if resp.is_async() and skip_async:
                logger.debug('hyperdeck async: %s', resp)
                self._notifications.append(resp)
                continue
            return resp

    def request(self, command: str,
                timeout: Optional[float] = None) -> Response:
        """Send a command, read one response, return it.

        Does NOT raise on protocol-level error codes (1xx). Use
        :meth:`_check_ok` (or just inspect the response) for that.

        The protocol has no request ids: the answer is the next
        non-asynchronous response. So:

        - Responses that arrived before the command is sent are not its
          answer. They are read and dropped first (logged at warning;
          asynchronous ones at debug). A stray answer that arrives after
          the command is sent can't be told from the real one.
        - A timeout closes the connection before ``TimeoutError`` is
          raised, since the late answer would otherwise be taken as the
          next command's and every answer after it would be one behind.
          Later calls raise ``OSError('not connected')`` until
          :meth:`connect`.
        - A command with a line break in it raises ``ValueError`` before
          anything is sent.
        """
        if self._sock is not None:
            self._drop_unrequested()
        self._send_line(command)
        try:
            return self._read_response(timeout=timeout)
        except TimeoutError:
            # The answer is still owed, and nothing could tell it from the
            # next command's (bmdwire#18).
            self.close()
            raise

    def _drop_unrequested(self) -> None:
        """Read and drop every complete response that has already arrived:
        those in ``_buf`` and whatever the socket has ready now, read
        without blocking. A partial response at the end of ``_buf`` stays.
        Before this, a response nobody asked for (a timed-out command's
        late answer, a deck answering twice) was returned to the next
        request, and every answer after it was one behind (bmdwire#19).
        """
        self._sock.settimeout(0)
        try:
            while True:
                try:
                    chunk = self._sock.recv(4096)
                except (BlockingIOError, InterruptedError, socket.timeout):
                    break
                if not chunk:
                    raise ConnectionError('peer closed connection')
                self._buf += chunk
        finally:
            self._sock.settimeout(self.read_timeout)
        while b'\n' in self._buf:
            kept = self._buf
            try:
                # A zero timeout reads only what _buf already holds.
                resp = self._read_response(timeout=0, skip_async=False)
            except TimeoutError:
                self._buf = kept
                break
            if resp.is_async():
                logger.debug('hyperdeck async: %s', resp)
                self._notifications.append(resp)
            else:
                logger.warning('hyperdeck: dropping a response no command '
                               'was waiting for: %s', resp)

    def _check_ok(self, command: str,
                  timeout: Optional[float] = None) -> Response:
        """Like :meth:`request` but raises :class:`HyperdeckError` on 1xx."""
        resp = self.request(command, timeout=timeout)
        if resp.is_error():
            raise HyperdeckError(resp.code, resp.text)
        return resp

    # ------------------------------------------------------------------
    # Info commands (read-only)
    # ------------------------------------------------------------------

    def device_info(self) -> Dict[str, str]:
        """Return ``model``, ``protocol version``, ``unique id``, etc."""
        return self._check_ok('device info').params()

    def remote_info(self) -> Dict[str, str]:
        """Return remote-control state (``enabled``, ``override``)."""
        return self._check_ok('remote').params()

    def slot_info(self, slot_id: Optional[int] = None) -> Dict[str, str]:
        """Return info for the active slot (or a specific slot id)."""
        cmd = 'slot info' if slot_id is None else f'slot info: slot id: {slot_id}'
        return self._check_ok(cmd).params()

    def transport_info(self) -> Dict[str, str]:
        """Return current transport state (status, speed, clip id, timecode, loop, …)."""
        return self._check_ok('transport info').params()

    def configuration(self) -> Dict[str, str]:
        """Return the unit's current configuration (file format, audio, timecode, …)."""
        return self._check_ok('configuration').params()

    def set_configuration(self, **params) -> None:
        """Update one or more configuration parameters in a single command.

        Pass snake_case keyword args; they're translated to the protocol's
        ``{param}: {value}`` form (underscores -> spaces) and stacked into
        a single ``configuration:`` line so the deck applies them as a
        batch. Booleans render as ``true`` / ``false`` strings.

        Examples::

            hd.set_configuration(file_format='H.264High')
            hd.set_configuration(file_format='QuickTimeProResHQ',
                                 default_standard='2160p25')
            hd.set_configuration(record_cache=True)

        Common parameter names (see ``HyperDeckEthernetProtocol.pdf`` for
        the full enum on each):

        ===================== =========================================
        snake_case kwarg      Protocol field
        ===================== =========================================
        ``file_format``       H.264High, QuickTimeProResHQ, DNxHR_HQX, …
        ``default_standard``  1080p25, 2160p50, 720p5994, …
        ``video_input``       SDI, 4xSDI, HDMI, component, composite
        ``audio_input``       embedded, XLR, RCA
        ``audio_codec``       PCM, AAC
        ``record_prefix``     str (UTF-8)
        ``record_cache``      bool
        ``append_timestamp``  bool
        ===================== =========================================

        A no-args call is a no-op; the protocol would reject an empty
        ``configuration:`` anyway.

        Note: BMD's docs say changing ``file_format`` *may* respond with
        ``213 deck rebooting`` (a 2xx success code) instead of ``200 ok``
        and close the connection. Both are treated as success here. In
        testing, a Studio HD Mini did not reboot on the format changes
        exercised, so no auto-reconnect logic is wired up; catch
        ``OSError`` on the next call if you trip the rare case.
        """
        if not params:
            return
        self._check_ok('configuration: ' + _stack(params))

    def notify(self) -> Dict[str, str]:
        """Return which notifications are on for this connection:
        ``transport``, ``slot``, ``remote``, ``configuration`` and the rest,
        each ``true`` or ``false`` (``209 notify``)."""
        return self._check_ok('notify').params()

    def set_notify(self, **kinds) -> None:
        """Turn notifications on or off for this connection, in one
        ``notify:`` line, as :meth:`set_configuration` stacks its settings::

            hd.set_notify(transport=True, slot=True)
            hd.set_notify(display_timecode=False)

        Notifications are per connection, and hyperdeckwire turns none on
        by itself. Once one is on, the deck sends that block (``508
        transport info``, ``502 slot info``, ``510 remote info``, ``511
        configuration``) as it changes, between the answers to commands;
        :meth:`read_notifications` returns them. ``display_timecode``
        comes every frame. A no-args call is a no-op.
        """
        if not kinds:
            return
        self._check_ok('notify: ' + _stack(kinds))

    def read_notifications(self, timeout: float = 0.0) -> List[Response]:
        """Return the notifications that have arrived, oldest first, and
        forget them.

        They are the asynchronous 5xx blocks :meth:`set_notify` turned on,
        taken whenever they arrive: before or among the answers to
        commands, or here. With ``timeout``, wait up to that many seconds
        for one when none has arrived; without it, take only what is
        there. A response that is not a notification is no command's
        answer, and is dropped as :meth:`request` drops one. Waiting out
        ``timeout`` raises nothing and leaves the connection as it was.
        Past ``NOTIFICATION_BACKLOG`` unread, the oldest go; a
        :meth:`connect` starts afresh.
        """
        if self._sock is None:
            raise OSError('not connected')
        deadline = time.monotonic() + timeout
        self._drop_unrequested()
        while not self._notifications:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sock.settimeout(min(remaining, 0.5))
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            finally:
                self._sock.settimeout(self.read_timeout)
            if not chunk:
                raise ConnectionError('peer closed connection')
            self._buf += chunk
            self._drop_unrequested()
        out = list(self._notifications)
        self._notifications.clear()
        return out

    def disk_list(self, slot_id: Optional[int] = None) -> List[Clip]:
        """List clips on the active disk (or specified slot)."""
        cmd = 'disk list' if slot_id is None else f'disk list: slot id: {slot_id}'
        resp = self._check_ok(cmd)
        return _parse_clip_lines(resp.lines, _parse_disk_line)

    def clips_count(self) -> int:
        """Return the number of clips on the current timeline."""
        resp = self._check_ok('clips count')
        return int(resp.params().get('clip count', '0'))

    def clips_get(self) -> List[Clip]:
        """Return all clips currently on the timeline (version 1 format).

        An empty timeline is ``[]``: a deck answers ``clips get`` on one
        with ``107 timeline empty`` (a Studio HD Mini on software 9.0.2,
        2026-10-27), not an empty ``205 clips info``. Any other refusal
        raises :class:`HyperdeckError`, as before.
        """
        resp = self.request('clips get')
        if resp.code == 107:
            return []
        if resp.is_error():
            raise HyperdeckError(resp.code, resp.text)
        return _parse_clip_lines(resp.lines, _parse_clips_v1_line)

    # ------------------------------------------------------------------
    # Action commands (write)
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Stop playback or recording."""
        self._check_ok('stop')

    def play(self, *,
             loop: Optional[bool] = None,
             single_clip: Optional[bool] = None,
             speed: Optional[int] = None,
             clip_id: Optional[int] = None) -> None:
        """Start playback. All parameters optional; defaults to a plain ``play``.

        ``speed`` is a percentage between -5000 and 5000 (100 = normal).
        ``clip_id`` starts playback at the given clip on the timeline.
        ``loop`` and ``single_clip`` go as given, ``true`` or ``false``,
        and left out (``None``) the line says nothing of them. Before
        1.8.0 a false one was left out too, so ``play(loop=False)``
        couldn't turn a loop off.
        """
        parts = []
        if clip_id is not None:
            parts.append(f'clip id: {clip_id}')
        if loop is not None:
            parts.append(f"loop: {'true' if loop else 'false'}")
        if single_clip is not None:
            parts.append(f"single clip: {'true' if single_clip else 'false'}")
        if speed is not None:
            parts.append(f'speed: {int(speed)}')
        cmd = 'play' if not parts else 'play: ' + ' '.join(parts)
        self._check_ok(cmd)

    def pause(self) -> None:
        """Pause playback, holding the current frame.

        The HyperDeck protocol has no literal ``pause`` verb — pausing is
        ``play`` at zero speed, which freezes on the current frame (vs ``stop``
        which ends playback). ``play()`` resumes at normal speed.
        """
        self._check_ok('play: speed: 0')

    def clips_clear(self) -> None:
        """Empty the current timeline (does not delete files on disk).

        The deck answers before the timeline is empty: a Studio HD Mini on
        9.0.2 (2026-10-27) still listed the old clips to a ``clips get``
        0.01 s after the ``200 ok``, and answered ``107 timeline empty`` at
        0.16 s. Read the timeline again a moment later, not at once.
        """
        self._check_ok('clips clear')

    def clips_add(self, name: str,
                  *, before_clip_id: Optional[int] = None) -> None:
        """Append a clip to the timeline (or insert before ``before_clip_id``).

        ``name`` may include subfolders (``folder/clip.mp4``). Names with
        spaces are passed through verbatim — the protocol parses the
        parameter value as everything after ``name:`` until end-of-line.
        """
        if before_clip_id is not None:
            cmd = f'clips add: clip id: {before_clip_id} name: {name}'
        else:
            cmd = f'clips add: name: {name}'
        self._check_ok(cmd)

    def clips_remove(self, clip_id: int) -> None:
        """Remove a clip from the timeline by id (invalidates ids after it)."""
        self._check_ok(f'clips remove: clip id: {clip_id}')

    def goto_clip(self, clip_id: int) -> None:
        """Seek the transport to the start of ``clip_id`` (does not play)."""
        self._check_ok(f'goto: clip id: {clip_id}')

    def goto_clip_by(self, offset: int) -> None:
        """Seek ``offset`` clips along the timeline, back when negative:
        ``goto_clip_by(1)`` is the next clip's start, ``goto_clip_by(-1)``
        the previous one's (``goto: clip id: +1``). Past either end the
        deck refuses it with ``109 out of range``, not holding at the end
        (a Studio HD Mini, software 9.0.2). An offset of 0 raises
        ``ValueError``.
        """
        offset = int(offset)
        if offset == 0:
            raise ValueError('goto_clip_by: an offset of 0 clips moves nowhere')
        self._check_ok(f'goto: clip id: {offset:+d}')

    def goto_timecode(self, timecode: str) -> None:
        """Seek to a timecode on the timeline, ``HH:MM:SS:FF`` (``;FF``
        for drop frame), without playing. Anything else raises
        ``ValueError`` before it is sent.
        """
        if not isinstance(timecode, str) or not _TIMECODE.fullmatch(timecode):
            raise ValueError(f'goto_timecode: {timecode!r} is not a timecode, HH:MM:SS:FF')
        self._check_ok(f'goto: timecode: {timecode}')

    def record(self, name: Optional[str] = None) -> None:
        """Start recording on the active slot, into a clip named ``name``
        (``record: name: …``), or, with none (or ``''``), one the deck
        names from its record prefix. ``stop()`` ends it.

        With no video input a Studio HD Mini (software 9.0.2) refuses it
        with ``110 no input`` (raised) and goes to preview all the same;
        ``stop()`` answers and leaves it there, and a ``goto_clip`` brings
        it out.
        """
        self._check_ok(f'record: name: {name}' if name else 'record')

    def remote_enable(self, enable: bool = True) -> None:
        """Enable or disable remote control."""
        flag = 'true' if enable else 'false'
        self._check_ok(f'remote: enable: {flag}')

    def slot_select(self, slot_id: int) -> None:
        """Switch the active slot."""
        self._check_ok(f'slot select: slot id: {slot_id}')

    def ping(self) -> None:
        """Check the unit is responding. Raises on error / disconnect."""
        self._check_ok('ping')


def _stack(params) -> str:
    """snake_case keyword arguments as one line's ``{name}: {value}``
    pairs: underscores become spaces, booleans ``true`` / ``false``."""
    parts = []
    for key, value in params.items():
        proto_key = key.replace('_', ' ')
        if isinstance(value, bool):
            proto_value = 'true' if value else 'false'
        else:
            proto_value = str(value)
        parts.append(f'{proto_key}: {proto_value}')
    return ' '.join(parts)


# ---------------------------------------------------------------------------
# Line parsers
# ---------------------------------------------------------------------------

def _looks_like_clip_line(line: str) -> bool:
    """True if the line begins with ``<int>:`` (a clip row) rather than
    a header like ``slot id: 1`` or ``clip count: 5``."""
    head, _, _ = line.strip().partition(':')
    return head.isdigit()


def _parse_clip_lines(lines, parser) -> List[Clip]:
    """Apply ``parser`` to every clip-shaped line, skipping (and logging)
    any single row the parser rejects.

    One malformed row from the deck must not blow away the whole list;
    callers need whatever clips parsed cleanly, not an exception. Header/non-clip lines are filtered first as before."""
    clips: List[Clip] = []
    for line in lines:
        if not _looks_like_clip_line(line):
            continue
        try:
            clips.append(parser(line))
        except (ValueError, IndexError) as e:
            logger.warning('hyperdeck: skipping unparseable clip line %r: %s',
                           line, e)
    return clips


def _parse_disk_line(line: str) -> Clip:
    """Parse a ``disk list`` row (version 1).

    Format: ``{id}: {name} {file format} {video format} {duration}``

    Filenames may contain spaces (verified against real hardware:
    "Intro Loop animation.mp4"). We pull the three trailing tokens
    (duration, video_format, file_format) and treat the rest as name.
    """
    head, _, rest = line.strip().partition(':')
    clip_id = int(head.strip())
    tokens = rest.strip().split(' ')
    if len(tokens) < 4:
        raise ValueError(f'malformed disk list line: {line!r}')
    duration = tokens[-1]
    video_format = tokens[-2]
    file_format = tokens[-3]
    name = ' '.join(tokens[:-3])
    return Clip(clip_id=clip_id, name=name, duration=duration,
                file_format=file_format, video_format=video_format)


def _parse_clips_v1_line(line: str) -> Clip:
    """Parse a ``clips get`` row in version 1 format.

    Format: ``{id}: {name} {startT} {duration}``

    Same filename-with-spaces caveat as ``_parse_disk_line``; tokens
    are parsed from the right.
    """
    head, _, rest = line.strip().partition(':')
    clip_id = int(head.strip())
    tokens = rest.strip().split(' ')
    if len(tokens) < 3:
        raise ValueError(f'malformed clips line: {line!r}')
    duration = tokens[-1]
    start = tokens[-2]
    name = ' '.join(tokens[:-2])
    return Clip(clip_id=clip_id, name=name, duration=duration, start=start)
