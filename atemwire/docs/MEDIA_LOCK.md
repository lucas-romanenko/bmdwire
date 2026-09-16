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
