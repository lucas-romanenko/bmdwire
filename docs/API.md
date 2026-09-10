# hyperdeckwire API reference

A small library for controlling Blackmagic **HyperDeck Studio**-class
units over the network.

## What it covers

Two transports, both required for a full feature loop:

| Transport | Protocol | Port | Used for |
|---|---|---|---|
| Ethernet Protocol | TCP, line-oriented text | 9993 | Transport control, clip listing, timeline manipulation |
| FTP | Standard FTP | 21 | File upload (push new clips onto the SD/SSD) |

The HTTP REST API that newer HyperDeck Studio HD Plus / Pro / HDR / Shuttle
SKUs got in firmware 8.x is **not** used. The **Studio HD Mini** was left
out of that rollout (port 80 is closed on it), and `9993 + FTP` is the only
universally-supported combination across the lineup.

The Ethernet protocol itself is fully documented by BMD in
`HyperDeckEthernetProtocol.pdf` (December 2024 revision). hyperdeckwire
exposes a curated subset focused on transport control, timeline
management and clip upload.

## Quickstart

```python
from hyperdeckwire import Hyperdeck, upload_clip

# Control via 9993
with Hyperdeck('192.0.2.11') as hd:
    print(hd.model, hd.protocol_version)
    for clip in hd.disk_list():
        print(clip.clip_id, clip.name, clip.duration)
    hd.stop()
    hd.clips_clear()
    hd.clips_add('intro.mp4')
    hd.play(loop=True, single_clip=True)

# File upload via FTP
result = upload_clip('192.0.2.11', '/local/path/intro.mp4')
print(f'{result.throughput_mb_s:.1f} MB/s into slot {result.slot_dir}')
```

## Public API

Everything below is exported from `hyperdeckwire`:

```python
from hyperdeckwire import (
    Hyperdeck,        # 9993 protocol client (context manager)
    Clip,             # dataclass — one row of disk_list / clips_get
    Response,         # dataclass — raw protocol response
    HyperdeckError,   # raised on 1xx protocol errors
    upload_clip,      # FTP upload helper
    UploadResult,     # dataclass returned by upload_clip
)
```

## Connection lifecycle

`Hyperdeck` is a context manager. `connect()` opens TCP 9993 and drains the
unit's greeting (`500 connection info:` block), caching `model` and
`protocol_version`. `close()` sends `quit` and shuts the socket. Both are
safe to call directly if you don't want the `with` block.

```python
hd = Hyperdeck('192.0.2.11')
hd.connect()
try:
    ...
finally:
    hd.close()
```

Multiple simultaneous 9993 connections to one unit work, but BMD's docs
note "a limited number of clients may connect at a time" — beyond that
the unit responds `120 connection failed` and closes the socket.

## Read commands

Every read returns a dict (params) or a list of `Clip` (listings). Raises
`HyperdeckError` on a 1xx protocol error.

```python
hd.device_info()        # dict: model, protocol version, unique id,
                        #       slot count, software version
hd.remote_info()        # dict: enabled, override
hd.slot_info()          # dict: status, volume name, recording time,
                        #       video format, blocked, ...
hd.slot_info(slot_id=2) # same, for a specific slot
hd.transport_info()     # dict: status, speed, slot id, clip id,
                        #       single clip, display timecode, timecode,
                        #       video format, loop, timeline, ...
hd.configuration()      # dict: audio input, video input, file format,
                        #       timecode input, record prefix, ...
hd.disk_list()          # List[Clip] — clips on the active disk
hd.disk_list(slot_id=2) # List[Clip] — clips on a specific slot
hd.clips_count()        # int — number of clips on the timeline
hd.clips_get()          # List[Clip] — clips on the current timeline
```

### `Clip` dataclass

`disk_list()` and `clips_get()` return `List[Clip]`. The fields populated
depend on which call you made:

| Field | disk_list | clips_get |
|---|---|---|
| `clip_id` | yes | yes |
| `name` | yes | yes |
| `duration` | yes | yes |
| `file_format` | yes (e.g. `H.264`) | no |
| `video_format` | yes (e.g. `1080p25`) | no |
| `start` | no | yes (timeline start TC) |

Names are returned with spaces intact — the parser correctly handles real
filenames like `Intro Loop animation.mp4` because format/duration tokens
are parsed from the right and the name is what's left.

## Write commands

All require the unit's "remote control" to be enabled (defaults to `true`
on HD-class firmware; use `remote_info()` to check, `remote_enable()` to
toggle).

```python
hd.stop()                                 # stop playback or recording.
                                          # Doubles as "pause" — by default
                                          # the unit holds the current frame
                                          # (per `play option: stop mode:
                                          # lastframe`, the factory default).

hd.play()                                 # plain play from current position
hd.play(loop=True)                        # loop all clips on timeline
hd.play(loop=True, single_clip=True)      # loop the current clip only
hd.play(single_clip=True)                 # play current clip, stop at end
hd.play(clip_id=3)                        # play from clip 3
hd.play(speed=200, loop=True)             # 2x speed, looped
hd.play(speed=-100)                       # reverse at normal speed

hd.clips_clear()                          # empty the timeline (does NOT
                                          # delete files on disk)
hd.clips_add('intro.mp4')                 # append to timeline
hd.clips_add('intro.mp4', before_clip_id=3)
                                          # insert before clip 3
hd.clips_remove(2)                        # remove timeline clip 2

hd.goto_clip(5)                           # seek to start of clip 5
                                          # (does not play)

hd.remote_enable(True)                    # enable remote control
hd.remote_enable(False)                   # disable

hd.slot_select(2)                         # switch active slot
hd.ping()                                 # round-trip liveness check
```

### Configuration changes

`set_configuration(**kwargs)` updates one or more configuration
parameters in a single command. snake_case kwargs are translated to
the protocol's `{name}: {value}` form (underscores become spaces) and
stacked into one `configuration:` line so the deck applies them as a
batch. Booleans render as `true` / `false`.

```python
hd.set_configuration(file_format='H.264High')
hd.set_configuration(default_standard='1080p25')

# Stack any combination — applied together:
hd.set_configuration(file_format='QuickTimeProResHQ',
                     default_standard='2160p25',
                     video_input='SDI')

hd.set_configuration(record_cache=True, append_timestamp=False)
```

Common parameter names:

| snake_case kwarg | Protocol field | Sample values |
|---|---|---|
| `file_format` | file format | `H.264High`, `QuickTimeProResHQ`, `DNxHR_HQX`, … |
| `default_standard` | default standard | `1080p25`, `2160p50`, `720p5994`, … |
| `video_input` | video input | `SDI`, `4xSDI`, `HDMI`, `component`, `composite` |
| `audio_input` | audio input | `embedded`, `XLR`, `RCA` |
| `audio_codec` | audio codec | `PCM`, `AAC` |
| `record_prefix` | record prefix | str (UTF-8) |
| `record_cache` | record cache | bool |
| `append_timestamp` | append timestamp | bool |

BMD's docs note that changing `file_format` *may* respond with
`213 deck rebooting` (a 2xx success code) instead of `200 ok` and
close the connection on some firmware/format combos. Both are
treated as success, but the next call on the same `Hyperdeck`
instance would fail if the reboot actually fires. In testing a Studio
HD Mini did not reboot for the format changes exercised, so no
auto-reconnect logic is wired up.

### About pause

There's no explicit `pause` in the HyperDeck Ethernet Protocol — `stop`
is the pause equivalent. With the default `play option: stop mode:
lastframe`, the unit freezes on the current frame rather than going to
black. Subsequent `play()` resumes from there. The transport status
reports `stopped` rather than `paused` (no such state exists in the
protocol).

## FTP upload

`upload_clip()` pushes a local file to the active slot. The HyperDeck
filesystem presents storage slots as top-level numeric directories
(`/1/`, `/2/`, `/3/`); STOR at FTP root is rejected with `550`. The
helper auto-detects the slot directory from the FTP root listing and
CWDs into it before uploading.

```python
from hyperdeckwire import upload_clip

result = upload_clip('192.0.2.11', '/local/clip.mp4')
print(result.name)              # 'clip.mp4'
print(result.size)              # bytes
print(result.duration_seconds)  # wall-clock upload time
print(result.slot_dir)          # '1'
print(result.throughput_mb_s)   # bench unit hit 47.6 MB/s on a 50MB clip

# Explicit slot — skip auto-detect:
upload_clip('192.0.2.11', '/local/clip.mp4', slot=2)

# Progress reporting (called after each chunk with running byte count):
def on_progress(bytes_so_far):
    print(f'{bytes_so_far:,} bytes uploaded')

upload_clip('192.0.2.11', '/local/clip.mp4',
            progress_callback=on_progress)
```

Anonymous login is used by default — works on stock HD-class firmware.
For servers that reject the bare empty-user form, the helper retries
with explicit `anonymous` user automatically. Pass `login_user` and
`login_pass` for custom credentials.

After the upload completes, the new clip is immediately visible to the
9993 `disk_list()` call — no manual rescan needed. You can chain
upload → query → cue + play in one connected flow.

## Error handling

`HyperdeckError(code, text)` is raised when the unit responds with a
1xx error code. Codes follow BMD's documentation:

| Code | Meaning | When |
|---|---|---|
| 100 | syntax error | malformed command |
| 101 | unsupported parameter | param the firmware doesn't recognise |
| 102 | invalid value | param value out of range |
| 103 | unsupported | command not on this firmware/SKU |
| 111 | remote control disabled | tried a write without remote-enable |
| 112 | clip not found | seeking a non-existent clip id |
| 120 | connection failed | too many connections |
| 150 | invalid state | e.g. `play` during a record |

```python
from hyperdeckwire import HyperdeckError

with Hyperdeck(ip) as hd:
    try:
        hd.clips_add('nonexistent.mp4')
    except HyperdeckError as e:
        if e.code == 112:
            print('clip not found on disk')
        else:
            raise
```

Connection-level errors (TCP refused, timeout, peer-closed) raise the
underlying `OSError` / `ConnectionError` / `TimeoutError` straight from
the socket. Protocol-level framing failures raise `OSError` with a
diagnostic message ("malformed response head: ...").

## Asynchronous notifications

The protocol's `notify:` family enables async 5xx messages
(`502 slot info`, `508 transport info`, etc.) that arrive interleaved
with regular responses. hyperdeckwire **skips async messages by default**
inside `request()` so a caller waiting on the actual response doesn't
see them. The greeting (`500 connection info`) is treated as
synchronous since it always arrives once on connect.

For a future async-notifications consumer, the lower-level
`_read_response(skip_async=False)` returns async responses through —
see `test_async_message_returned_when_skip_async_false` in the test
suite for the pattern.

## What's intentionally NOT in the API

These exist in the protocol but aren't exposed yet:

- `playrange:` family (in/out point timeline ranges)
- `goto:` family beyond `goto_clip` (frame-offset, timecode-relative)
- `jog:` / `shuttle:` (frame-accurate scrub)
- `record:` (recording-side commands; playback only so far)
- `format:` (disk format / partition)
- `slate:` (digital slate metadata for recordings)
- `nas:` (NAS share management — units mount network storage)
- `notify:` toggle interface (no subscription API yet)
- Multi-line commands (`authenticate:`, etc. — line-oriented only today)

## Validated against

- **HyperDeck Studio HD Mini, firmware 8.1.1** (bench hardware). Full
  bench cycle verified: probe → clear slot → FTP
  upload → cue + loop. 47.6 MB/s upload on a 50MB clip into a freshly
  cleared slot.

Other Studio HD-class units (Plus, Pro, HDR, Shuttle, Extreme) speak
the same 9993 protocol. They additionally expose the HTTP REST API,
but hyperdeckwire doesn't use it. Should still work on those units
without changes.

## Wire-protocol reference

The Ethernet Protocol's full command catalogue and response-code list
live in BMD's `HyperDeckEthernetProtocol.pdf` (Dec 2024). The
authoritative source for protocol semantics; this document covers the
**subset** this library wraps.
