# ultimattewire

Python library that archives and restores the configuration of a Blackmagic
**Ultimatte 12** or **Ultimatte 12 4K** keyer over its native TCP protocol,
producing and consuming the same zip archives the vendor's Smart Remote 4
writes with **Archive All** and reads with **Restore**.

[![CI](https://github.com/lucas-romanenko/ultimattewire/actions/workflows/ci.yml/badge.svg)](https://github.com/lucas-romanenko/ultimattewire/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/ultimattewire.svg)](https://pypi.org/project/ultimattewire/) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg) [![Latest tag](https://img.shields.io/github/v/tag/lucas-romanenko/ultimattewire?label=release&sort=semver)](https://github.com/lucas-romanenko/ultimattewire/tags)

Blackmagic does not document the protocol. Everything here was
reverse-engineered from packet captures of the vendor app talking to real
hardware, then validated by round-tripping archives through the vendor app in
both directions. The wire details in the code are marked as not to be changed
without re-validating against a unit; the Protocol notes below record what is
established and what is still a guess.

## Status

- **Pre-release** (`0.1.0.dev0`), extracted from a broadcast control
  application where operators archive and restore keyer configurations from a
  web page.
- Verified against an Ultimatte 12 4K (the unit the captures came from). The
  Ultimatte 12 (HD) speaks the same protocol by the vendor's own docstrings;
  the HD Mini and other models were not tested.

## Install

From PyPI:

```sh
pip install ultimattewire
```

Only pre-release versions exist so far (0.1.0.dev0). pip installs a pre-release when it is the only release there is, so no `--pre` is needed; pin the version in a requirements file (`ultimattewire==0.1.0.dev0`) so a later release cannot change your install under you. To install straight from a GitHub tag instead (no git needed on the machine):

```sh
pip install "ultimattewire @ https://github.com/lucas-romanenko/ultimattewire/archive/refs/tags/v0.1.0.dev0.tar.gz"
```

Python 3.10 or newer. No other dependencies.

## Features

- `archive_unit_to_bytes(host)`: pull every saved preset slot plus the `GPISettings` and `SavedSettings` resources into an in-memory zip in Smart Remote's exact layout, with a human-readable annotated state dump riding along.
- `restore_unit_from_bytes(host, zip_bytes)`: push such a zip back, in the order the vendor app uses, aborting at the first rejected write.
- Reads the unit's text prelude on TCP 9998 (label, firmware release, live control values, the preset FILE LIST) and does binary slot/resource reads and writes on TCP 9996.
- Failed reads are omitted from the archive and reported as warnings instead of being written as placeholder bytes that a later restore would push into live hardware.
- Short reads raise; truncated blobs are never archived.
- One-retry connects to survive cold-ARP and first-packet hiccups.
- Per-parameter display ranges for about 150 control values, used to annotate the state dump (raw `0..10000` to percent, frames, pixels).
- Pure standard library. No logging; results and warnings are returned to the caller.

## Usage

```python
from ultimattewire import archive_unit_to_bytes, restore_unit_from_bytes

# Archive one unit. The label comes from the unit's own Label field.
zip_bytes, info = archive_unit_to_bytes("192.0.2.21")
with open(f"{info['label']}.zip", "wb") as f:
    f.write(zip_bytes)
print(info["slots"], info["warnings"])

# Restore it (or a Smart Remote "Archive All" zip) onto another unit.
with open("Keyer_A.zip", "rb") as f:
    ok, report = restore_unit_from_bytes("192.0.2.22", f.read())
if not ok:
    print("stopped at", report["stopped_at"])
```

A zip produced by this library can be opened with Smart Remote 4's Restore
unchanged, and a Smart Remote "Archive All" zip can be passed straight to
`restore_unit_from_bytes`.

## Protocol notes

This is the reverse-engineered part and the most useful thing to read before
changing anything.

### How it was determined

The wire format was recovered from packet captures of Blackmagic's Ultimatte
Smart Remote 4 / Ultimatte Software Control performing **Archive All** and
**Restore** against a real Ultimatte 12 4K. The library was then validated by
round-tripping archives through the vendor app: a zip produced here restores
from Smart Remote, and a Smart Remote zip restores through this code. The
display ranges come from the Ultimatte 12 Operations Manual (February 2026
revision) and from Smart Remote 4 panel screenshots, not from the wire.

### Two TCP channels

| Port | Role | Framing |
|---|---|---|
| 9998 | Text control channel | The unit streams a text prelude on connect |
| 9996 | Binary settings channel | One request per TCP connection, big-endian length-prefixed frames |

### The 9998 prelude

On connect the unit immediately sends a text prelude and, if the client does
not continue with the interactive greeting the vendor app uses, may close the
connection partway through. The library reads until the literal
`END PRELUDE:` marker, then drains for a further 0.3 s, and returns whatever
arrived (decoded as UTF-8 with replacement). The prelude is a sequence of
sections introduced by an upper-case header ending in a colon, each holding
`key: value` lines. The parts the library uses:

- `Label: <name>`: the unit's user-assigned name. Sanitised to
  `[A-Za-z0-9._-]` for use as the archive filename root.
- `Software Release: <x.y>`: the firmware version. Not used by the library
  itself, but this is the field to read if you want it.
- `CONTROL:` and `CONTROL DEFAULT:`: the live and default control values as
  `Name: raw` lines, raw integers mostly in `0..10000`. Only used for the
  annotated text dump.
- `FILE LIST:`: one saved preset slot name per line. This is the list of
  slots the archive will read.

Section extraction is a regular expression anchored on the header and
terminated by the next all-caps header or `END PRELUDE:`; two pitfalls
already hit and fixed are recorded in `_preamble.py` (a `$` anchor that
truncated multi-line FILE LISTs, and a `\s*` that let an empty section
swallow the next one).

### The 9996 binary channel

Every request opens its own TCP connection; the unit closes it after
replying.

Read (`binary_request`):

```
send  [opcode: u16 BE][len: u32 BE][payload: len bytes]
recv  [len: u32 BE][body: len bytes]
```

Write (`upload_one`):

```
send  [opcode: u16 BE][name_len: u32 BE][name][data_len: u32 BE][data][0x00]
recv  6 bytes of 0x00  (success ACK)
```

Known opcodes:

| Opcode | Direction | Payload / name | Meaning |
|---|---|---|---|
| `0x0000` | read | preset slot name from FILE LIST | read a saved preset blob |
| `0x0003` | read | `GPISettings` or `SavedSettings` | read a named resource |
| `0x0100` | write | `.../presets/<slot>` | write a preset slot (mirror of `0x0000`) |
| `0x0103` | write | `.../GPISettings` or `.../SavedSettings` | write a resource (mirror of `0x0003`) |

Details that matter and were established by experiment:

- The trailing `0x00` on a write frame is mandatory. Without it the unit
  silently declines to ACK.
- The vendor app puts the full filesystem path of the extracted zip member
  into the name field (a macOS temporary directory under
  `Ultimatte Software Control-PYREST/...`). The unit appears to inspect only
  the last path segment, and for slot writes the presence of a `presets/`
  segment. The library sends the same path shape for parity.
- The ACK is six zero bytes, but the unit closes the socket so quickly after
  sending it that a client can see EOF after fewer than six. The library
  accepts any all-zero prefix as success; any non-zero byte is treated as a
  rejection and raised as `IOError`.
- A short read of the 4-byte length header, including an immediate clean
  EOF, and a short body read both raise `IOError`. An empty resource whose
  header says length 0 returns `b""` cleanly.
- Blob contents are opaque. The library never parses preset or resource
  bytes; it moves them verbatim.

### Archive layout

The zip must match the vendor app byte for byte in structure, or its Restore
rejects it. Members in this order, with explicit directory entries carrying
`0o40755` external attributes:

```
images/                       (empty directory entry)
GPISettings                   (raw bytes, no length prefix)
SavedSettings                 (raw bytes, no length prefix)
quickfiles/                   (empty directory entry)
presets/                      (explicit directory entry; required)
presets/<slot>                (one per FILE LIST entry)
<label>_config_readable.txt   (this library's addition; ignored by the vendor app)
```

### Restore order

1. Every `presets/<slot>` member, sorted by name, with opcode `0x0100`.
2. `GPISettings` with opcode `0x0103`, if present.
3. `SavedSettings` with opcode `0x0103`, if present. It overwrites the live
   state, so it goes last.

The first failed write aborts the run; `restore_unit_from_bytes` returns
`(False, report)` with `report["stopped_at"]` naming the member. Members
that are not presets or the two resources are ignored, which is how the
annotated text dump rides along harmlessly.

### Timeouts and retries

Connect timeout 5 s, read timeout 30 s on writes, 2 s quiet timeout while
reading the prelude. Every connect is retried once after 250 ms because a
cold unit regularly refuses or times out the very first packet and answers
the second.

### Verified against

- Ultimatte 12 4K, the unit the captures were taken from and the round-trip
  tests were run on. The docstrings also name the Ultimatte 12 (HD) as a
  target of the same protocol.
- The firmware version of the bench unit was not recorded at capture time.
  Read it from the prelude's `Software Release` line on your own unit and
  treat any difference from the behaviour above as something to re-verify.

### Unverified or guessed

- Whether the unit reads anything in the write name field beyond the last
  segment and the `presets/` anchor. The full vendor path is sent to be safe.
- Whether non-zero ACK bytes carry an error code. They are treated as a
  generic rejection.
- `images/` and `quickfiles/` are always written as empty directories. A unit
  that actually holds images or quick files would not have them archived by
  this library, and no opcode for them is known.
- Other opcodes almost certainly exist (the gaps between `0x0000` and
  `0x0003`, and between `0x0100` and `0x0103`, are suggestive). None have been
  probed.
- The interactive greeting the vendor app sends on 9998 after the prelude is
  not implemented; the library only ever reads the prelude.
- `read_preamble` is bounded per `recv` but has no overall deadline or size
  cap; a unit that never stops sending would keep it reading.
- Ultimatte 12 HD Mini and other models were not tested.
- The display-range table is an interpretation of the manual and the panel
  UI, not wire data. It affects only the readable text dump.

## Development

```sh
git clone https://github.com/lucas-romanenko/ultimattewire.git
cd ultimattewire
pip install -e ".[test]"
python -m pytest
```

The suite needs no hardware. CI runs it on Python 3.10, 3.12 and 3.14 for every push and pull request.

## Related libraries

One library per Blackmagic device family, same shape, same author, all pure standard library except atemwire's small C extension:

- [atemwire](https://github.com/lucas-romanenko/atemwire): ATEM switchers (UDP protocol, macros, profiles)
- [hyperdeckwire](https://github.com/lucas-romanenko/hyperdeckwire): HyperDeck recorders (transport control, clip upload)
- [videohubwire](https://github.com/lucas-romanenko/videohubwire): Videohub routers (routing, labels)

## License

MIT. See `LICENSE`.
