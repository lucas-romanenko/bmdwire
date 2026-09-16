# hyperdeckwire API reference

A small library for controlling Blackmagic **HyperDeck Studio**-class
units over the network.

## What it covers

Three channels, one per thing a deck offers over the network:

| Transport | Protocol | Port | Used for |
|---|---|---|---|
| Ethernet Protocol | TCP, line-oriented text | 9993 | Transport control, clip listing, timeline manipulation |
| FTP | Standard FTP | 21 | File upload (push new clips onto the SD/SSD) |
| Configuration API | HTTP, JSON | 80 | What HyperDeck Setup shows: name, network, the FTP / Web Media Manager / Ethernet Protocol switches, certificate, users, date and time, reboot (`HyperdeckSetup`, see [Configuration](#configuration-hyperdecksetup)) |

Playback and upload stay on `9993 + FTP` on purpose: every networked
HyperDeck has them, while the HTTP media/transport REST API that arrived in
firmware 8.x is only on the Plus / Pro / HDR / Shuttle models. The
configuration API is a different thing: it is what the HyperDeck Setup app
is a browser onto, it is on the Studio HD Mini too (checked on 9.0.2), and
it is the only place the network and access switches live, since the 9993
protocol has no commands for them.

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
    Hyperdeck,           # 9993 protocol client (context manager)
    Clip,                # dataclass — one row of disk_list / clips_get
    Response,            # dataclass — raw protocol response
    HyperdeckError,      # raised on 1xx protocol errors
    upload_clip,         # FTP upload helper
    UploadResult,        # dataclass returned by upload_clip
    HyperdeckSetup,      # configuration API client (port 80)
    HyperdeckSetupError, # the deck refused, or a setting did not read back
    SetupInfo, NetworkInterface, RemoteAdmin, User,   # its dataclasses
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

## Configuration (`HyperdeckSetup`)

Everything the HyperDeck Setup app shows for a networked deck lives on a
REST API at `http://<deck>/admin/api/v1/`; the app itself is a browser onto
`http://<deck>/admin/`, over USB as well. `HyperdeckSetup` is a client for
that API: stateless, one HTTP request per call, standard library only.
Measured on two HyperDeck Studio HD Mini units on software 9.0.2; the
network endpoints are the same shapes the ATEM and Videohub setup apps use.

```python
from hyperdeckwire import HyperdeckSetup, HyperdeckSetupError

deck = HyperdeckSetup('192.0.2.11')          # timeout=4.0 per request

info = deck.setup_basic()       # SetupInfo: product_name, device_name, hostname,
                                #            software, build, hardware, language
net = deck.network()            # NetworkInterface: address, netmask, gateway, dns
                                #   (configured) + active_* (running), dhcp, mac
deck.network_access()           # {'FTP': 'Enabled', 'HTTP': 'Enabled'}
deck.ethernet_protocol()        # 'Enabled'   — TCP 9993 on
deck.remote_admin()             # RemoteAdmin(enabled=True, usb_request_source=False)
```

Two rules the client is built around:

- **"Configure via USB and Ethernet" is read-only from the network.** While
  it is off the deck answers every GET and refuses every PUT with HTTP 401,
  including the PUT that would turn it on; it can only be enabled over USB.
  `remote_admin()` reads it so a 401 can be explained before it happens, and
  there is deliberately no setter. A 401 raises `HyperdeckSetupError` with
  `status == 401` and a message that names the switch.
- **Nothing is believed on a 200.** Every setter reads the setting back and
  raises `HyperdeckSetupError` (`status is None`, message ends in
  `CHECK THIS DECK`) if the deck holds something else.

### Network interface

```python
deck.set_network(netmask='255.255.248.0')    # widen; address, gateway, DNS kept
deck.set_network(gateway='192.0.2.1', dns=['192.0.2.53'])
deck.set_network(dns=[])                     # clear the DNS list
deck.set_network(dhcp=True)                  # manual values stay on the deck
deck.set_network(address='192.0.2.40')       # read-back goes to the NEW address
```

Fields left out keep their current values: the deck is read first and the
whole object is written back (the API takes it whole). The read-back is
retried until the interface is *live* on the new values, `NetworkInterface.settled`
(the deck reapplies for about a second, during which the running config lags),
or `readback_timeout` (20 s) passes and the call raises. `set_network` writes
what it is told; whether a change is safe is the caller's question. A wrong
address or gateway strands a deck until someone walks to it.

### Network access: FTP, Web Media Manager, the 9993 protocol

```python
deck.network_access()              # {'FTP': 'Enabled', 'HTTP': 'Enabled'}
deck.network_access_options()      # {'FTP': False, 'HTTP': True}   — offers SecureOnly?
deck.network_access_urls()         # {'FTP': 'ftp://Deck.local', 'HTTP': 'http://Deck.local'}
deck.set_network_access(ftp='Enabled')            # returns reboot_required (bool)
deck.set_network_access(http='SecureOnly')        # HTTPS only, needs the certificate

deck.ethernet_protocol()           # 'Enabled' | 'Disabled'
deck.set_ethernet_protocol('Enabled')
```

States are `'Disabled'`, `'Enabled'`, `'SecureOnly'` (module constants
`DISABLED` / `ENABLED` / `SECURE_ONLY`). `HTTP` is the Web Media Manager
switch. Protocol keywords are case-insensitive and protocols not named keep
their state. When the deck answers that a reboot is needed the read-back is
skipped (the old state is what it reads until then) and `True` is returned.

What the content-change use of this library needs: `ethernet_protocol()` Enabled
(for `Hyperdeck`, and for the ATEM's own HyperDeck control) and `FTP` Enabled
(for `upload_clip`). `HTTP` is not used by the library, but this configuration
API is served on the same port, so leave it Enabled rather than Disabled
(what Disabled does to `/admin/api` was not tested, because there is no way
back except USB).

### Name, identify, language

```python
deck.set_name('Deck 4 wide')       # also the 9993 `device info` name; changes the mDNS hostname
deck.identify()                    # flash the front panel; deck.identify(False) stops
deck.languages(); deck.set_language('en_US.UTF-8')
deck.capabilities()                # which API sections this firmware has
deck.heartbeat()
```

### Certificate, users, date and time, reboot

```python
deck.certificate_summary()               # {'hostname': …} plus domain/issuer/validity when installed
deck.create_self_signed_certificate()
deck.upload_certificate(pem_text)
deck.delete_certificate()
deck.create_signing_request(common_name=…, country=…, state_name=…, locality=…, organization=…)
deck.download_signing_request(csr_id)    # bytes of the .csr   (the CSR pair is untested on hardware)

deck.admin_required()                    # False on a fresh deck
deck.users()                             # [User(auth_user_id='1', username='Guest', …)]
deck.create_user('ops', 'secret'); deck.update_user('2', password='new'); deck.delete_user('2')

deck.date_and_time()                     # {'time': <unix s>, 'timezone_offset': <min>, 'time_friendly': …}
deck.set_date_and_time(unix_seconds, timezone_offset_minutes)
deck.ntp(); deck.set_ntp('192.0.2.5', enabled=True)
deck.timezone_offset(); deck.set_timezone_offset(-300)

deck.reboot()                            # GET hands out a one-time key the PUT must return
```

Connection-level failures (refused, timeout, no route) propagate as
`OSError` (`urllib.error.URLError` is one); anything the deck itself refused
is `HyperdeckSetupError`. `opener=` takes any object with
`open(request, timeout=)` for tests, the suite's `_FakeDeck` being one.

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

- **HyperDeck Studio HD Mini, software 9.0.2** (two units): the
  configuration API (`HyperdeckSetup`), every read endpoint plus the
  write paths exercised as same-value round trips (name, network, network
  access, Ethernet protocol, NTP, timezone, identify).

Other Studio HD-class units (Plus, Pro, HDR, Shuttle, Extreme) speak
the same 9993 protocol and the same configuration API. They additionally
expose the HTTP media/transport REST API, which hyperdeckwire doesn't use.
Should still work on those units without changes.

## Wire-protocol reference

The Ethernet Protocol's full command catalogue and response-code list
live in BMD's `HyperDeckEthernetProtocol.pdf` (Dec 2024). The
authoritative source for protocol semantics; this document covers the
**subset** this library wraps.
