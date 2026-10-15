# The media store lock, as the switcher actually behaves

Everything below was measured on the wire on 2026-09-16 against an ATEM
1 M/E Constellation HD, with a session that logged every `LKOB` / `LKST` /
`FTDE` / `FTDC` as it was decoded and every `LOCK` / `PLCK` / `FTSU` as it
was sent. Other models may differ; nothing here is inferred from
documentation. The OpenSwitcher protocol notes describe the same commands
but several of their claims (the automatic release of a partial lock, for
one) did not hold on this hardware.

## Requests and grants

* `LOCK store=0 state=1` is answered by `LKOB` in about 1 ms and then an
  `LKST locked` broadcast to every session.
* `PLCK store=0 slot=n` is answered the same way. It is **not released
  automatically** after one transfer: the explicit `LOCK state=0` is
  required, and its `LKST unlocked` echo arrives about 40 ms later.
* `LKST` goes out at the switcher's frame tick (every 40 ms at 25p), so a
  request sent with a release, or right after it, is granted (`LKOB`)
  **before** the release is broadcast. See "What ATEM Software Control
  does with an upload".
* A `PLCK` sent while another session holds the store is **queued by the
  switcher** and granted the moment the holder releases. In that case the
  requester receives `LKOB` directly and there may be **no `LKST unlocked`
  broadcast at all** between the two holders. A client must therefore act
  on `LKOB`, not only on the release broadcast.
* A `LOCK` (full store) or `PLCK` sent by a session that **already holds**
  the store gets no answer.
* A `LOCK state=0` from a session that does not hold the store does
  nothing visible (this was tested only while a foreign client held it).

## Two sessions at once

* Two sessions requesting within ~150 ms can **both** receive `LKOB`. The
  first `FTSU` to be serviced wins; the other transfer is aborted with
  **`FTDE status 6`**. That session **still holds the lock** after the 6;
  the winner's `PLCK` stays queued behind it. The only correct reaction to
  a 6 is: release, keep the task, and re-request when the store frees.
  Re-requesting without releasing deadlocks both sessions until their
  timeouts.
* With that rule two sessions interleave one frame at a time, exactly like
  two ATEM Software Control instances: four 1080p downloads across two
  sessions complete in 8.2 s, the wire floor.
* `FTDC` for a transfer is broadcast to **every** session, with the owner's
  transfer id. A session must match the id against its own transfer.

## Session end

* A lock still held when the session says goodbye (hello opcode 0x04) is
  released by the switcher within **10 ms**.
* A lock held by a session whose socket simply closed (no goodbye) is
  released when the switcher reaps the session, about **5 s** later. (The
  "five minutes" that older comments in this package repeated was never
  measured.)

## The state dump, and the bug that looked like a foreign lock

* The switcher answers a request sent **during** its initial state dump
  normally; the `LKOB` is the first packet after the dump.
* `atemwire.transport.receive_packet` used to throw that packet away: it
  pulled a packet from the queue, saw it owed the caller the
  `ConnectionReady` sentinel (the packet after `InCm`), returned the
  sentinel and dropped the packet. The switcher retransmitted the `LKOB`
  every 80 ms, and every copy was deduplicated as already received. The
  session then **held the media lock without knowing it** for the rest of
  its life, and every other client, ATEM Software Control included, was
  refused the store. From the outside that is "locked by another instance"
  with nobody else there. The same shape existed for the
  `TransferQueueFlushed` sentinel mid-upload. Fixed in 1.0.3: a displaced
  packet is kept and handed out on the next call.
* `atemwire.ready.wait_ready` used to return on the **first** packet of the
  dump (`protocol.connected` flips on any data packet; `video-mode` is in
  that first packet). Since 1.0.3 it waits for the whole dump
  (`protocol.initialized`, set when `InCm` has been processed). A session
  on a LAN reports ready in ~0.5 s with ~107 state keys, where it used to
  report ready in 4 ms with 25.
* Until 1.0.3 the dump itself was not ACKed until the switcher's first
  no-data packet after it. Any dump slower than the switcher's ~80 ms
  retransmit timer was resent in full, repeatedly. Reliable packets are now
  ACKed as soon as the session is established.

## Timing that is ours, not the switcher's

* A 1080p still downloads in ~1.75 s from `FTSU` to `FTDC`. The RLE decode
  that follows is pure Python and takes ~250 ms on the connection's worker
  thread. Since 1.0.3 the lock is released **before** that decode; before,
  every frame held the store a quarter second longer than the transfer
  needed. The decode still blocks the worker for those 250 ms; moving it
  into the C extension is the remaining improvement.

## What ATEM Software Control does with the media pool

A Wireshark capture on the ASC machine, 2026-10-14: ATEM Software Control
connecting to the same model (firmware 9.2.1, 1080p25) with 20 stills in
the pool, 8 different pictures, then saving a profile.

* **On connecting, ASC downloads every used still in full**, one after
  another, starting 0.75 s after its hello (once the dump is in): 20
  transfers of 8.32 MB, about 5,940 `FTDa` chunks of about 1,400 bytes
  each, **1.78 to 1.82 s apiece from `FTSU` to `FTDC`**, with 17 to 39 ms
  (median 24) from one `FTDC` to the next `FTSU`. It downloads repeated
  pictures too: 20 transfers for 8 pictures.
* It takes the **whole store** for each still, `LOCK store=0 state=1`
  (the last byte varied: `0x64`, `0xff`), and releases it with
  `LOCK store=0 state=0` after each `FTDC`. It never sent a `PLCK`.
* Every `FTDa` is acknowledged with an `FTUA`; its second field is
  nonzero and varies, where atemwire sends 0. Up to 153 chunks were in
  flight before an acknowledgement.
* `FTSU`'s last four bytes were `00 ef 67 70` for a still and
  `03 xx xx 00` for a macro (atemwire sends `00 d0 9b 8c` for a still).
* **Saving the profile, 61 s in, downloaded only the macros**: the six
  used macro slots (three name-only, no ops), in 0.5 s, and **no still**.
  ASC's quick save writes the stills it downloaded on connecting.

So a still comes off this switcher at its own pace, about 1.8 s for 1080p,
for ATEM Software Control as for atemwire (a client of this package,
bmdctl, measured 1.80 to 1.83 s on the same pool). The wire floor above
holds for one session too. Nothing in the capture is a faster transfer:
the whole-store lock, the acknowledgement's second field and `FTSU`'s
suffix cost the same time. A client that wants a quick save keeps the
stills it has downloaded, which is what `download_media_pool_images`'s
`cache_get` and `cache_put` are for (WebATEM 2.1.0 keeps them on disk by
hash), rather than looking for a faster download.

## What ATEM Software Control does with an upload

A Wireshark capture on the ASC machine, 2026-10-15: ATEM Software Control
restoring a profile with 20 stills (8 pictures) to the same model
(firmware 9.2.1, 1080p25), into a pool already holding them but for two
slots changed beforehand.

* **It uploads every still the profile names**, changed or not: 20
  uploads. It doesn't skip a slot that already holds its picture.
* **Twenty photographic 1080p stills in about 12 s.** Each sends 0.31 s of
  data (5,955 to 5,958 chunks, 8.29 MB: RLE barely shortens a
  photograph), takes 0.52 to 0.60 s from `FTSD` to `FTDC`, and the next
  `FTSD` follows 35 to 54 ms after the `FTDC`. A client of this package,
  pacing five chunks per packet from the switcher, took 23 s a still.
* **The handover.** 1 to 5 ms after each `FTDC`, `LOCK store=0 state=0`;
  the next still's `PLCK` in the same packet (8 of 19 times) or the next
  one, 3 to 21 ms after the `FTDC`. `LKOB` comes 0.3 to 0.7 ms after the
  `PLCK`, every time, before any `LKST`. The release's `LKST` comes at the
  switcher's next frame tick (they sit on a 40 ms grid), after that
  `LKOB` and often after the next `FTSD`: `state=0` with the released slot
  in byte 3, then `state=1`, or two `state=1` when the release and the
  request shared a packet. ASC holds the lock from `LKOB` to its own
  release whatever `LKST` says.
* Its downloads on connecting hand over the same way with the whole store
  (the 2026-10-14 capture): `LOCK state=0` and `LOCK state=1` in one
  packet, `LKOB` 0.8 ms later.
* **`FTSD`** follows the `LKOB` by 10 to 35 ms (ASC's own pace), not the
  `LKST`. Its bytes are atemwire's: transfer id, store 0, slot,
  `0x007e9000` (the 8,294,400-byte frame), mode 1.
* **`FTFD` goes with the first budget**: 0.1 to 1.6 ms after the first
  `FTCD` when that came about 30 ms after the `FTSD`, or among the first
  150 to 424 chunks when it came within 6 ms. Never after the data. Its
  name and description fields carry leftover memory after the NUL.
* **Budgets.** `FTCD` grants 768 chunks of 1,396 bytes (a few 601 to 760),
  nine per still. ASC sends each whole, one `FTDa` per packet, back to
  back: 768 chunks in about 35 ms, 24 µs apart (median). The next `FTCD`
  comes about 5 ms after the last chunk. The switcher acknowledged about
  every 16 packets, at most 88 behind.
* `PLCK`'s bytes 4 to 7 were `ab 01 a9 9b` in every request, where the
  2026-09-16 notes found `ff 01 00 00`: byte 5 is the constant.
* Nothing was lost or resent either way. ASC's acknowledgement-only
  packets carry sequence number 0, so nothing tells the switcher that a
  last packet went missing until it is sent again: a client resends what
  isn't acknowledged on a timer, as the switcher does (every ~80 ms).

### The lock request that seemed to be eaten

From 2026-07-07 atemwire held back a lock request until the `LKST` echo of
its own release, believing the switcher silently drops a request that
arrives while an unlock is in flight. ASC sends exactly that request, every
time, and the switcher grants it. What happened was the order above: the
release's broadcast comes after the grant, and atemwire's lock-state
handler took a release broadcast as the end of its own hold. It forgot the
new lock, skipped the release after that transfer, and asked again as the
holder, which the switcher ignores (see "Requests and grants"). Waiting
for the echo hid it, at a frame per transfer at best and up to 6.6 s
measured, during a 20-still restore by bmdctl, which mirrors this code.
Since `bd6ae77` atemwire holds the lock from `LKOB` to its own release, as
ASC does, and sends the release and the next request in one packet.

