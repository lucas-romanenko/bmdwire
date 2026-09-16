# ATEM Macro Recording / Playback / Upload Protocol

This is a reverse-engineered reference for how Blackmagic ATEM switchers
record, store, play back, **and accept direct upload of** macros over
the wire protocol. Blackmagic does not publish a schema. The recording
side was reconstructed from a Wireshark capture of an ATEM Software
Control "record" session; the upload side from a "Restore from
Switcher State" capture (2026-04-29). All paths
verified live against an ATEM 1 M/E Constellation HD.

> **Headline findings.**
>
> 1. ATEM has **two paths** for getting a macro into a slot:
>    * **Recording.** Send `MSRc` (start record), issue normal control
>      commands (`CPgI`, `CKOn`, `DCut`, ...), then `MAct` action 0x02
>      to stop. Whatever lands on the wire while recording is live is
>      captured into the slot. Drives live state through every recorded
>      op as a side effect.
>    * **Bytecode upload.** Send `FTSD` (start upload, store=0xFFFF,
>      mode=0x0300), then `FTDa` chunks of the LE bytecode, then
>      `FTFD` with name/description/MD5. ATEM responds with `FTDC`.
>      No live state changes — the bytecode lands directly in the slot.
> 2. The **upload mode** is the trick: the macro store rejects
>    `mode=0x0001` (Write RLE, what the still store accepts) with
>    `FTDE` code 2 ("not-found"). Macros need `mode=0x0300` (Write
>    uncompressed + Pre-erase). This is the only firmware quirk that
>    matters for the upload path.
> 3. **Encoder symmetry.** The bytecode format is identical in both
>    directions — every op the decoder produces (`<Op id="..."
>    attrs="..."/>`) round-trips through `encode_macro_bytecode` to
>    byte-identical bytes (verified on slot 0 "WIDE CAM": 712 bytes,
>    52 ops, full round-trip). Marker bytes that earlier reverse-
>    engineering captured as `0x028e` / `0x026c` / `0x000c` were
>    artifacts of synthesized test commands; real Software Control
>    recordings use `0x0000`.
> 4. `atemwire.profile.Profile.apply` defaults to the **upload path**.
>    The recording-based ops (`macro_start_recording`,
>    `macro_stop_recording`, `macro_sleep`) are still in
>    `atemwire.operations` for callers who need them, but the macro
>    apply path no longer uses them except as an empty-macro fallback.

## Recording lifecycle

```
client                                                   ATEM
  │                                                       │
  │  MSRc { slot, name, description }                     │
  │ ────────────────────────────────────────────────────► │
  │                                                       │  enters
  │                                                       │  recording
  │                                                       │  mode
  │  CPgI(input=Camera1)                                  │
  │ ────────────────────────────────────────────────────► │
  │  CKOn(keyer=0, on_air=True)                           │  records
  │ ────────────────────────────────────────────────────► │  every
  │  CKeF(keyer=0, source=MediaPlayer1)                   │  control
  │ ────────────────────────────────────────────────────► │  command
  │  ...                                                  │
  │  MSlp(frames=60)                                      │  records
  │ ────────────────────────────────────────────────────► │  pause
  │  ...                                                  │
  │  MAct(index=0xFFFF, action=0x02)  ◄── stop record     │
  │ ────────────────────────────────────────────────────► │
  │                                                       │  persists
  │                                                       │  + emits
  │  ◄──────────────────────── MPrp { slot, name, ... }   │  MPrp
  │                                                       │
```

The MPrp confirmation typically arrives within ~10-100 ms of the stop.
Multiple MPrp packets may be received as the ATEM rebroadcasts state.
The first one with `is_used=True` and the recorded name is the
confirmation.

### Settle timing

A small delay after `MSRc` is required before the ATEM is ready to
accept control commands as part of the recording. Empirically ~150 ms
is sufficient on a Constellation HD; without the delay, the first
1-2 control commands occasionally don't make it into the saved macro.
`atemwire.profile._apply_macros` sleeps for `0.15` s after every
`MSRc` and again before the `MAct` stop to let the worker thread
fully drain.

## Wire commands

### `MSRc` — Start Recording

Toward the ATEM. Header is 6 bytes; the name + description follow
immediately and are padded to 4-byte alignment:

| Offset | Size | Type   | Description                       |
|--------|------|--------|-----------------------------------|
| 0      | 2    | u16 BE | Macro slot index (0-based)        |
| 2      | 2    | u16 BE | Name length (bytes, no NUL)       |
| 4      | 2    | u16 BE | Description length (bytes)        |
| 6      | N    | utf-8  | Name                              |
| 6+N    | M    | utf-8  | Description                       |
| ...    | 0–3  | u8     | Padding to 4-byte alignment       |

> **Earlier reading of the format mistakenly treated bytes 6-7 as
> padding** — that interpretation came from a capture where the
> description length happened to be 0, so bytes 6-7 were both `00 00`.
> Live tests against the ATEM confirmed the header is 6 bytes and the
> name follows immediately after byte 5. There is no fixed-size pad
> between the header and the name.

Example: record into slot 10 with name `"TEST MACRO"`, no description:
```
00 0a 00 0a 00 00 54 45 53 54 20 4d 41 43 52 4f
slot  nlen  dlen  T  E  S  T  ' ' M  A  C  R  O
```

Recording state is global — the ATEM only records into one slot at a
time. Sending `MSRc` while another recording is active is undefined
in the captures we have; the safe pattern is `MSRc → MAct(stop) →
MSRc` for the next slot.

### `MSlp` — Macro Sleep

Toward the ATEM. Records a wait into the active recording. Has no
effect outside an active recording.

| Offset | Size | Type   | Description                |
|--------|------|--------|----------------------------|
| 0      | 4    | u32 BE | Frame count                |

Frames are display frames at the switcher's current refresh rate.
On 1080p60 (display rate 30 fps in ATEM Software Control's
convention), `MSlp(frames=60)` produces a 2-second pause. Verified
live: a `frames=60` sleep on a 1080p60 ATEM stalls macro playback
for ~2.0 s between consecutive ProgramInput ops, matching the
calculation.

### `MAct` — Macro Action

Toward the ATEM. Drives macro playback and recording lifecycle.

| Offset | Size | Type   | Description                       |
|--------|------|--------|-----------------------------------|
| 0      | 2    | u16 BE | Slot index, or `0xFFFF` for "active recording" |
| 2      | 1    | u8     | Action code                        |
| 3      | 1    | u8     | padding (`00`)                     |

Action codes:

| Code | Name                | Effect                                   | Verified |
|------|---------------------|------------------------------------------|----------|
| 0x00 | `RUN`               | Play the macro at the given slot         | ✅ |
| 0x01 | `STOP_RUN`          | Stop a running macro (`index=0xFFFF`)    | UNTESTED |
| 0x02 | `STOP_RECORD`       | Stop the active recording, persist macro | ✅ |
| 0x03 | `INSERT_USER_WAIT`  | Insert a "wait for input" mark in the recording | UNTESTED |
| 0x04 | `CONTINUE`          | Resume after a `STOP_AND_RESUME` checkpoint | UNTESTED |

> Action code 0x05 (`DELETE`) is also referenced in upstream OpenAtem
> code but unverified here. The safe way to "delete" a macro is to
> overwrite it via a fresh `MSRc → MAct(stop)` cycle with an empty
> command sequence — this clears the slot's content but leaves the
> name. Verified live: re-recording overwrites whatever was there.

### `MPrp` — Macro Properties (received)

This packet is what the ATEM emits to describe each macro slot. atemwire
already parses it via `atemwire.messages.macros.MacroPropertiesField`. The
bytecode-upload path receives an MPrp implicitly through `FTDC` — no
separate wait is needed. The recording path used to wait for MPrp
after `MAct(stop_record)` to confirm; that helper was removed when
the apply path moved to bytecode upload.

## XML profile mapping

ATEM Software Control's "Save Switcher State" XML serializes each
macro as a `<Macro>` element with `<Op>` children. Each `<Op id="...">`
is one of the commands recorded into the macro:

```xml
<Macro index="0" name="Deal Cam" description="">
    <Op id="ProgramInput" mixEffectBlockIndex="0" input="Camera1"/>
    <Op id="KeyOnAir" mixEffectBlockIndex="0" keyIndex="0" onAir="True"/>
    <Op id="KeyFillInput" mixEffectBlockIndex="0" keyIndex="0" input="MediaPlayer1"/>
    <Op id="KeyCutInput" mixEffectBlockIndex="0" keyIndex="0" input="MediaPlayer1Key"/>
    <Op id="MediaPlayerSourceStillIndex" mediaPlayer="0" index="0"/>
    <Op id="MediaPlayerSourceStill" mediaPlayer="0"/>
</Macro>
```

To restore: encode the `<Op>` children to LE bytecode via
`encode_macro_bytecode`, then upload via `upload_macro_bytecode(protocol,
slot, name, description, bytecode)` (FTSD/FTCD/FTDa/FTFD/FTDC,
mode=0x0300). No live state changes during apply.

Alternatively, the recording path still works: `MSRc("Deal Cam")` →
issue the corresponding atemwire operation for each `<Op>` →
`MAct(stop_record)`. Use this when you want the macro to capture
side-effects of compound operations the encoder doesn't know about
yet, accepting that program/USK/DSK will twitch through every op
during recording.

## Op-ID encoder / decoder coverage

`atemwire.macrotransfer._KNOWN_OPS` is the single source of truth for
the mapping. Each entry is a 3-tuple `(xml_id, decoder, encoder)`:
the **decoder** turns op_code + params into a dict of XML attributes,
and the **encoder** is its inverse. A unit test
(`test_known_ops_table_entries_are_3_tuples`) enforces the 3-tuple
shape so a future addition can't silently land with no encoder.

Since 2026-04-29, profile restore uploads the **bytecode** directly
instead of re-recording, so per-op handler functions (the old
`_MACRO_OP_HANDLERS` in `profile.py`) are no longer needed and have
been removed. The encoder is the only thing that needs to know about
each op-id.

### Two encoder entry points

- **`encode_single_op(op_dict) -> bytes`** — encode one op (4-byte
  header + params). Raises `ValueError` if the op id has no encoder
  or required attrs are missing. Pure function; never swallows.
- **`encode_macro_bytecode(ops) -> bytes`** — strict batch: any
  unknown op aborts the whole list. Equivalent to
  `b''.join(encode_single_op(op) for op in ops)`.

The apply path (`atemwire.profile._apply_macros`) calls
`encode_single_op` per op so it can decide per-op what to do with
unknown ops (current policy: drop them, upload the rest, report
which ops were dropped). Bulk callers that want strict
all-or-nothing semantics use `encode_macro_bytecode` directly.

### Currently covered (143 op-ids — full encode/decode round-trip)

Comprehensive close-out 2026-04-30: the macro-recordable surface of
the 1 M/E Constellation HD is fully covered. A Software Control
recording that exercises every op type (transitions across all 5
styles, color generators, fade-to-black, USK mask + fly-key + DVE
+ border + shadow, full Advanced Chroma Key, fly-key keyframe ops,
Fairlight EQ + dynamics processor + headphones) round-trips
**168/168 ops byte-identically** through the encoder/decoder.

**Bus / transition**
| Op id             | Operation                       |
|-------------------|---------------------------------|
| `ProgramInput`    | `set_program`                   |
| `PreviewInput`    | `set_preview`                   |
| `Cut`             | `cut`                           |
| `Auto`            | `auto`                          |
| `FadeToBlack`     | `fade_to_black`                 |
| `TransitionStyle` | `set_transition_style`          |
| `TransitionSource`| `set_next_transition_layers` (absolute mask in one packet) |

**Aux routing**
| Op id            | Operation        |
|------------------|------------------|
| `AuxiliaryInput` | `set_aux_output` |

**Upstream keyer (USK)**
| Op id                    | Operation                         |
|--------------------------|------------------------------------|
| `KeyOnAir`               | `set_usk_on_air`                  |
| `KeyType`                | `set_usk_type`                    |
| `KeyFillInput`           | `set_usk_fill_source`             |
| `KeyCutInput`            | `set_usk_key_source`              |
| `KeyMaskEnable`          | `set_usk_mask_enabled`            |
| `KeyFlyEnable`           | `set_usk_fly_enabled`             |
| `LumaKeyPreMultiply`     | `set_usk_luma_pre_multiplied`     |
| `LumaKeyInvert`          | `set_usk_luma_invert`             |
| `LumaKeyClip`            | `set_usk_luma_clip` (×100 to %)   |
| `LumaKeyGain`            | `set_usk_luma_gain` (×100 to %)   |

**USK DVE / fly key**
| Op id                       | Operation                  |
|-----------------------------|-----------------------------|
| `DVEAndFlyKeyXSize`         | `set_usk_dve_size_x`        |
| `DVEAndFlyKeyYSize`         | `set_usk_dve_size_y`        |
| `DVEAndFlyKeyXPosition`     | `set_usk_dve_position_x`    |
| `DVEAndFlyKeyYPosition`     | `set_usk_dve_position_y`    |
| `DVEKeyMaskEnable`          | `set_usk_dve_masked`        |
| `DVEKeyMaskTop`             | `set_usk_dve_top`           |
| `DVEKeyMaskBottom`          | `set_usk_dve_bottom`        |
| `DVEKeyMaskLeft`            | `set_usk_dve_left`          |
| `DVEKeyMaskRight`           | `set_usk_dve_right`         |

**Downstream keyer (DSK)**
| Op id                     | Operation               |
|---------------------------|--------------------------|
| `DownstreamKeyOnAir`      | `set_dsk_on_air`        |
| `DownstreamKeyAuto`       | `dsk_auto`              |
| `DownstreamKeyFillInput`  | `set_dsk_fill_source`   |
| `DownstreamKeyCutInput`   | `set_dsk_key_source`    |
| `DownstreamKeyRate`       | `set_dsk_rate`          |
| `DownstreamKeyMaskEnable` | `set_dsk_mask_enabled`  |
| `DownstreamKeyPreMultiply`| `set_dsk_pre_multiplied`|
| `DownstreamKeyClip`       | `set_dsk_clip` (×100 to %)|
| `DownstreamKeyGain`       | `set_dsk_gain` (×100 to %)|

**Media player**
| Op id                          | Operation                         |
|--------------------------------|------------------------------------|
| `MediaPlayerSourceStillIndex`  | `set_media_player_still`           |
| `MediaPlayerSourceStill`       | (no-op — covered by previous op)   |

**Macro flow**
| Op id        | Operation        |
|--------------|------------------|
| `MacroSleep` | `macro_sleep`    |

**Fairlight audio mixer** (added 2026-04-30 from live discovery on a production switcher)
| Op id                                       | Operation                       |
|---------------------------------------------|---------------------------------|
| `FairlightAudioMixerInputSourceFaderGain`   | `set_fairlight_strip(volume=…)` |
| `FairlightAudioMixerInputSourceMixType`     | `set_fairlight_strip(state=…)`  |
| `FairlightAudioMixerMasterOutFaderGain`     | `set_fairlight_master(volume=…)`|

**Video mode + fade-to-black enable** (added 2026-04-30 from live discovery)
| Op id                | Operation             |
|----------------------|-----------------------|
| `VideoMode`          | `set_video_mode`      |
| `FadeToBlackEnabled` | `set_ftb_disabled`    |

Note: op-code `0x000c` also surfaces as an auto-prologue at the head
of macros recorded by Software Control — it captures the video mode
that was active when recording began. Round-trip via `Profile.from_atem`
will surface that prologue as an extra `<Op id="VideoMode"/>` at the
head of any macro that didn't have one in the source XML; harmless
because the value is the current mode (replaying re-asserts the same
mode, a functional no-op).

### Op IDs observed in live recordings but not yet shipped

(Empty as of 2026-04-30 close-out. The previous entries — transition
rates / wipe sub-ops / DVE sub-ops / color generator — were all
shipped against ground-truth Software Control XML in the same
session.)

### Op IDs observed but not yet handled

These don't appear in the reference XMLs but Software Control may emit
them in macros that exercise other features (audio dynamics processing,
counter, etc). They'd be dropped by the partial-upload path with a
clear message.

- `MacroAction` (?) — observed referencing other macros (one macro
  triggers another). Needs verification.

`TransitionSource` and `AuxiliaryInput` — previously listed here as
gaps — were closed 2026-04-28. See the verified-scenario row in §
"Verified scenarios" below. The 11 DVE / DSK ops missing from the
decoder table were closed 2026-04-29 (C1 fix); see _KNOWN_OPS in
`atemwire/macrotransfer.py`.

### Symbolic input resolution

The XML uses Software Control's display names for inputs
(`"Camera1"`, `"MediaPlayer1Key"`, `"Black"`, etc.) rather than the
numeric source IDs. `atemwire.profile._resolve_input_symbol` converts
those at apply time using:

1. Direct integer parse (`"3010"` → 3010)
2. Built-in internal-source table (`"MediaPlayer1"` → 3010,
   `"Black"` → 0, `"Color1"` → 2001, etc.)
3. Live ATEM input properties (matches against `short_name` and
   `name`, so renamed inputs work — important when a profile from
   one switcher is applied to another with custom rename schemes)
4. `CameraN` / `InputN` shortcuts (`"Camera5"` → 5)

Returning None means "couldn't resolve"; the op is logged + skipped
without aborting the macro.

## Download protocol

To save macro contents, a client downloads each used slot's bytecode
over the standard ATEM file-transfer protocol. **No locking required**:
unlike the still store which goes through `PLCK` → `LKOB` first, the
macro store is read directly via FTSU. (Verified by a Wireshark
capture of Software Control's "Save As → Macros only":
the only client→ATEM commands are FTSU and FTUA.)

```
client ──FTSU──────────────────► ATEM    request
client ◄──FTDa(chunk)──────────  ATEM    one or more, zero for empty slots
client ──FTUA(chunk_tid)────────► ATEM   per-chunk ack
client ◄──FTDC─────────────────  ATEM    transfer complete
```

For empty slots: `FTSU` → `FTDC` (no `FTDa`).

### `FTSU` — Start Upload (toward ATEM)

12 bytes:

| Offset | Size | Type   | Description                        |
|--------|------|--------|------------------------------------|
| 0      | 2    | u16 BE | Transfer ID (any unique value)     |
| 2      | 2    | u16 BE | Store ID — `0xFFFF` for macros     |
| 4      | 4    | u32 BE | Slot index                          |
| 8      | 1    | u8     | Sub-store byte: `0x03` for macros, `0x00` for stills |
| 9      | 3    | u8[]   | Magic trailer `D0 9B 8C` — same value the still downloader uses; appears to be a fixed signature |

The trailer bytes 9-11 are `D0 9B 8C` for both macros and stills in
atemwire's `TransferDownloadRequestCommand`. Software Control varies
those three bytes per request in the captures we have, but the
constant atemwire values work in practice (verified by downloading all
40 used slots on a live ATEM and getting clean responses).

### `FTDa` — File Transfer Data (from ATEM)

| Offset | Size | Type   | Description                       |
|--------|------|--------|-----------------------------------|
| 0      | 2    | u16 BE | Transfer ID (matches FTSU)        |
| 2      | 2    | u16 BE | Data length (bytes in this chunk) |
| 4      | N    | bytes  | Macro bytecode (LE — see below)   |

Multiple FTDa packets may arrive for one transfer if the macro is
large. Empirically all macros captured fit in one FTDa, but the
implementation accumulates chunks in case of larger macros.

### `FTUA` — File Transfer Upload Acknowledge (toward ATEM)

| Offset | Size | Type   | Description                      |
|--------|------|--------|----------------------------------|
| 0      | 2    | u16 BE | Transfer ID                      |
| 2      | 2    | u16    | (placeholder — Software Control sends `0xE081`, atemwire sends `0x0000`; both work) |

Sent once per FTDa chunk.

### `FTDC` — File Transfer Data Complete (from ATEM)

| Offset | Size | Type   | Description                      |
|--------|------|--------|----------------------------------|
| 0      | 2    | u16 BE | Transfer ID                      |
| 2      | 2    | u16    | Status (`0x0000` = OK)           |

The transfer is complete. Empty slots produce FTDC with no FTDa.

## Upload protocol

Uploading bytecode to a macro slot uses the file-transfer protocol's
WRITE direction. **The upload mode is the only firmware quirk that
matters**: `mode=0x0001` (Write RLE — what the still store accepts) is
rejected on the macro store with `FTDE` code 2 ("not-found"). Macros
need `mode=0x0300` (Write uncompressed + Pre-erase). Verified against
a Wireshark capture of ATEM Software Control's "Restore from State"
(2026-04-29).

```
client ──FTSD(store=0xFFFF, mode=0x0300, len=N)─► ATEM    request
client ◄──FTCD(chunk_size, count)──────────────  ATEM    granted
client ──FTDa(chunk 0..N/chunk_size)────────────► ATEM   data
client ◄──FTUA(per chunk)────────────────────────  ATEM   ack
client ──FTFD(name, description, MD5)───────────► ATEM   metadata
client ◄──FTDC─────────────────────────────────  ATEM    transfer complete
```

The macro store does **not** require explicit `PLCK` / `LKOB` locking
before upload — `FTSD` is the first write-side command in the
"Restore from State" capture and atemwire's `protocol.upload(0xFFFF, ...)`
skips the lock for store=0xFFFF (per `_transfer_trigger`'s existing
`if next.store != 0xffff` guard). This is the asymmetry with the still
store, which always locks first.

**FTFD ordering.** Software Control's capture sends `FTFD` BEFORE the
data chunks; upstream's `_queue_chunks` → `_queue_flushed` sends
`FTFD` AFTER the last data chunk. Both orderings work in practice on
this firmware. atemwire's `upload_macro_bytecode` follows upstream's order
for consistency with the still-upload state machine.

### `FTSD` — Start Upload

16 bytes:

| Offset | Size | Type   | Description                                         |
|--------|------|--------|-----------------------------------------------------|
| 0      | 2    | u16 BE | Transfer ID                                          |
| 2      | 2    | u16 BE | Store ID — `0xFFFF` for macros                       |
| 4      | 2    | -      | Pad (zero)                                           |
| 6      | 2    | u16 BE | Slot index                                           |
| 8      | 4    | u32 BE | Total uncompressed length                            |
| 12     | 2    | u16 BE | Mode bits — see below                                |
| 14     | 2    | -      | Pad (zero)                                           |

Mode bit field (per OpenSwitcher / upstream `TransferUploadRequestCommand`):

| Bit | Hex   | Name                |
|-----|-------|---------------------|
| 0   | 0x01  | Write RLE           |
| 1   | 0x02  | Clear               |
| 8   | 0x100 | Write uncompressed  |
| 9   | 0x200 | Pre-erase           |

`mode=0x0300` = bits 8 + 9 = Write uncompressed + Pre-erase. Required
for the macro store; other modes return FTDE code 2.

### `FTFD` — File Transfer File Data

212 bytes:

| Offset | Size | Type   | Description                              |
|--------|------|--------|------------------------------------------|
| 0      | 2    | u16 BE | Transfer ID                               |
| 2      | 64   | bytes  | Name (UTF-8, NUL-truncated, zero-padded)  |
| 66     | 128  | bytes  | Description (UTF-8, NUL-truncated, zero-padded) |
| 194    | 16   | bytes  | MD5 hash of the **uncompressed** bytecode |
| 210    | 2    | -      | Pad (zero)                                |

The MD5 covers the bytecode bytes that will be (or have been) uploaded
via FTDa. Upstream's `TransferTask.calculate_hash` runs MD5
before `compress()` so it always reflects the uncompressed payload.

### `FTCD` — File Transfer Continue Data (from ATEM)

12 bytes:

| Offset | Size | Type   | Description                  |
|--------|------|--------|------------------------------|
| 0      | 2    | u16 BE | Transfer ID                   |
| 2      | 4    | -      | Unknown                       |
| 6      | 2    | u16 BE | Chunk size (typically 0x0574 = 1396) |
| 8      | 2    | u16 BE | Chunk count                   |
| 10     | 2    | -     | Unknown                       |

ATEM's grant of upload — chunk size and count tell the client how to
slice the payload. Both directions of the transfer protocol use 1396-byte
chunks in observed captures.

## Bytecode format

> **The bytecode is little-endian.** This is unusual for the ATEM
> protocol — every other ATEM wire format (live control commands,
> still RLE chunks, even the macro **write** commands MSRc/MSlp/MAct)
> is big-endian. The macro store is the exception. Verified by
> decoding the "Deal Cam" macro from a live download and matching
> exact-byte against its known XML form in
> `macros_only_2025-05-12_11-24-47.xml`.

A macro is a sequence of ops. Each op is:

| Offset | Size | Type   | Description                              |
|--------|------|--------|------------------------------------------|
| 0      | 2    | u16 LE | Total op length (including the 4-byte header) |
| 2      | 2    | u16 LE | Op type code                             |
| 4      | N-4  | bytes  | Parameters (length depends on op_type)   |

Most ops have a length of 8 (4-byte header + 4-byte parameter block).
A few use 12 (with an 8-byte parameter block — typically a u16 marker
plus a u32 LE fixed-point value).

Common parameter shapes:

- `u8 mE, u8 keyer, u16 LE value` — keyer-scoped ops (KeyOnAir,
  KeyFillInput, KeyCutInput, ...)
- `u8 mE, u8 _, u16 LE source` — bus / aux ops (ProgramInput, ...)
- `u8 mE, _, _, _` — single-trigger ops (Cut, Auto, FadeToBlack)
- `u8 mE, u8 keyer, u16 LE marker, u32 LE value` — fractional value ops
  (LumaKeyClip, LumaKeyGain, DownstreamKeyClip, DVE size). The u32 LE
  value is the XML 0..1 scalar in Q16.16 fixed point (`fraction × 65536`).
  To recover the XML scalar, divide by 65536 for clip/gain (per-mille
  wire 0..1000) and for DVE size alike. (Clip/gain previously used 655360
  here — a 10× error that set 100% on the ATEM for a 30% macro.)
- `u32 LE frames` — `MacroSleep` (no mE/keyer header)

## Op-code table

Every entry is verified live against an ATEM 1 M/E Constellation HD
on 2026-04-28. Codes marked **(XML-verified)** were correlated against
`macros_only_2025-05-12_11-24-47.xml`'s "Deal Cam" macro — the
strongest validation. Codes marked **(live-verified)** were verified
by recording a single-op test macro on the live ATEM and decoding the
resulting bytecode (lower confidence than XML-verified, but still
direct evidence: the bytecode appeared exactly when the
corresponding op was recorded).

| Code   | XML id                         | Param shape                                     | Source           |
|--------|--------------------------------|--------------------------------------------------|------------------|
| 0x0002 | `ProgramInput`                 | `u8 mE, _, u16 LE source`                       | XML-verified      |
| 0x0003 | `PreviewInput`                 | `u8 mE, _, u16 LE source`                       | live-verified     |
| 0x0004 | `Cut`                          | `u8 mE, _, _, _`                                | live-verified     |
| 0x0005 | `Auto`                         | `u8 mE, _, _, _`                                | live-verified     |
| 0x0007 | `MacroSleep`                   | `u32 LE frames`                                 | XML-verified      |
| 0x001F | `AuxiliaryInput`               | `u8 auxiliaryIndex, _, u16 LE source`           | XML-verified      |
| 0x0025 | `KeyCutInput`                  | `u8 mE, u8 keyer, u16 LE source`                | XML-verified      |
| 0x0026 | `KeyFillInput`                 | `u8 mE, u8 keyer, u16 LE source`                | XML-verified      |
| 0x0027 | `KeyOnAir`                     | `u8 mE, u8 keyer, u16 LE onAir`                 | XML-verified      |
| 0x0028 | `KeyType`                      | `u8 mE, u8 keyer, u8 type, u8 _` (type: 0 Luma, 1 Chroma, 2 Pattern, 3 DVE) | live-verified |
| 0x0029 | `LumaKeyClip`                  | `u8 mE, u8 keyer, u16 LE marker(0), u32 LE value` (XML scalar = value / 65536) | live-verified 2026-05-28: Software Control loads `clip="0.3"` as 30%; value = 0.3 × 65536. (Earlier 655360 was 10× off — set 100% on a 30% macro.) |
| 0x002A | `LumaKeyGain`                  | (same as LumaKeyClip)                            | live-verified     |
| 0x002B | `KeyFlyEnable`                 | `u8 mE, u8 keyer, u8 enable, u8 _`              | live-verified     |
| 0x002C | `LumaKeyInvert`                | `u8 mE, u8 keyer, u8 invert, u8 _`              | live-verified     |
| 0x002D | `LumaKeyPreMultiply`           | `u8 mE, u8 keyer, u8 preMultiply, u8 _`         | live-verified     |
| 0x002F | `KeyMaskEnable`                | `u8 mE, u8 keyer, u8 enable, u8 _`              | live-verified     |
| 0x0047 | `DVEAndFlyKeyXSize`            | `u8 mE, u8 keyer, u16 LE marker, u32 LE value` (XML scalar = value / 65536) | live-verified |
| 0x0083 | `TransitionStyle`              | `u8 mE, u8 style, u16 _` (style: 0 Mix, 1 Dip, 2 Wipe, 3 DVE, 4 Stinger) | live-verified |
| 0x0084 | `TransitionSource`             | `u8 mE, u8 _, u16 LE layer_mask` (bitfield: 0x01 BG, 0x02 Key1, 0x04 Key2, 0x08 Key3, 0x10 Key4) | live-verified |
| 0x0035 | `DVEKeyMaskEnable`             | `u8 mE, u8 keyer, u8 enable, u8 _`              | live-verified     |
| 0x0036 | `DVEKeyMaskTop`                | `u8 mE, u8 keyer, u16 LE marker, i32 LE value` (XML scalar = value / 65536; signed) | live-verified |
| 0x0037 | `DVEKeyMaskBottom`             | (same as MaskTop)                                | live-verified     |
| 0x0038 | `DVEKeyMaskLeft`               | (same as MaskTop)                                | live-verified     |
| 0x0039 | `DVEKeyMaskRight`              | (same as MaskTop)                                | live-verified     |
| 0x0048 | `DVEAndFlyKeyYSize`            | `u8 mE, u8 keyer, u16 LE marker, u32 LE value` (XML scalar = value / 65536) | live-verified |
| 0x004A | `DVEAndFlyKeyXPosition`        | `u8 mE, u8 keyer, u16 LE marker, i32 LE value` (XML scalar = value / 65536; signed) | live-verified |
| 0x004B | `DVEAndFlyKeyYPosition`        | (same as XPosition)                              | live-verified     |
| 0x0096 | `DownstreamKeyFillInput`       | `u8 dsk, _, u16 LE source`                      | live-verified     |
| 0x0097 | `DownstreamKeyCutInput`        | `u8 dsk, _, u16 LE source` (inferred parallel to 0x0096; not yet exercised live) | inferred |
| 0x0098 | `DownstreamKeyRate`            | `u8 dsk, _, u16 LE rate (frames)`              | live-verified     |
| 0x0099 | `DownstreamKeyAuto`            | `u8 dsk, _, _, _`                              | live-verified     |
| 0x009A | `DownstreamKeyOnAir`           | `u8 dsk, u8 onAir, _, _`                       | live-verified     |
| 0x009C | `DownstreamKeyClip`            | `u8 dsk, u8 ?, u16 LE marker, u32 LE value` (XML scalar = value / 65536) | divisor corrected to 65536 by parity with LumaKeyClip (2026-05-28); verify against a real DSK macro |
| 0x009D | `DownstreamKeyGain`            | (same as DownstreamKeyClip)                      | live-verified     |
| 0x009E | `DownstreamKeyMaskEnable`      | `u8 dsk, u8 enable, u16 _`                      | live-verified     |
| 0x00A4 | `DownstreamKeyPreMultiply`     | `u8 dsk, u8 preMultiply, u16 _`                 | live-verified     |
| 0x00A7 | `FadeToBlack`                  | `u8 mE, _, _, _`                               | live-verified     |
| 0x000C | `VideoMode`                    | `u8 mode_id, _, _, _` (mode_id matches `VideoModeField`; also surfaces as auto-prologue capturing the recording-start mode) | live-verified |
| 0x0202 | `FadeToBlackEnabled`           | `u8 mEBI, u8 enabled, u16 0` (mEBI byte is uninitialized memory in Software Control's emit; ATEM ignores it; round-trip as-is) | live-verified |
| 0x00DA | `MediaPlayerSourceStillIndex`  | `u8 mp, _, u16 LE index`                       | XML-verified      |
| 0x00E1 | `MediaPlayerSourceStill`       | `u8 mp, _, _, _`                               | XML-verified      |
| 0x014A | `FairlightAudioMixerInputSourceMixType`     | `u16 LE source, u16 marker(0x4346), i64 LE sourceId, u16 LE state, u16 marker(0x000c)` (state: 1=Off, 2=On, 4=AFV; sourceId is the universal stereo sentinel `0xFFFFFFFFFFFF0100` = i64 -65280) | live-verified |
| 0x014B | `FairlightAudioMixerInputSourceFaderGain`   | `u16 LE source, u16 marker(0x4346), i64 LE sourceId, i32 LE gain × 65536` (XML scalar = value / 65536, floats with 6 decimals) | live-verified |
| 0x016B | `FairlightAudioMixerMasterOutFaderGain`     | `i32 LE gain × 65536`                          | live-verified     |

The strip ops (0x014A/0x014B) carry a sourceId field — Software Control's
universal stereo sentinel is `0xFFFFFFFFFFFF0100` (= i64 -65280),
representing "all channels of this source". The 0x4346 u16 at param
offset 2 is constant in every captured recording but its meaning isn't
known; the decoder ignores it and the encoder emits the constant for
byte-identical round-trip.

### Op codes observed but not yet mapped

As of 2026-04-29 every macro op-id with an apply-side handler in
``_MACRO_OP_HANDLERS`` also has a decoder in ``_KNOWN_OPS`` — the
table is symmetric and the import-time guard
(``atemwire.profile._check_macro_op_table_symmetry``) raises if a
future change introduces an asymmetry. Op-ids unknown at import time
still show up at decode time as ``<Op id="Unknown_0xNNNN"
rawParams="hex"/>`` and round-trip verbatim, but they do not restore
on apply. (Caveat: this guarantees there are no silent drops for the
op-ids the apply table claims to handle. Op-codes never observed in
any captured macro are still unmapped — the table grows as new ATEM
features show up in real macros.)

Codes from the original slot 10 "TEST MACRO" record-session capture
that turned out to be: 0x0003 (PreviewInput), 0x0004 (Cut), 0x0005
(Auto), 0x00A7 (FadeToBlack), 0x0083 (TransitionStyle), 0x0084
(TransitionSource), 0x009A (DownstreamKeyOnAir) — all verified.

## Limitations / known gaps

1. **Fairlight macro ops not yet decoded.** Software Control's macro
   XML carries `FairlightAudioMixerInputSourceMixType`,
   `FairlightAudioMixerInputSourceFaderGain`, and
   `FairlightAudioMixerMasterOutFaderGain`. Their bytecode op-codes
   weren't exercised in the captures we have. The bytecode
   parameters likely include 64-bit `sourceId` and dB-encoded gains;
   needs targeted recording.

2. **MAct action codes 0x01, 0x03, 0x04 still unverified.** The
   captures we have only exercise 0x00 (RUN) and 0x02 (STOP_RECORD).

3. **Multiple-ME macros not tested.** The reference Constellation HD
   is a 1 M/E switcher. Multi-ME models presumably use the same op
   format with `mixEffectBlockIndex` set; the handlers honor that
   attribute but the assumption isn't verified.

4. **Chunked downloads not exercised.** All macros captured fit in
   one FTDa packet. Larger macros (15+ ops) would split, but this
   path is untested live. The implementation accumulates chunks
   anyway, so it should "just work" — but it's worth a flag.

## Implementation pointers

- Wire commands (write side): `atemwire/messages/macros.py` —
  `MacroRecordCommand` (MSRc), `MacroSleepCommand` (MSlp),
  `MacroActionCommand` (MAct). Reuse the existing
  `TransferDownloadRequestCommand` (FTSU, in
  `atemwire/messages/file_transfer.py`) for the
  read side.
- Read side: `atemwire/macrotransfer.py` —
  `download_macro_bytecode(protocol, slot)` does the FTSU/FTDa/FTUA/FTDC
  dance via direct-socket takeover (same pattern as
  `atemwire.rawtransfer.raw_download_slot`); `decode_macro_bytecode(raw, mx)`
  parses the LE bytecode using `_KNOWN_OPS`.
- Write side ops: `atemwire/operations.py` — `macro_start_recording`,
  `macro_stop_recording`, `macro_sleep`, `macro_run`, `macro_stop`.
- Facade: `atemwire/atem.py` — same names, `atem.macro_*`.
- Profile save: `atemwire/profile.py` `_build_macro_pool` walks every
  used slot, calls `download_macro_bytecode` + `decode_macro_bytecode`,
  emits `<Op>` children.
- Profile apply: `atemwire/profile.py` `_apply_macros` +
  `_MACRO_OP_HANDLERS` map XML op-ids back to live wire commands.

## Verified scenarios

The implementation was verified live against an ATEM 1 M/E
Constellation HD:

| Test | Date | Result |
|------|------|--------|
| Record `MSRc(slot=50, name="TEST_50_HELLO", description="descr")` then a Cut, then `MAct(stop)`. Read MPrp, verify name + desc roundtripped. | 2026-04-28 | ✅ |
| Apply 6-op `Deal Cam` macro from `macros_only_2025-05-12_11-24-47.xml` into slot 0. Trigger playback — verify program switches to Camera1, USK 0 turns on with fill=MediaPlayer1 and cut=MediaPlayer1Key, MP0 selects still slot 0. | 2026-04-28 | ✅ |
| Overwrite slot 0, re-apply the original profile, verify slot 0 is back to "Deal Cam". | 2026-04-28 | ✅ |
| Apply synthetic 5-op macro with `MacroSleep frames=60`. Trigger playback, sample program over 4 s. | 2026-04-28 | ✅ |
| **Download bytecode for all 40 used slots on a production switcher via FTSU/FTDa/FTUA/FTDC. No locking.** | 2026-04-28 | ✅ |
| **Decode "Deal Cam" bytecode and compare op-by-op against `macros_only_2025-05-12_11-24-47.xml`. All 6 ops match exactly, including attribute values.** | 2026-04-28 | ✅ |
| **Round-trip: live-save (Profile.from_atem) → live-apply (Profile.apply) → live-save again. 39/40 slots functionally identical; one slot differs by exactly the documented Unknown_0x001F gap.** | 2026-04-28 | ✅ (now closed; see 0x001F entry below) |
| **Apply reference XML's "Deal Cam" macro to slot 0, then read back via Profile.from_atem. All 6 ops byte-identical to input XML.** | 2026-04-28 | ✅ |
| **0x001F mapped to `AuxiliaryInput` after user-supplied XML reference. Round-trip "aux" macro at slot 80 (2 AuxiliaryInput ops): apply→save→modify→restore→save preserves both ops; playback flips AUX1+AUX6 to recorded sources. Full-pool round-trip on the captured corpus was 42/42 with 0 unknown ops.** | 2026-04-28 | ✅ |
| **C1 fix: 11 op-codes added (DVE Y-size, X/Y position, mask enable + four edges, DSK gain/mask-enable/pre-multiply). Op codes discovered live with the op-code discovery script: 0x0035, 0x0036–0x0039, 0x0048, 0x004A–0x004B, 0x009D, 0x009E, 0x00A4. Profile.from_atem of an 11-op test macro produces zero `Unknown_0x...` artifacts.** | 2026-04-29 | ✅ |

## Future work

- **Fairlight macro-op decoders** — `FairlightAudioMixerInputSourceMixType`,
  `*FaderGain`, `MasterOutFaderGain`. Bytecode op codes not yet
  observed; needs targeted recording.
- **Action code verification** — capture STOP_RUN (0x01),
  INSERT_USER_WAIT (0x03), CONTINUE (0x04), DELETE (0x05); confirm
  wire byte values.
- **Multi-ME / multi-DSK** — verify on a Production-class ATEM that
  `mixEffectBlockIndex=1` and `keyIndex=4..` work as expected.
- **Chunked download stress test** — record a >50-op macro, verify
  the multi-FTDa accumulation path.
