# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
import logging
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
# a queued PLCK answers the moment the holder releases; a full LOCK sent
# while another session holds the store is simply dropped by the switcher.
LOCK_REREQUEST_SECONDS = 1.0


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
            Only meaningful for UDP; TCP and USB ignore the flag.
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

        self.locks = {}
        # Stores whose unlock we have SENT but whose LKST release echo has
        # not yet arrived. A lock request that reaches the switcher while
        # its own unlock is still in flight is silently eaten — and with
        # the lock then free, no further LKST ever arrives to pounce on:
        # dead air until the caller's timeout (observed live 2026-07-07,
        # back-to-back downloads). _transfer_trigger defers requests for
        # these stores; the LKST echo clears the entry and re-triggers.
        self._lock_release_pending = set()
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
        self.mode = None
        self.transfer_queue = {}
        self.transfer_id = 42
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
            self._raise('connected')
            return
        if isinstance(packet, Wakeup):
            # Caller-side wake-up to drain a pending outbound command — the
            # worker thread (ATEMConnection._run) will pick up the queue
            # right after this returns. No state to mutate here.
            return
        self.connected = True
        if isinstance(packet, TransferQueueFlushed):
            self._queue_flushed()
            return
        try:
            for fieldname, data in self.decode_packet(packet.data):
                self.save_field_data(fieldname, data)
        except ConnectionError:
            # The old handler claimed "closing connection" but closed
            # nothing: the session stayed ESTABLISHED with a gutted
            # mixerstate the ATEM never re-dumps. Say goodbye and
            # re-handshake so a fresh full state dump arrives
            # (2026-07-06).
            self.log.error(
                'Protocol corruption — forcing a clean session reconnect')
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
            self._lock_requested.pop(contents.store, None)
            self._transfer_trigger(contents.store)
            return
        elif key == 'lock-state':
            if contents.state:
                # Ignore lock aquired messages from other clients
                return
            if contents.store in self.locks and self.locks[contents.store]:
                # Remove the lock if we held it
                del self.locks[contents.store]
            self.log.debug(contents)
            # Our per-frame unlock is now confirmed processed — requests
            # for this store are safe to send again.
            self._lock_release_pending.discard(contents.store)
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
                    vm = self.mixerstate.get('video-mode')
                    if vm is not None and self.transfer.store != 0xffff:
                        total_size = vm.get_pixels() * 4
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
                self._release_then_continue(store)
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
                return
            if contents.transfer != self.transfer.tid:
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
            queue = self.transfer_queue[contents.store]
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

        if key == 'InCm':
            self.transport.mark_next_connected = True
            # TCP proxy fires 'connected' here, on InCm — UDP/USB fire
            # it later via the transport's ConnectionReady sentinel.
            if _is_tcp_transport(self.transport):
                self.initialized = True
                self._raise('connected')
        self._raise('change', key, contents)

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
        self._lock_release_pending.clear()
        self._lock_requested.clear()

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
        self._lock_release_pending.clear()
        self._lock_requested.clear()

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
        # A pending release-echo wait must not outlive the abort: the queue
        # it was deferring for is gone, and the next caller's fresh request
        # must not be deferred against a stale mark.
        self._lock_release_pending.clear()
        self._lock_requested.clear()
        for lock in list(self.locks):
            if self.locks[lock]:
                try:
                    self.send_commands([LockCommand(lock, False)])
                except Exception:
                    pass
                self.locks[lock] = False

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
        for i in range(0, self.transfer_budget.count):
            if len(self.transfer.data) == 0:
                break

            chunk = self.transfer.data[0:chunk_size]
            used = chunk_size
            if chunk[-8:] == b'\xFE\xFE\xFE\xFE\xFE\xFE\xFE\xFE':
                chunk = self.transfer.data[0:chunk_size - 8]
                used -= 8
            elif chunk[-16:-8] == b'\xFE\xFE\xFE\xFE\xFE\xFE\xFE\xFE':
                chunk = self.transfer.data[0:chunk_size - 16]
                used -= 16
            self.transfer.data = self.transfer.data[used:]

            self.transfer_budget.count -= 1
            if self.transfer_budget.count == 0:
                self.log.debug('Transfer budget ran out')
                self.transfer_budget = None

            cmd = TransferDataCommand(self.transfer.tid, chunk)
            packet = Packet()
            packet.flags = UdpProtocol.FLAG_RELIABLE
            packet.data = cmd.get_command()
            self.transport.queue_packet(packet)
        self.transport.queue_trigger()

    def _queue_flushed(self):
        self.log.info('Queue flushed')
        if self.transfer is None:
            # Straggler flush sentinel after abort_transfers nulled the
            # task (same guard FTDa/FTDE already carry).
            self.log.debug('Queue flushed with no transfer in flight — ignoring')
            return
        if len(self.transfer.data):
            self._queue_chunks()
            return
        self.log.info('Sending file metadata')
        cmd = TransferFileDataCommand(self.transfer.tid, self.transfer.hash,
                                      name=self.transfer.name, description=self.transfer.description)
        self.send_commands([cmd])

    def _release_then_continue(self, store):
        """Per-frame lock discipline (ASC parity, 2026-07-07).

        Called when a transfer finishes (FTDC) or dies fatally (FTDE with a
        non-retryable status): release the store lock NOW instead of holding
        it across the rest of the queue. The old whole-queue hold meant a
        thumbnail sweep held the ATEM's media lock for its entire duration
        (measured 40s+ on a full pool) — during which ASC's media page shows
        LOCKED and its preview fetches starve (observed live 2026-07-07 on a
        freshly rebooted switcher). ASC holds per frame; now so do we.

        The next queued transfer is NOT started here: our own unlock's LKST
        echo (~20ms) re-triggers via the lock-state handler's pounce path —
        sequencing the re-request after the release is processed, since a
        request that lands while an unlock is in flight is silently dropped
        by the switcher. Between those frames, any other client can win the
        lock; our next request then waits for THEIR release broadcast. The
        macro store (0xffff) is lock-exempt and continues directly.
        """
        # The finished task is dealt with — clear it so a duplicate FTDC or
        # straggler FTDa during the echo window can't match its tid and
        # double-pop the queue.
        self.transfer = None
        if store != 0xffff and self.locks.get(store):
            self.log.info(f'Releasing lock {store} (per-frame)')
            try:
                self.send_commands([LockCommand(store, False)])
                # Requests sent while this unlock is in flight get eaten by
                # the switcher — defer them until the LKST echo confirms.
                self._lock_release_pending.add(store)
            except Exception:
                self.log.exception('per-frame unlock send failed')
            self.locks[store] = False
            return  # LKST echo continues the queue
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
            if next.store in self._lock_release_pending:
                # Our own unlock for this store is still in flight — a
                # request sent now would be silently eaten by the switcher
                # and nothing would ever wake us (the lock ends up free, so
                # no further LKST arrives). The release echo clears the
                # pending mark and re-triggers via the pounce path.
                self.log.debug(
                    f'Deferring lock request for {next.store} until the '
                    f'release echo lands')
                return
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

        # Assign a transfer id and start the transfer
        if not retry:
            self.transfer_id += 1
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
        self.send_commands(outbound)
