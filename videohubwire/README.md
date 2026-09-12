# videohubwire

Python client for Blackmagic **Videohub** routers speaking the Videohub
Ethernet Protocol on TCP 9990: state snapshot, crosspoint routing with
ACK/NAK handling, port labels, and live application of the router's pushed
updates.

[![CI](https://github.com/lucas-romanenko/bmdwire/actions/workflows/ci.yml/badge.svg)](https://github.com/lucas-romanenko/bmdwire/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/videohubwire.svg)](https://pypi.org/project/videohubwire/) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg) [![Latest tag](https://img.shields.io/github/v/tag/lucas-romanenko/bmdwire?filter=videohubwire-v*&label=release)](https://github.com/lucas-romanenko/bmdwire/tags)

videohubwire is small, dependency-free and synchronous by design: open, read,
act, close, in milliseconds on a LAN, which suits per-request use from a web
backend or a script.

## Status

- **Pre-release** (`0.1.0.dev0`), extracted from a broadcast control
  application where it backs a routing page operators use in production
  against Smart Videohub 40 x 40 and 80 x 80 routers.
- The canned preamble in the tests has the shape of a Smart Videohub 40 x 40
  on protocol version 2.7; other models were not exercised on the bench.

## Install

From PyPI:

```sh
pip install videohubwire
```

Only pre-release versions exist so far (0.1.0.dev0). pip installs a pre-release when it is the only release there is, so no `--pre` is needed; pin the version in a requirements file (`videohubwire==0.1.0.dev0`) so a later release cannot change your install under you. To install straight from a GitHub tag instead (git needed on the machine):

```sh
pip install "videohubwire @ git+https://github.com/lucas-romanenko/bmdwire.git@videohubwire-v0.1.0.dev0#subdirectory=videohubwire"
```

Python 3.10 or newer. No other dependencies.

## Features

- `Videohub` context manager: `connect()` parses the preamble, `close()` shuts the socket, `state()` returns a UI-ready dict.
- `route(dest, src)` sends one `VIDEO OUTPUT ROUTING` change, waits for `ACK`/`NAK`, and applies any pushed blocks that arrive in between.
- `set_input_label()` / `set_output_label()` with newline collapsing and a 64-character cap before anything hits the wire.
- `ping()` liveness check.
- Output lock states (`U` / `O` / `L`) surfaced per destination; the client never takes locks.
- Bounds checks against the router's own port counts; a `NAK` or timeout raises `VideohubError`.
- Works with and without the `END PRELUDE:` marker, so old and new firmware both connect.
- One connect retry for cold-ARP first-packet loss; malformed lines are logged and skipped, not fatal.
- Pure standard library; a `socket_factory` hook lets the whole suite run without hardware.

## Usage

```python
from videohubwire import Videohub, VideohubError

with Videohub('192.0.2.31') as vh:
    snap = vh.state()
    print(snap['device']['model_name'], snap['device']['video_inputs'], 'x',
          snap['device']['video_outputs'])
    for out in snap['outputs']:
        print(out['index'], out['label'], '<-', out['source'], out['lock'])

    try:
        vh.route(dest=3, src=12)          # 0-based, like the wire
    except VideohubError as e:
        print('refused:', e)               # locked destination, NAK, or timeout

    vh.set_output_label(3, 'Wall Monitor 1')
```

## Protocol notes

Blackmagic documents the block format, the block names and the `ACK` / `NAK`
replies in the Videohub Ethernet Protocol document that ships with the
Videohub SDK; this section covers only what that document does not settle
or what this client does on top of it.

- **Preamble end detection.** Newer firmware ends the initial state dump with an `END PRELUDE:` block; older firmware simply stops sending. The client accepts either: it returns as soon as the marker arrives, or once a `VIDEOHUB DEVICE:` block and a `VIDEO OUTPUT ROUTING:` block have both been seen and the wire has been quiet for 0.3 s. A router that sends neither the marker nor a routing block is reported as "no state preamble"; that is a known limitation for an unusual device rather than a supported case.
- **Pushed updates interleave with replies.** After the preamble the router pushes the same block shapes whenever state changes from any client. Those pushes can land between a command and its `ACK`/`NAK`, so `route()`, the label setters and `ping()` apply any non-reply block they read and keep waiting for the reply. `state()` drains pending pushes with a short non-blocking read before building the snapshot.
- **Optimistic routing apply.** On `ACK` the client records the new route immediately instead of waiting for the router's own `VIDEO OUTPUT ROUTING:` broadcast, so a `state()` call right after `route()` is already correct.
- **Lock letters.** `VIDEO OUTPUT LOCKS` reports `U` (unlocked), `O` (locked by this connection) and `L` (locked by another client). This client never sends a lock command; it reports the letters so a caller can render locked destinations read-only. Routing a destination locked elsewhere returns `NAK`, which is raised as `VideohubError`.
- **Labels with spaces and empty labels.** Indexed body lines are `<index> <value>`; only the first token is the index and the rest, spaces included, is the value. A line such as `3 ` (index and nothing else) is a cleared label; `state()` substitutes `Input N` / `Output N` (1-based) for display.
- **Label limits.** The router rejects labels containing newlines because they break the block framing, and caps labels at roughly 64 characters. The client collapses all whitespace runs to single spaces and refuses labels longer than 64 characters before sending.
- **Port counts.** `video_inputs` / `video_outputs` come from the `VIDEOHUB DEVICE:` block. If those keys are absent the snapshot sizes itself from the highest label index seen. A non-numeric count in that block raises `ValueError` rather than `VideohubError`.
- **Ignored blocks.** `CONFIGURATION:`, `SERIAL PORT ...`, `MONITORING OUTPUT ...`, `VIDEO INPUT STATUS`, and any other block the client does not model are parsed past and dropped. Only `PROTOCOL PREAMBLE`, `VIDEOHUB DEVICE`, `INPUT LABELS`, `OUTPUT LABELS`, `VIDEO OUTPUT ROUTING` and `VIDEO OUTPUT LOCKS` update state.
- **Connect retry.** The first TCP connect to a router the host has not spoken to recently is sometimes lost to ARP resolution. `connect()` retries once after 0.3 s on any `OSError`; a second failure propagates unchanged.
- **Timeouts.** Connect and read timeouts default to 3 s. A read timeout while waiting for a reply raises `VideohubError`; the router closing the connection raises it too, rather than looping.
- **What the test fixture reflects.** The canned preamble in the tests has the shape of a Smart Videohub 40 x 40 reporting protocol version 2.7 (device block, labels, locks, routing, optional marker). Other models were not exercised.

## Development

```sh
git clone https://github.com/lucas-romanenko/bmdwire.git
cd bmdwire/videohubwire
pip install -e ".[test]"
python -m pytest
```

The suite needs no hardware. CI runs it on Python 3.10, 3.12 and 3.14 for every push and pull request.

## Related libraries

One library per Blackmagic device family, same shape, same author, all pure standard library except atemwire's small C extension:

- [atemwire](../atemwire/): ATEM switchers (UDP protocol, macros, profiles)
- [hyperdeckwire](../hyperdeckwire/): HyperDeck recorders (transport control, clip upload)
- [ultimattewire](../ultimattewire/): Ultimatte keyers (archive and restore)

## License

MIT. See `LICENSE`.
