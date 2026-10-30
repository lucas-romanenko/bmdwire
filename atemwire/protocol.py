# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
import logging
import random
import struct
import time

from atemwire._transfer import TransferTask, TransferQueueFlushed
from atemwire.transport import UdpProtocol, Packet, ConnectionReady, Wakeup
from atemwire.messages import (
    ClearStillCommand,
    KEY_FORMAT_BY_PRETTY,
    LockCommand,
    PartialLockCommand,
    PRETTY_BY_CODE,
    RECV_BY_CODE,
    TimeRequestCommand,
    TransferAckCommand,
    TransferDataCommand,
    TransferDownloadRequestCommand,
    TransferFileDataCommand,
    TransferUploadRequestCommand,
)
from atemwire.messages.audio_legacy import (
    LEGACY_AUDIO_DIAGNOSTIC_KEYS,
    audio_mixer_type,
    legacy_audio_diagnostic,
)
from atemwire.messages.system_info import topology_diagnostic
from atemwire.imaging import rle_decode


def _is_tcp_transport(transport) -> bool:
    """Identify the OpenSwitcher TCP-proxy transport without import-time
    coupling to the lazy-loaded class.  Two AtemProtocol code paths
    behave differently on TCP: the InCm handler raises 'connected'
    here rather than waiting for the transport's ConnectionReady, and
    file-transfer uploads go straight to ``transport.upload(task)``."""
    return type(transport).__name__ == 'TcpProtocol'

# A lock request (LOCK / PLCK) with no LKOB back after this long is sent
# again on the next trigger. Measured: a granted request answers in ~1 ms;
# a queued one (PLCK, and a full LOCK too: ATEM Software Control's, in a
# capture of 2026-10-15) the moment the holder releases.
LOCK_REREQUEST_SECONDS = 1.0

# Another client's turn. A request of another client's that waited for
# the store is granted the moment we release it, and the switcher grants a
# request of ours sent soon after too (two sessions asking within ~150 ms
# can both get LKOB; MEDIA_LOCK.md). Our transfer then aborts theirs with
# FTDE 6, and ATEM Software Control, which tries each still once when it
# connects and again only when the media pool changes, is left without
# its thumbnails (a capture, 2026-10-15: 20 stills, 20 FTDE 6, while a
# client of this package downloaded back to back; 1.5.0 sent the release
# and the next request in one packet). A request handed the store at our
# release isn't announced at all (no LKST either way), and a PLCK of ours
# is granted beside another client's LOCK even half a second into its
# turn, its transfer aborted (a second capture, the same day). So after a
# per-frame release with more work queued for the store, the next request
# waits for the store to be announced free (LKST), then LOCK_COURTESY more
# (a frame, for anyone else to take it); a store another client is seen
# holding (its LKST) is not asked for until announced free. No
# announcement means someone was handed the store: we wait, as long as
# anyone is seen at work (another session's FTDC, any LKST), and ask only
# after LOCK_QUIET with no sign of anyone. bmdwire 1.4.0 waited the same
# way for the release's echo, by accident: its long waits in a restore
# were WebATEM refreshing each new still's thumbnail.
LOCK_COURTESY = 0.06
LOCK_QUIET = 10.0

# The turn after our own release goes to whoever was waiting for it. They
# ask LOCK_COURTESY after the release is announced; so did we, and two
# requests that close together are both granted, or one is aborted
# (FTDE 6 to a download; to an upload, a refusal of every chunk of its
# budget). Measured on a Constellation HD, 2026-10-30: an uploader and a
# downloader in one process asked 0.3 ms apart after each upload's release
# (bmdwire#63). So after a release of ours, our next request waits
# LOCK_HANDOVER more: time for a waiter's request, its grant, and the LKST
# that announces it (the next frame tick) to reach us, after which the
# store is another client's and we ask once they let go. Only while
# another client has been seen in the last LOCK_QUIET (its LKST, its FTDC,
# an FTDE 6): alone, nothing is waiting, and a restore of 20 stills would
# pay it 20 times for nobody.
LOCK_HANDOVER = 0.1

# Our release's own LKST comes at the switcher's next frame tick, about
# 40 ms later. A release announced much later than this is another
# client's: the store was handed to a request waiting at the switcher, with
# nothing announced, and that client has now let go. We are its waiter
# then, not the releaser, and ask LOCK_COURTESY after it like any waiter.
LOCK_ECHO_WINDOW = 0.25


def _first_transfer_id():
    """A session's first transfer id, at random in 1..0xFFFF.

    The switcher broadcasts FTDC to every session with the owner's transfer
    id, and a session matches it against its own. Every session used to
    start at 43, so two sessions of this package (in one process or not)
    used the same ids: one took the other's FTDC for its own, finished a
    transfer the switcher was still sending, and stopped acknowledging it.
    The switcher, its transfer engine held by a transfer nobody drained,
    refused every later request with FTDE 1 (try again), from both
    sessions, for good (bmdwire#64)."""
    return random.randrange(1, 0x10000)


# A state dump that ends (InCm) without a video mode has lost its first
# packet for good: the transport takes the first reliable packet that
# arrives as the session's baseline, so one lost before it is never resent
# (bmdwire#13). The protocol re-handshakes for a fresh dump at once instead
# of leaving wait_ready to time out (15 s through ATEMConnection) or the
# session to run without one, at most this many times in a row, so a
# switcher whose VidM never parses (SURFACE.md §5.4 item 3) costs a few
# handshakes, not a loop.
INCOMPLETE_DUMP_RETRIES = 3

# Fields every switcher sends early in its dump; the ones missing at InCm
# are named in the log.
_CORE_DUMP_FIELDS = ('video-mode', 'topology', 'product-name')


class IncompleteStateDump(ConnectionError):
    """The state dump ended without a video mode. Raised from
    ``save_field_data`` and handled by ``loop()`` like transport
    corruption: goodbye, re-handshake, fresh dump."""


class AtemProtocol:
    STRUCT_FIELD = struct.Struct('!H2x 4s')

    # Wire codes whose pretty names live on the Recv classes themselves
    # (via the ``PRETTY`` class attribute) are routed through
    # ``PRETTY_BY_CODE`` from ``atemwire.messages``. This table only
    # carries wire codes that the ATEM emits but atemwire doesn't parse —
    # the bytes are stored under the pretty name as raw payload so
    # downstream code can still find them in ``mixerstate``.
    _UNMAPPED_PRETTY = {
        'ATMP': 'talkback-mixer-properties',
        'CCst': 'camera-control-settings',
        'DCPV': 'displayclock-properties',
        'DSTV': 'displayclock-set-time',
        'FMPP': 'fairlight-properties',
        'MMOP': 'mix-minus-output-properties',
        'MPAS': 'mediaplayer-audio-source',
        'MPCS': 'mediaplayer-clip-source',
        'MPSp': 'mediaplayer-space',
        'MvVM': 'multiview-video-mode-capability',
        'Powr': 'power-status',
        'RCPS': 'mediaplayer-clip-status',
        'RXCC': 'hyperdeck-clip-count',
        'RXCP': 'hyperdeck-status',
        'RXSS': 'hyperdeck-storage',
        'SAth': 'streaming-authentication',
        'SRST': 'streaming-time',
        'StMv': 'multiviewer-safe-area-type',
        'TMIP': 'talkback-mixer-input-properties',
        'TcLk': 'timecode-lock',
        'VuMo': 'multiviewer-vu-opacity',
        '_DVE': 'dve-capabilities',
        '_FAC': 'fairlight-audio-config',
        '_MAC': 'macro-config',
        '_MvC': 'multiviewer-config',
        '_SSC': 'supersource-config',
        '_TlC': 'tally-config',
    }

    # Merged dispatch table — class-attribute PRETTY values for mapped
    # codes, falling back to the small _UNMAPPED_PRETTY dict above for
    # codes with no Recv class. Built once at class-definition time.
    FIELDNAME_PRETTY = dict(PRETTY_BY_CODE, **_UNMAPPED_PRETTY)

    # Pretty names whose KEY_FORMAT lives on the Recv class come from
    # ``KEY_FORMAT_BY_PRETTY``. Pretty names listed here have no Recv
    # class (the ATEM emits the packet but we don't parse it) yet still
    # need an index-extraction format so the raw bytes can be stored
    # in ``mixerstate`` under the right per-index key.
    # key-properties-fly-keyframe (KKFP) now keys per (me, keyer, keyframe)
    # via KeyPropertiesFlyKeyframeField.KEY_FORMAT ('>BBB'), so it arrives
    # through KEY_FORMAT_BY_PRETTY like every other indexed field — no
    # manual entry needed.
    FIELDNAME_UNIQUE = dict(KEY_FORMAT_BY_PRETTY)

    def __init__(self, ip=None, port=9910, usb=None, *, aggressive_drain=False):
        """
        :param aggressive_drain: if True and the transport is UDP, enable
            bulk-upload throughput mode on the transport (drains the whole
            send queue per queue_trigger instead of one ACK-paced batch).
            Only meaningful for UDP; TCP and USB ignore the flag. Uploads,
            the only packets queued, now send each budget whole whatever
            the flag (_queue_chunks), so it changes nothing.
        """
        if ip is None and usb is None:
            raise ValueError("Need either an ip or usb port")
        if ip is not None:
            if ip.startswith('tcp://'):
                # The OpenSwitcher TCP-proxy transport is not shipped in
                # this fork; only the native UDP transport is supported.
                raise NotImplementedError(
                    'tcp:// proxy transport is not included in this fork')
            self.transport = UdpProtocol(ip, port, aggressive_drain=aggressive_drain)
        else:
            # The USB transport (ATEM Mini over libusb) is not shipped in
            # this fork; only the native UDP transport is supported.
            raise NotImplementedError(
                'USB transport is not included in this fork')

        self.log = logging.getLogger('AtemProtocol')
        self.transport.queue_callback = self.queue_callback
        self.mixerstate = {}
        self.callbacks = {}
        self.callback_idx = 1
        # ``connected``: any data packet has arrived on the session (flips
        # on the FIRST packet of the state dump). ``initialized``: the dump
        # is complete — InCm seen and the ConnectionReady sentinel
        # delivered; this is what "ready" means (see atemwire.ready).
        self.connected = False
        self.initialized = False
        # Consecutive dumps that ended without a video mode; see
        # INCOMPLETE_DUMP_RETRIES.
        self._incomplete_dumps = 0
        # (key, AMIP source) of the legacy-audio packets already logged
        # (messages/audio_legacy.py, "Diagnostics").
        self._legacy_audio_logged = set()

        # store -> True while we hold its lock: from the LKOB until we send
        # the release, whatever LKST says meanwhile. The switcher announces
        # lock changes once a frame, so the release broadcast of the frame
        # we just gave back can arrive AFTER the LKOB for the next request
        # sent with that release (ATEM Software Control's restore capture,
        # 2026-10-15). Reading that late broadcast as losing the new grant
        # is what once looked like the switcher eating a request sent
        # during a release: we skipped the next release, and our next
        # request came from the holder, which the switcher ignores. See
        # docs/MEDIA_LOCK.md.
        self.locks = {}
        # store -> monotonic time of the last lock request (LOCK / PLCK)
        # sent with no LKOB back yet. ``download()`` calls _transfer_trigger
        # once per queued task, and before this existed every call re-sent
        # the request: three downloads queued together put three PLCKs on
        # the wire for one grant (measured 2026-09-16). A request younger
        # than LOCK_REREQUEST_SECONDS is not repeated; an older one is,
        # because the switcher does not queue every kind of request (a
        # full LOCK sent while held is ignored) and a client must re-ask.
        # Cleared by the LKOB, by the store's LKST release broadcast (the
        # pounce re-requests anyway), and by every lane reset.
        self._lock_requested = {}
        # store -> [since, announced]: for a store we released and have more
        # work for (LOCK_COURTESY), when we released it or last saw anyone
        # at work, and when it was announced free (None until it is).
        self._yielding = {}
        # Stores another client holds, from its LKST until its release's:
        # we don't ask for them meanwhile (LOCK_COURTESY).
        self._foreign_held = set()
        # store -> monotonic time of the release of ours that started its
        # yield: the turn after it goes to a waiter first (LOCK_HANDOVER).
        self._own_release = {}
        # monotonic time another client was last seen at the store (its
        # LKST, its FTDC, an FTDE 6 to us); None if never.
        self._others_seen = None
        # store -> monotonic time of our last LKOB for it.
        self._last_grant = {}
        self.mode = None
        self.transfer_queue = {}
        self.transfer_id = _first_transfer_id()
        # Download chunks accumulate as a LIST and join once at FTDC. The
        # historical bytes concat (buffer += chunk) was O(n²) across the
        # ~6000 chunks of a 1080p still — the reason the (since-deleted)
        # rawtransfer bypass existed. List-append makes this path
        # wire-speed (2026-07-02, ASC-style shared-session work).
        self.transfer_buffer = []
        self.transfer_buffer_bytes = 0
        self.transfer = None
        self.transfer_requested = False
        self.transfer_packets = 0
        self.transfer_budget = []

    def connect(self):
        self.log.debug('Starting connection')
        self.transport.connect()

    def loop(self):
        self.log.debug('Waiting for data packet...')
        packet = self.transport.receive_packet()
        if packet is None:
            # Disconnected from hardware
            if self.connected:
                self._raise('disconnected')
                self.mixerstate = {}
                self.initialized = False
                # A transfer in flight when the session died is gone. The
                # transport auto-reconnects (same worker survives), and the
                # NEW session must not inherit ghost lane state — a stale
                # transfer_requested made the first post-reconnect transfer
                # queue behind a corpse for a full caller timeout, and
                # stale locks[] skipped the PLCK the new session needs
                # (2026-07-06).
                self._reset_transfer_lane()
            self.connected = False
            return
        if isinstance(packet, ConnectionReady):
            self.connected = True
            self.initialized = True
            self.send_commands([TimeRequestCommand()])
            self._log_audio_mixer()
            self._log_topology()
            self._raise('connected')
            return
        if isinstance(packet, Wakeup):
            # Caller-side wake-up to drain a pending outbound command — the
            # worker thread (ATEMConnection._run) will pick up the queue
            # right after this returns — or the transport's, for the lock
            # yield (wake_at): a store left free may be asked for now.
            self._service_yields()
            return
        self.connected = True
        if isinstance(packet, TransferQueueFlushed):
            self._queue_flushed()
            return
        try:
            for fieldname, data in self.decode_packet(packet.data):
                self.save_field_data(fieldname, data)
        except ConnectionError as e:
            # The old handler claimed "closing connection" but closed
            # nothing: the session stayed ESTABLISHED with a gutted
            # mixerstate the ATEM never re-dumps. Say goodbye and
            # re-handshake so a fresh full state dump arrives
            # (2026-07-06). decode_packet raises a bare ConnectionError
            # on a zero-length field; save_field_data raises
            # IncompleteStateDump, which says why.
            self.log.error(
                f'{str(e) or "Protocol corruption"} — forcing a clean '
                f'session reconnect')
            self._raise('disconnected')
            self.mixerstate = {}
            self.connected = False
            self.initialized = False
            self._reset_transfer_lane()
            try:
                self.transport.close_session()
            except Exception:
                pass
            try:
                self.transport.connect()
            except Exception as e:
                self.log.error(f'Post-corruption reconnect failed: {e}')

    def on(self, event, callback):
        if event not in self.callbacks:
            self.callbacks[event] = {}
        self.callbacks[event][self.callback_idx] = callback
        self.callback_idx += 1
        return self.callback_idx - 1

    def off(self, event, callback_id):
        if event not in self.callbacks:
            return
        del self.callbacks[event][callback_id]

    def _raise(self, event, *args, **kwargs):
        # Iterate a SNAPSHOT: handlers routinely off() themselves from the
        # caller thread the moment their event fires (download-done wakes
        # the waiter, whose finally immediately unregisters). Iterating the
        # live dict raced that off() into "dictionary changed size during
        # iteration", aborting save_field_data BETWEEN FTDC's queue-pop and
        # _transfer_trigger — a silently stranded media-store lock on the
        # shared session (2026-07-06). A
        # handler exception must not poison the packet loop either.
        handlers = self.callbacks.get(event)
        if not handlers:
            return
        for cb in list(handlers.values()):
            try:
                cb(*args, **kwargs)
            except Exception:
                self.log.exception(f"'{event}' handler raised (ignored)")

    def decode_packet(self, data):
        offset = 0
        while offset < len(data):
            datalen, cmd = self.STRUCT_FIELD.unpack_from(data, offset)

            # A zero length header is not possible, this occurs when the transport layer has corruption, mark the
            # connection closed to restart and recover state
            if datalen == 0:
                raise ConnectionError()

            raw = data[offset + 8:offset + datalen]
            yield (cmd, raw)
            offset += datalen

    def save_field_data(self, fieldname, contents):
        raw = contents
        wire_code = fieldname.decode()
        if wire_code == '*XFC' and not _is_tcp_transport(self.transport):
            # The OpenSwitcher TCP relay's transfer-complete event; no ATEM
            # sends it over UDP. Handled here it indexed the transfer queue:
            # with none queued for the store, a KeyError lost the rest of
            # the packet; with one, it popped a real transfer in flight and
            # could raise a spurious 'upload-done' (bmdwire#14). Dropped
            # before parsing, so a short one can't raise either.
            self.log.debug('Ignoring *XFC on a UDP session (a TCP-relay event)')
            return
        # Look up the Recv class by wire code via the registry built in
        # atemwire.messages. If a parser is registered we instantiate it
        # and use the pretty key from FIELDNAME_PRETTY; if no parser
        # exists (or the code is unmapped) we store the raw bytes under
        # the raw 4-char code, mirroring the original dispatch behavior.
        recv_cls = RECV_BY_CODE.get(wire_code)
        if recv_cls is not None and wire_code in self.FIELDNAME_PRETTY:
            key = self.FIELDNAME_PRETTY[wire_code]
            contents = recv_cls(contents)
        else:
            key = wire_code

        if key == 'lock-obtained':
            self.log.info('Got lock for {}'.format(contents.store))
            self.locks[contents.store] = True
            self._last_grant[contents.store] = time.monotonic()
            self._lock_requested.pop(contents.store, None)
            self._transfer_trigger(contents.store)
            return
        elif key == 'lock-state' and contents.state:
            # Lock taken: stored and raised as 'change:lock-state' by the
            # generic path below, like a release, but the transfer machinery
            # reacts to releases only. This used to return here, unstored
            # and silent, so mixerstate['lock-state'] only ever showed the
            # last release (bmdwire#15). The packet doesn't name the holder:
            # held by someone else is state=True while self.locks.get(store)
            # is falsy. Held by someone else, we don't ask for it until they
            # release it (LOCK_COURTESY), and a store we were leaving free
            # is theirs now.
            if not self.locks.get(contents.store):
                self._foreign_held.add(contents.store)
                granted = self._last_grant.get(contents.store)
                if granted is None or time.monotonic() - granted > LOCK_ECHO_WINDOW:
                    # Not the late announcement of a grant of ours (a transfer,
                    # or a clear, shorter than a frame): another client.
                    self._others_seen = time.monotonic()
                if contents.store in self._yielding:
                    self._yielding[contents.store] = [time.monotonic(), None]
                    # Theirs now: the next turn after their release is ours
                    # as a waiter, not as the releaser.
                    self._own_release.pop(contents.store, None)
        elif key == 'lock-state' and self.locks.get(contents.store):
            # A release broadcast while we hold the store is the late echo
            # of our own previous release: the switcher announces lock
            # changes once a frame, after the LKOB it sent for the request
            # that went out with that release. Our hold ends only when we
            # release it (an FTDE 5 says so if it somehow ended), so nothing
            # changes here; stored and raised below like any other.
            self.log.debug(f'{contents} while we hold store {contents.store}: '
                           f'our own earlier release, announced late')
        elif key == 'lock-state':
            self.log.debug(contents)
            self._foreign_held.discard(contents.store)
            # A release announced, ours or another client's: the store is
            # seen free, and a request waits LOCK_COURTESY more for anyone
            # else to take it. Another client releasing and taking the store
            # again for its next transfer (ASC does, in one packet) is
            # announced as a release then a lock, often in one packet: a
            # request sent on the release would be granted alongside theirs
            # and abort their transfer (Studio ATEM, 2026-10-15).
            y = self._yielding.get(contents.store)
            now = time.monotonic()
            if y is None and self._can_wake():
                self._yielding[contents.store] = [now, now]
                self._own_release.pop(contents.store, None)
            elif y is not None and y[1] is None:
                y[1] = now
                released = self._own_release.get(contents.store)
                if released is not None and now - released > LOCK_ECHO_WINDOW:
                    # Not our release's echo: another client's (LOCK_ECHO_WINDOW).
                    self._own_release.pop(contents.store, None)
            if contents.store in self._yielding and self.transfer_queue.get(contents.store):
                self._wake_at(now + LOCK_COURTESY)
            # The lock is free: whatever request we had outstanding is
            # either about to be granted (a queued PLCK — the switcher
            # grants it directly, sometimes without even broadcasting the
            # release) or was ignored (a LOCK sent while held). Let the
            # pounce below re-request either way; a duplicate is harmless.
            self._lock_requested.pop(contents.store, None)
            # THE POUNCE (ASC parity, 2026-07-07): the store lock just became
            # free — either our own per-frame release echoing back (see
            # _release_then_continue) or a FOREIGN holder (ASC, another
            # client, another session) letting go. If we have transfers
            # waiting, request the lock NOW: reacting to the release
            # broadcast in milliseconds is how ASC clients interleave
            # smoothly, versus our old blind per-20s-timeout retries that
            # ping-pong-starved against ASC's media page.
            if (self.transfer_queue.get(contents.store)
                    and not self.transfer_requested
                    and not self.locks.get(contents.store)):
                self._transfer_trigger(contents.store)
            # No early return: fall through to the generic path below so the
            # release is stored in mixerstate['lock-state'] and the
            # 'change:lock-state' event fires, exactly as upstream does.
        elif key == 'file-transfer-continue-data':
            self.transfer_budget = contents
            old = self.transfer_budget.size
            self.transfer_budget.size = self.transfer_budget.size // 8 * 8
            if old != self.transfer_budget.size:
                self.log.debug(f"Adjusted transfer chunk size from {old} to {self.transfer_budget.size}")
            if (self.transfer is not None and self.transfer.upload
                    and not self.transfer.metadata_sent):
                # FTFD with the first budget, before the data, as ATEM
                # Software Control sends it (restore capture, 2026-10-15;
                # MACRO_FORMAT.md found both orders work for macros). It
                # used to wait for the send queue to drain after the last
                # chunk, a round trip per upload.
                self._send_file_metadata()
            self._queue_chunks()
            return
        elif key == 'file-transfer-data':
            if self.transfer is None:
                # Straggler chunk after abort_transfers / lane reset — the
                # packet is already ACKed, so raising here would also drop
                # any state fields bundled in it (2026-07-06).
                self.log.debug('FTDa with no transfer in flight — ignoring')
                return
            if contents.transfer == self.transfer.tid:
                self.transfer_packets += 1
                self.transfer_buffer.append(contents.data)
                self.transfer_buffer_bytes += len(contents.data)
                if self.transfer_packets % 20 == 0:
                    # Progress is advisory and only meaningful for STILL
                    # downloads (fraction of a frame). Macro-store (0xffff)
                    # downloads have no frame size, and a raw KeyError on a
                    # missing video-mode would abort save_field_data
                    # mid-packet, dropping fields bundled behind the FTDa.
                    # An unknown video mode has no frame size (get_pixels()
                    # is None): no progress then, rather than a TypeError.
                    vm = self.mixerstate.get('video-mode')
                    pixels = vm.get_pixels() if vm is not None else None
                    if pixels and self.transfer.store != 0xffff:
                        total_size = pixels * 4
                        transfer_progress = self.transfer_buffer_bytes / total_size
                        self._raise('transfer-progress', self.transfer.store, self.transfer.slot, transfer_progress)
                # The 0 should be the transfer slot, but it seems it's always 0 in practice
                self.send_commands([TransferAckCommand(self.transfer.tid, 0)])
            else:
                self.log.error('Got file transfer data for wrong transfer id')
            return
        elif key == 'file-transfer-error':
            # Status 1 (try-again), 5 (no-lock) and 6 (lock taken by another
            # session) are part of the normal per-frame lock dance — the
            # machinery below recovers them. Only genuinely fatal statuses
            # deserve ERROR.
            if getattr(contents, 'status', None) in (1, 5, 6):
                self.log.debug(f"file-transfer-error: {str(contents)}")
            else:
                self.log.error(f"file-transfer-error: {str(contents)}")
            if self.transfer is None:
                # Straggler FTDE after abort_transfers / lane reset.
                self.log.debug('FTDE with no transfer in flight — ignoring')
                return
            if contents.transfer != self.transfer.tid:
                # A stale or foreign transfer id must not fail OUR
                # in-flight transfer (2026-07-06).
                self.log.debug(
                    f'FTDE for transfer {contents.transfer}, ours is '
                    f'{self.transfer.tid} — ignoring')
                return
            self.transfer_requested = False
            if contents.status == 1:
                # Status is try-again
                self.log.debug('Retrying transfer')
                self._transfer_trigger(self.transfer.store, retry=True)
            elif contents.status == 5:
                self.locks[self.transfer.store] = False
                self._transfer_trigger(self.transfer.store, retry=True)
            elif contents.status == 6:
                # Another session asked for the store while our transfer
                # was being set up: the switcher aborts OUR request with 6
                # but we STILL hold the lock (measured 2026-09-16 — two
                # clients requesting within ~150 ms: the first gets LKOB,
                # FTSU, then FTDE 6; the second's PLCK stays queued until
                # the first releases; re-requesting without releasing
                # deadlocked both for the full timeout). So: give the lock
                # back now, keep the task at the head of the queue, and let
                # the store's next release broadcast re-request it (the
                # pounce in the lock-state handler). Treating 6 as fatal
                # used to drop the task and leave the caller to time out —
                # 15-30 s per collision with ATEM Software Control or a
                # second control page on the same switcher.
                store = self.transfer.store
                self._lock_requested.pop(store, None)
                self._others_seen = time.monotonic()
                self._release_then_continue(store, request_next=False)
            else:
                # Fatal for THIS transfer (e.g. code 2 = rejected mode/slot):
                # retrying the same request would fail identically forever.
                # Historically the task was left at the queue head with the
                # store lock held — a wedged lane where every later transfer
                # queued behind the corpse until a caller timeout aborted.
                # Drop the failed task and trigger the next; when the queue
                # is empty the trigger's cleanup path releases the lock.
                if self.transfer is not None:
                    store = self.transfer.store
                    queue = self.transfer_queue.get(store) or []
                    if queue and queue[0] is self.transfer:
                        self.transfer_queue[store] = queue[1:]
                    self.transfer = None
                    self.transfer_buffer = []
                    self.transfer_buffer_bytes = 0
                    self._release_then_continue(store)
                # Tell subscribers (macrotransfer's upload waiter) the real
                # cause. Before this, the event was handled entirely
                # in-branch and NEVER raised — a fatal macro-upload
                # rejection surfaced as a bland 10 s TimeoutError instead
                # of "ATEM rejected macro upload (status=…)". Raised only
                # for FATAL statuses; the 1/5 lock-dance recoveries above
                # stay internal.
                self._raise('file-transfer-error', contents)
            return
        elif key == 'file-transfer-data-complete':
            self.log.debug('Transfer complete')
            if self.transfer is None:
                # The ATEM announces transfer completions to ALL sessions —
                # this is another session's transfer (watcher, uploader, a
                # sibling operator) finishing. Expected on a shared setup.
                self.log.debug("FTDC for another session's transfer — ignoring")
                self._someone_at_work()
                return
            if contents.transfer != self.transfer.tid:
                self._someone_at_work()
                return
            # Remove current item from the transfer queue
            store = self.transfer.store
            slot = self.transfer.slot
            upload = self.transfer.upload
            queue = self.transfer_queue[store]
            self.transfer_queue[store] = queue[1:]
            raw = b''.join(self.transfer_buffer)
            self.transfer_buffer = []
            self.transfer_buffer_bytes = 0
            self.transfer_requested = False

            # Give the store lock back FIRST, then decode and notify. The
            # RLE decode of a 1080p still is pure Python and took ~250 ms
            # measured — with the release after it, every frame held the
            # switcher's media lock a quarter second longer than the
            # transfer needed, on every thumbnail, against every other
            # client. The queue head is already popped, so this MUST run
            # before anything that can raise: the empty-queue unlock inside
            # the trigger is what frees the ATEM's media lock (2026-07-06).
            self._release_then_continue(store)
            try:
                if upload:
                    self._raise('upload-done', store, slot)
                else:
                    data = rle_decode(raw) if store == 0 else raw
                    self._raise('download-done', store, slot, data)
            except Exception:
                self.log.exception('transfer completion handling failed')
            return
        elif key == 'transfer-complete':
            self.log.debug('Proxy transfer complete')

            # Remove current item from the transfer queue
            queue = self.transfer_queue.get(contents.store) or []
            self.transfer_queue[contents.store] = queue[1:]

            if contents.upload:
                self._raise('upload-done', contents.store, contents.slot)
            else:
                # TODO: Implement proxy download
                pass
            # Start next transfer in the queue. contents.store, NOT
            # self.transfer.store — the TCP-proxy upload path never sets
            # self.transfer (it's None here), so the old line was a
            # guaranteed AttributeError on this (unused-in-production) path.
            self._transfer_trigger(contents.store)
            return
        elif key == 'InCm':
            self._check_dump_complete()

        if key in self.FIELDNAME_UNIQUE:
            idxes = self.FIELDNAME_UNIQUE[key].unpack_from(raw, 0)
            if key not in self.mixerstate:
                self.mixerstate[key] = {}

            # Fairlight strips have weird numbering that's harder to parse here, read it back from the class
            if hasattr(contents, 'strip_id'):
                idxes = list(idxes)
                idxes[0] = contents.strip_id
                idxes = tuple(idxes)

            unique = self.make_unique_dict(contents, idxes)
            self.mixerstate[key] = self.recursive_merge(self.mixerstate[key], unique)
            self._raise('change:' + key + ':' + str(idxes[0]), contents)
            self._raise('change:' + key + ':*', contents)
        else:
            self.mixerstate[key] = contents
            self._raise('change:' + key, contents)

        if key in LEGACY_AUDIO_DIAGNOSTIC_KEYS:
            self._log_legacy_audio(key, contents)

        if key == 'InCm':
            self.transport.mark_next_connected = True
            # TCP proxy fires 'connected' here, on InCm — UDP/USB fire
            # it later via the transport's ConnectionReady sentinel.
            if _is_tcp_transport(self.transport):
                self.initialized = True
                self._log_audio_mixer()
                self._log_topology()
                self._raise('connected')
        self._raise('change', key, contents)

    def _log_legacy_audio(self, key, contents):
        """Log the first of each legacy-audio packet (per AMIP source) at
        INFO, raw and decoded (messages/audio_legacy.py, "Diagnostics").
        Never raises: a log line must not cost the packet loop."""
        first = (key, getattr(contents, 'index', None))
        if first in self._legacy_audio_logged:
            return
        self._legacy_audio_logged.add(first)
        try:
            self.log.info('legacy-audio: %s', legacy_audio_diagnostic(contents))
        except Exception:
            self.log.debug('legacy-audio: could not describe %s', key, exc_info=True)

    def _log_audio_mixer(self):
        """At each connect, say which audio mixer the state dump showed:
        INFO for a legacy one or none, DEBUG for Fairlight."""
        mixer = audio_mixer_type(self.mixerstate)
        if mixer == 'fairlight':
            self.log.debug('Audio mixer at connect: fairlight')
            return
        note = '' if mixer == 'legacy' else ' (the state dump had neither FAMP nor AMMO)'
        self.log.info('legacy-audio: mixer at connect: %s%s', mixer, note)

    def _log_topology(self):
        """At each connect, log the ``_top`` payload, the protocol version
        and the block counts read from them at INFO, so a log from any
        model can confirm the layout (``system_info.TOPOLOGY_LAYOUTS``).
        Never raises."""
        try:
            self.log.info('topology at connect: %s', topology_diagnostic(self.mixerstate))
        except Exception:
            self.log.debug('topology at connect: could not describe it', exc_info=True)

    def _check_dump_complete(self):
        """At ``InCm``: raise ``IncompleteStateDump`` when the dump had no
        video mode, unless INCOMPLETE_DUMP_RETRIES re-handshakes in a row
        have already failed to get one; then log it and carry on, as the
        session did before."""
        if 'video-mode' in self.mixerstate:
            self._incomplete_dumps = 0
            return
        missing = ', '.join(k for k in _CORE_DUMP_FIELDS if k not in self.mixerstate)
        if self._incomplete_dumps < INCOMPLETE_DUMP_RETRIES:
            self._incomplete_dumps += 1
            raise IncompleteStateDump(
                f'State dump ended without {missing}, so its first packet '
                f'was probably lost (re-handshake {self._incomplete_dumps} '
                f'of {INCOMPLETE_DUMP_RETRIES})')
        self._incomplete_dumps = 0
        self.log.error(
            f'State dump ended without {missing} after '
            f'{INCOMPLETE_DUMP_RETRIES} re-handshakes; carrying on without it')

    def make_unique_dict(self, content, path):
        result = {}
        if len(path) == 1:
            result[path[0]] = content
        else:
            result[path[0]] = self.make_unique_dict(content, path[1:])
        return result

    def recursive_merge(self, d1, d2):
        '''update first dict with second recursively'''
        if not isinstance(d2, dict):
            return d2
        for k, v in d1.items():
            if k in d2:
                d2[k] = self.recursive_merge(v, d2[k])
        d1.update(d2)
        return d1

    def send_commands(self, commands):
        data = b''
        for command in commands:
            data += command.get_command()

        if len(data) > 1300:
            raise ValueError("Command list too long for UDP packet")

        self.send_raw(data)

    def send_raw(self, data):
        packet = Packet()
        packet.flags = UdpProtocol.FLAG_RELIABLE
        packet.data = data
        self.transport.send_packet(packet)

    def queue_callback(self, remaining, size):
        if not self.transfer:
            return

        self.transfer.send_done += size
        fraction = self.transfer.send_done / self.transfer.send_length
        self._raise('upload-progress', self.transfer.store, self.transfer.slot, fraction * 100, self.transfer.send_done,
                    self.transfer.send_length)

    def _reset_transfer_lane(self):
        """Offline reset of the transfer state machine — no wire sends.

        For session-death paths (disconnect sentinel, protocol corruption)
        where the old session's locks died with it on the ATEM side and
        sending unlock commands would target the WRONG (new) session.
        ``abort_transfers`` is the on-wire variant for live sessions."""
        self.transfer_queue = {}
        self.transfer = None
        self.transfer_requested = False
        self.transfer_buffer = []
        self.transfer_buffer_bytes = 0
        self.locks = {}
        self._lock_requested.clear()
        self._yielding.clear()
        self._own_release.clear()
        self._foreign_held.clear()
        self._last_grant.clear()

    def release_locks_now(self):
        """Give back every store lock we hold, SYNCHRONOUSLY, for teardown.

        Belt and braces. Measured 2026-09-16 (1 M/E Constellation HD): the
        goodbye alone releases a held lock within ~10 ms, and even an
        abandoned session's lock is reaped by the switcher in ~5 s, so this
        is not what "connect and find the store locked" was about (that
        was a lock GRANTED to us whose LKOB the transport dropped, fixed in
        ``transport.receive_packet``). It still costs nothing and protects
        the one case the goodbye datagram itself is lost.

        ``abort_transfers`` is not usable here: it releases through
        ``send_commands``, which queues for the worker thread, and on a
        close path that worker is already exiting. This writes straight to
        the socket the way the goodbye does (``_send_packet_low``), before
        the socket is closed. Best effort and never raises.

        Call it BEFORE ``transport.close_session()``.
        """
        held = [store for store, is_held in self.locks.items() if is_held]
        if not held:
            return
        for store in held:
            if store == 0xffff:          # macro store is lock-exempt
                continue
            try:
                packet = Packet()
                packet.flags = UdpProtocol.FLAG_RELIABLE
                packet.data = LockCommand(store, False).get_command()
                self.transport._send_packet_low(packet)
                self.log.info(f'Releasing lock {store} (teardown)')
            except Exception:
                self.log.debug('teardown lock release failed for store %s', store,
                               exc_info=True)
            self.locks[store] = False
        self._lock_requested.clear()
        self._yielding.clear()
        self._own_release.clear()

    def abort_transfers(self):
        """Best-effort reset of the transfer state machine, for callers that
        time out waiting on a queued transfer. Without this, an unanswered
        FTSU leaves ``transfer_requested`` True forever and every later
        transfer on this connection queues behind it ('Request already
        submitted, do nothing') — a wedged transfer lane on a connection
        that is otherwise healthy. Releases any store locks we believe we
        hold so the next transfer re-requests cleanly."""
        self.transfer_queue = {}
        self.transfer = None
        self.transfer_requested = False
        self.transfer_buffer = []
        self.transfer_buffer_bytes = 0
        # A lock request in flight must not outlive the abort: the queue it
        # was for is gone. Another client's turn does: a caller that timed
        # out waiting for it (WebATEM's media pool gives a download 15 s)
        # retries at once, and the switcher may have handed the store over
        # unannounced at our last release, so the retry still waits. A lock
        # released here leaves the store its turn as any release does.
        self._lock_requested.clear()
        for lock in list(self.locks):
            if self.locks[lock]:
                try:
                    self.send_commands([LockCommand(lock, False)])
                except Exception:
                    pass
                self.locks[lock] = False
                if self._can_wake():
                    self._yielding[lock] = [time.monotonic(), None]
                    self._own_release[lock] = time.monotonic()

    def download(self, store, index):
        # The native interleaved download: transfers ride the normal packet
        # loop alongside control traffic and state events — no
        # exclusive_access, no freeze (ASC-style shared-session path,
        # adopted 2026-07-02; still-capture uses it via
        # ATEMConnection.download_still).
        self.log.info("Queue download of {}:{}".format(store, index))
        if store not in self.transfer_queue:
            self.transfer_queue[store] = []
        self.transfer_queue[store].append(TransferTask(store, index))
        self._transfer_trigger(store)

    def queue_clear(self, store, index):
        # A locked ClearStill: rides the transfer machinery's lock
        # discipline (LOCK → LKOB → CSTL → per-frame release). The switcher
        # silently ignores a bare CSTL from a session that does not hold
        # the still-store lock (observed live 2026-07-29 on a 1 M/E
        # Production Studio 4K: bare CSTL with the lock FREE was dropped;
        # the same CSTL while holding the lock cleared immediately), so
        # this is the only reliable way to clear a slot. Returns the
        # queued task so callers can surgically dequeue it on timeout.
        self.log.info("Queue clear of {}:{}".format(store, index))
        if store not in self.transfer_queue:
            self.transfer_queue[store] = []
        task = TransferTask(store, index, clear=True)
        self.transfer_queue[store].append(task)
        self._transfer_trigger(store)
        return task

    def dequeue_clear(self, task):
        # Remove a not-yet-dispatched clear task (dispatch pops it before
        # sending CSTL, so a dispatched task is a no-op here). Callers use
        # this when their wait for dispatch times out: a stale clear task
        # firing much later could delete content an operator has since
        # uploaded into the recycled slot.
        queue = self.transfer_queue.get(task.store)
        if queue and task in queue:
            self.transfer_queue[task.store] = [
                t for t in queue if t is not task]
            return True
        return False

    def upload(self, store, index, data, compress=True, compressed=False, name=None, description=None, size=None,
               task=None):
        self.log.info("Queue upload of {}:{}".format(store, index))
        if store not in self.transfer_queue:
            self.transfer_queue[store] = []

        if task is None:
            task = TransferTask(store, index, upload=True)
            task.data = data
            task.send_length = len(data)
            task.name = name
            task.description = description
            if compressed:
                uncompressed = rle_decode(data)
                task.data = uncompressed
                task.calculate_hash()
                task.data = data
            else:
                task.calculate_hash()
            if compress:
                task.compress()
            elif compressed:
                task.data_length = len(rle_decode(data))

        self.log.info(f'New upload task is {len(task.data)} bytes, {task.data_length} uncompressed')

        if _is_tcp_transport(self.transport):
            self.transport.upload(task)
        else:
            self.transfer_queue[store].append(task)
            self._transfer_trigger(store)

    def _queue_chunks(self):
        # Can't transfer without a chunk size
        if self.transfer_budget is None:
            # Normal between budgets: the queue drained, the FTCD that
            # grants the next batch has not arrived yet. Not an error
            # (it was logged as one ten times per still upload).
            self.log.debug('Chunk budget exhausted — waiting for the next FTCD')
            return

        # Only queue chunks if an upload is planned
        if self.transfer is None:
            self.log.error('No transfer scheduled')
            return

        if not self.transfer.upload:
            self.log.error('Current transfer is a download')
            return

        chunk_size = self.transfer_budget.size
        self.log.debug(f'Queue {self.transfer_budget.count} chunks of {chunk_size}')
        data = memoryview(self.transfer.data)
        for i in range(0, self.transfer_budget.count):
            start = self.transfer.offset
            if start >= len(data):
                break

            chunk = data[start:start + chunk_size]
            used = chunk_size
            if chunk[-8:] == b'\xFE\xFE\xFE\xFE\xFE\xFE\xFE\xFE':
                chunk = data[start:start + chunk_size - 8]
                used -= 8
            elif chunk[-16:-8] == b'\xFE\xFE\xFE\xFE\xFE\xFE\xFE\xFE':
                chunk = data[start:start + chunk_size - 16]
                used -= 16
            self.transfer.offset = start + used

            self.transfer_budget.count -= 1
            if self.transfer_budget.count == 0:
                self.log.debug('Transfer budget ran out')
                self.transfer_budget = None

            cmd = TransferDataCommand(self.transfer.tid, bytes(chunk))
            packet = Packet()
            packet.flags = UdpProtocol.FLAG_RELIABLE
            packet.data = cmd.get_command()
            self.transport.queue_packet(packet)
        # The whole budget at once, back to back, as ATEM Software Control
        # sends it: the budget (FTCD) is the switcher's flow control, 768
        # chunks of a 1080p still at a time on a Constellation, and it
        # grants the next only once it has these. Paced at five chunks per
        # packet from the switcher (queue_trigger), a still took 23 s to
        # upload where ASC takes 0.5 s (docs/MEDIA_LOCK.md).
        self.transport.drain_queue()

    def _queue_flushed(self):
        self.log.info('Queue flushed')
        if self.transfer is None:
            # Straggler flush sentinel after abort_transfers nulled the
            # task (same guard FTDa/FTDE already carry).
            self.log.debug('Queue flushed with no transfer in flight — ignoring')
            return
        if self.transfer.offset < len(self.transfer.data):
            self._queue_chunks()
            return
        if not self.transfer.metadata_sent:
            # Not sent with the first budget (no FTCD came before the data
            # drained); the switcher needs it to finish the transfer.
            self._send_file_metadata()

    def _send_file_metadata(self):
        self.log.info('Sending file metadata')
        cmd = TransferFileDataCommand(self.transfer.tid, self.transfer.hash,
                                      name=self.transfer.name, description=self.transfer.description)
        self.send_commands([cmd])
        self.transfer.metadata_sent = True

    def _release_then_continue(self, store, request_next=True):
        """Per-frame lock discipline (ASC parity, 2026-07-07).

        Called when a transfer finishes (FTDC) or dies fatally (FTDE with a
        non-retryable status): release the store lock NOW instead of holding
        it across the rest of the queue. The old whole-queue hold meant a
        thumbnail sweep held the ATEM's media lock for its entire duration
        (measured 40s+ on a full pool) — during which ASC's media page shows
        LOCKED and its preview fetches starve (observed live 2026-07-07 on a
        freshly rebooted switcher). ASC holds per frame; now so do we.

        With more work queued for the store, its next lock request waits
        for the store's next LKST (see LOCK_COURTESY): another client that
        was waiting gets the store at our release, and a request of ours
        soon after would abort its transfer. 1.5.0 sent the release and the
        next request in one packet, as ASC sends them; that shut ASC out.
        ``request_next=False`` sends the release alone and leaves the next
        request to the store's release broadcast (the pounce): after an
        FTDE 6 another session is queued for the store, and asking again
        at once is what deadlocked two sessions. The macro store (0xffff)
        is lock-exempt and continues directly.
        """
        # The finished task is dealt with — clear it so a duplicate FTDC or
        # straggler FTDa during the echo window can't match its tid and
        # double-pop the queue.
        self.transfer = None
        if store != 0xffff and self.locks.get(store):
            self.log.info(f'Releasing lock {store} (per-frame)')
            self.locks[store] = False
            try:
                self.send_commands([LockCommand(store, False)])
            except Exception:
                self.log.exception('per-frame unlock send failed')
            if self._can_wake():
                # Every release, whether or not more work is queued now: a
                # caller that queues its transfers one at a time (WebATEM's
                # media pool watcher queues the next still after decoding the
                # last) asks again a moment after the release, with nothing
                # queued at it, and that request must wait its turn too. The
                # trigger defers it and asks to be woken.
                self._yielding[store] = [time.monotonic(), None]
                self._own_release[store] = time.monotonic()
            if not request_next:
                return  # the store's release broadcast continues the queue
        self._transfer_trigger(store)

    def _can_wake(self):
        return hasattr(self.transport, 'wake_at')

    def _wake_at(self, when):
        wake = getattr(self.transport, 'wake_at', None)
        if wake is not None:
            wake(when)

    def _yield_due(self, store):
        """When a store we are leaving free may be asked for again."""
        since, announced = self._yielding[store]
        if announced is not None:
            if store in self._own_release and self._others_recently():
                return announced + LOCK_COURTESY + LOCK_HANDOVER
            return announced + LOCK_COURTESY
        return since + LOCK_QUIET

    def _others_recently(self):
        return (self._others_seen is not None
                and time.monotonic() - self._others_seen < LOCK_QUIET)

    def _end_yield(self, store):
        self._yielding.pop(store, None)
        self._own_release.pop(store, None)

    def _someone_at_work(self):
        """Another session's transfer ended: a store we wait for, not yet
        announced free, is busy, and LOCK_QUIET starts again."""
        now = time.monotonic()
        self._others_seen = now
        for y in self._yielding.values():
            if y[1] is None:
                y[0] = now

    def _service_yields(self):
        """Ask for each store we left free whose yield is over; forget a
        yield that is over with nothing waiting on it. A Wakeup comes from
        callers too (WebATEM's pump sends one every 20 ms): a yield not yet
        over is only woken for again, its time perhaps moved by someone at
        work, so the trigger runs once per turn, not once per tick."""
        now = time.monotonic()
        for store in list(self._yielding):
            due = self._yield_due(store)
            if not self.transfer_queue.get(store):
                if now >= due:
                    self._end_yield(store)
            elif (self.transfer_requested or self.locks.get(store)
                    or store in self._foreign_held):
                continue                         # their release asks for us
            elif now < due:
                self._wake_at(due)
            else:
                self._transfer_trigger(store)

    def _transfer_trigger(self, store, retry=False):
        next = None

        self.log.info(f'transfer trigger for store {store} (retry={retry})')

        # Try the preferred queue
        if store in self.transfer_queue:
            if len(self.transfer_queue[store]) > 0:
                next = self.transfer_queue[store][0]

        # Try any queue
        if next is None:
            for store in self.transfer_queue:
                if len(self.transfer_queue[store]) > 0:
                    next = self.transfer_queue[store][0]
                    break

        self.log.info(f'next transfer: {next}')

        # All transfers done, clean locks
        if next is None:
            for lock in self.locks:
                if self.locks[lock]:
                    self.log.info('Releasing lock {}'.format(lock))
                    cmd = LockCommand(lock, False)
                    self.send_commands([cmd])
            return

        # Request a lock if needed
        if next.store != 0xffff and (next.store not in self.locks or not self.locks[next.store]):
            if next.store in self._foreign_held:
                # Another client's turn: its release's LKST asks for us.
                self.log.debug(f'Store {next.store} is another client\'s: '
                               f'asking once they release it')
                return
            if next.store in self._yielding:
                due = self._yield_due(next.store)
                if time.monotonic() < due:
                    self.log.debug(f'Leaving store {next.store} free a moment '
                                   f'for other clients')
                    self._wake_at(due)
                    return
                self._end_yield(next.store)
            asked = self._lock_requested.get(next.store)
            if asked is not None and time.monotonic() - asked < LOCK_REREQUEST_SECONDS:
                self.log.debug(
                    f'Lock request for {next.store} already on the wire — '
                    f'waiting for LKOB')
                return
            self.log.info('Requesting lock for {}'.format(next.store))
            # Clear tasks take the FULL store lock: CSTL was verified
            # honored under a LOCK grant; whether a PLCK per-slot grant
            # suffices is unverified, and a silently-dropped clear is
            # exactly the failure mode this path exists to prevent.
            if next.clear:
                cmd = LockCommand(next.store, True)
            else:
                cmd = PartialLockCommand(next.store, next.slot)
            self._lock_requested[next.store] = time.monotonic()
            self.send_commands([cmd])
            return

        # A transfer request is already running, don't start a new one
        if self.transfer_requested:
            self.log.info('Request already submitted, do nothing')
            return

        # A clear task has no FTSU/FTDC arc: with the lock held, pop it,
        # send the CSTL, and release per-frame — in-order delivery
        # guarantees the switcher processes the CSTL (as lock holder)
        # before the unlock. The LKST release echo advances the rest of
        # the queue via the pounce path, exactly like a finished transfer.
        if next.clear:
            queue = self.transfer_queue.get(next.store) or []
            if queue and queue[0] is next:
                self.transfer_queue[next.store] = queue[1:]
            self.log.info(
                'Clearing {}:{} under store lock'.format(next.store, next.slot))
            self.send_commands([ClearStillCommand(slot=next.slot)])
            self._raise('clear-dispatched', next.store, next.slot)
            self._release_then_continue(next.store)
            return

        # Assign a transfer id and start the transfer. A u16 on the wire,
        # never 0.
        if not retry:
            self.transfer_id = self.transfer_id % 0xFFFF + 1
        self.transfer = next
        self.transfer.tid = self.transfer_id
        if not self.transfer.upload:
            # Fresh accumulation for a new or RETRIED download — a
            # status-1 retry otherwise appends onto the partial chunks of
            # the failed attempt and corrupts the reassembled frame.
            self.transfer_buffer = []
            self.transfer_buffer_bytes = 0

        if self.transfer.upload:
            # Macro store (0xFFFF) requires mode 0x0300 (Write
            # uncompressed + Pre-erase) — mode 0x0001 (the still-store
            # default) is rejected with FTDE code 2. Verified against
            # ATEM Software Control's "Restore from State" capture.
            mode = 0x0300 if self.transfer.store == 0xFFFF else 1
            cmd = TransferUploadRequestCommand(self.transfer.tid, self.transfer.store, self.transfer.slot,
                                               self.transfer.data_length, mode)
            self.log.info('Requesting upload to {}:{}'.format(next.store, next.slot))
        else:
            cmd = TransferDownloadRequestCommand(self.transfer.tid, self.transfer.store, self.transfer.slot)
            self.log.info('Requesting download of {}:{}'.format(next.store, next.slot))
        self.transfer_requested = True
        if self.transfer.upload:
            # Each attempt sends its metadata and its data from the start.
            self.transfer.metadata_sent = False
            self.transfer.offset = 0
        outbound = [cmd]
        # Zero-length upload (e.g. clearing a macro slot via the
        # FTSD(len=0)+FTFD pattern that BMD uses for empty macros in
        # "Restore from State"): the ATEM does NOT send FTCD when
        # there are no chunks to size, so the normal
        # FTCD→_queue_chunks→queue_flushed→FTFD path never fires.
        # Verified against a Wireshark capture of that restore: the client sends FTSD
        # and FTFD at the same timestamp with no FTCD in between,
        # and the ATEM responds with FTDC. Send FTFD inline here so
        # the empty upload completes — without this, empty uploads
        # time out at upload_macro_bytecode's 10s deadline and leave
        # an orphaned transfer that poisons subsequent uploads.
        if self.transfer.upload and self.transfer.data_length == 0:
            self.log.info('Empty upload: sending FTFD inline (no FTCD expected)')
            outbound.append(TransferFileDataCommand(
                self.transfer.tid, self.transfer.hash,
                name=self.transfer.name,
                description=self.transfer.description,
            ))
            self.transfer.metadata_sent = True
        self.send_commands(outbound)
