# ultimattewire

Python library for the Blackmagic **Ultimatte 12** keyers (12 8K, 12 4K,
12 HD, 12 HD Mini) over their native TCP protocols. It speaks the whole of
Blackmagic's documented **text protocol** (every status block as typed
state, live updates, every control, preset files, GPI event lists, frame
buffers, camera control), archives and restores unit configuration (the
same zip archives the vendor's Smart Remote 4 writes with **Archive All**
and reads with **Restore**), and reads and sets the unit's **network
interface** (address, netmask, gateway, DNS, static or DHCP), which is what
the vendor's Ultimatte Setup does.

[![CI](https://github.com/lucas-romanenko/bmdwire/actions/workflows/ci.yml/badge.svg)](https://github.com/lucas-romanenko/bmdwire/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/bmdwire.svg?label=pypi%20bmdwire)](https://pypi.org/project/bmdwire/) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)

The text protocol on TCP 9998 is documented by Blackmagic ("Ultimatte 12
Ethernet Protocol v2.0", September 2022); `Ultimatte` follows that document.
The binary channel on TCP 9996 (archive and restore) is not documented:
it was reverse-engineered from packet captures of the vendor app talking to
real hardware, then validated by round-tripping archives through the vendor
app in both directions. The wire details in that code are marked as not to
be changed without re-validating against a unit. [The text protocol](#the-text-protocol-tcp-9998)
and the [Protocol notes](#protocol-notes) below record what is official,
what is reverse-engineered, what is verified on hardware and what is still
unknown.

## Status

- **1.0.0**, extracted from a broadcast control
  application where operators archive and restore keyer configurations from a
  web page.
- Archive, restore, the prelude reads and `read_network` are verified
  against an Ultimatte 12 4K (the unit the captures came from). The
  Ultimatte 12 (HD) speaks the same protocol by the vendor's own docstrings;
  the HD Mini and other models were not tested.
- **`Ultimatte`, the text protocol client, is not yet verified on
  hardware.** It is tested against a fake unit built strictly from
  Blackmagic's document; `python -m ultimattewire.smoke <host>` is the
  read-only check to run on a real one (see below).

## Install

ultimattewire ships inside the `bmdwire` distribution together with the other three
libraries in this repository, at one version:

```sh
pip install bmdwire
```

Pin the version in a requirements file (`bmdwire==1.11.0`) so a later release cannot change your install under you. To install straight from a GitHub tag instead (git needed on the machine):

```sh
pip install "bmdwire @ git+https://github.com/lucas-romanenko/bmdwire.git@v1.11.0"
```

Python 3.10 or newer. No other dependencies.

## Features

- `Ultimatte(host)`: one session on the documented text protocol. The unit's whole state as typed fields (identity, versions, network, video format and input locks, every control and its default, current file, file list, GPI event lists, image list, frame buffers, camera id), kept current by the unit's pushes; `subscribe(callback)` for live updates; every documented command (any control with its documented range checked first, relative offsets, functions such as Factory Defaults and Auto Screen Sample, file load/save/delete/rename, GPI event insert/remove/index, frame buffer assign/enable/mix/transition duration, camera id on the HD Mini, status dump requests). Model and protocol-version differences from the document's footnotes are enforced before anything is sent.
- `python -m ultimattewire.smoke <host>`: read-only report of everything a unit says and where it differs from the document; `--write` does one reversible change; `--image` uploads a PNG or JPG still, assigns it and undoes both; `--sample wall|floor` runs a sample and puts the controls back; `--flush` sends FLUSH SETTINGS.
- `read_network(host)` / `set_network(host, address=…, netmask=…, gateway=…, dns=…, dynamic=…)`. The unit's network interface, the one Ultimatte Setup configures. `set_network` verifies by reading back the *settled* interface, not by trusting the unit's acknowledgement.
- `archive_unit_to_bytes(host)`: pull every saved preset slot plus the `GPISettings` and `SavedSettings` resources into an in-memory zip in Smart Remote's exact layout, with a human-readable annotated state dump riding along.
- `restore_unit_from_bytes(host, zip_bytes)`: push such a zip back, in the order the vendor app uses, aborting at the first rejected write. Its `SavedSettings` become the unit's live state at once, so settings changed on the unit and not saved are lost; the prelude's CURRENT FILE says whether there are any (`Modified`).
- Reads the unit's text prelude on TCP 9998 (label, model, firmware release, live control values, the current file and whether it has unsaved changes, the preset FILE LIST) and does binary slot/resource reads and writes on TCP 9996.
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

```python
from ultimattewire import Ultimatte

with Ultimatte("192.0.2.21") as u:               # reads the status dump
    s = u.state
    print(s.identity.model, s.device.video_format, s.tally)
    print(s.controls["Matte Density"], s.control_defaults["Matte Density"])
    print(s.current_file.name, s.file_list, s.frame_buffer.images)

    u.subscribe(lambda upd: print(upd.header, upd.changes))
    u.set_control("Backing Color", "blue")       # returns the unit's echo
    u.offset_control("Matte Density", 10)
    u.load_file("Studio B")                      # raises UltimatteMessageError on a MESSAGE
    u.upload_image("Newsroom.jpg", "/path/Newsroom.jpg",  # 9996, PNG or JPG,
                   progress=lambda sent, total: print(sent, total))  # streamed
    u.assign_image("BG 1", "Newsroom.jpg")       # MESSAGE refusals raise
    u.assign_image("Garbage Matte", "mask.png")
    u.delete_image("Newsroom.jpg")
    u.set_control("Wall Cursor Position Enable", True)
    u.run_function("Sample Wall")                # ends on yes or CONTROL DEFAULT
    u.poll(2.0)                                  # pushes from the panel or other clients
```

There is no thread: pushed updates are read (and passed to subscribers)
whenever the session reads, inside a command or in `poll()`. Every wait is
a no-progress wait, ending after `idle_timeout` seconds (3 by default) with
nothing from the unit, however long the unit keeps talking before that.

```python
from ultimattewire import read_network, set_network

iface = read_network("192.0.2.21")
print(iface.static_address, iface.static_netmask, iface.static_gateway, iface.mac)

# Widen the mask, keeping the address and gateway. Returns the settled
# interface as the unit reports it, not what was asked for.
iface = set_network("192.0.2.21", address=iface.static_address,
                    netmask="255.255.248.0", gateway=iface.static_gateway)
assert iface.netmask == "255.255.248.0"
```

## The text protocol (TCP 9998)

### What is official, what is reverse-engineered, what is unknown

| | Source | Status |
|---|---|---|
| The 9998 block protocol: status dump, pushes, ACK/NAK, status dump requests, CONTROL (set, Offset, functions), FILE, GPI, FRAME BUFFER, CAMERACONTROL, MESSAGE | Blackmagic's "Ultimatte 12 Ethernet Protocol v2.0" | Implemented in full by `Ultimatte`. Tested against a fake built from the document. **Not yet run against a unit.** |
| The 289 controls, their ranges and choices, model footnotes | the same document's Controls table | `ultimattewire.controls`; checked row by row against the document |
| What a 12 4K sends that the document doesn't say (below) | the existing code and bmdctl's hardware runs on a 12 4K, software 2.3 | Read in both forms |
| `NETWORK INTERFACE n` sets and their settling | reverse-engineered on a 12 4K (2026-09) | `set_network`, verified |
| Archive/restore on 9996 | reverse-engineered from Smart Remote 4 captures | verified (see Protocol notes) |
| **Media pool images** | not in the text protocol ("This has to be done from either a Smart Remote 4, or using the Software Control application"); captured from Software Control on a 12 4K, 2026-11-11 and -12 | Upload: the 9996 write frame with opcode `0x0101`, the PNG or JPG as is (the frame doesn't say which), sent in chunks from bytes, a path, a file or an iterable with a progress callback (`upload_image`); the size check reads a PNG's IHDR or a JPEG's SOF. Other formats weren't tried and are refused. Delete: `Image:` / `Delete: <name>` on 9998. Assign on the 12 4K: `Background 1: <name>`; the unit ACKs, then refuses with a MESSAGE when the image isn't the running format's size or no FG is locked. Also `FLUSH SETTINGS: YES` (`flush_settings`), sent before an Archive. **Not yet run from ultimattewire on a unit**; `python -m ultimattewire.smoke <ip> --image` does. Reading an image back for an archive isn't captured yet. |

### Where a unit differs from the document

From the 12 4K (software 2.3, protocol 2.3) that the archive code and
bmdctl were run against; the client reads both forms:

- `CURRENT FILE` carries `Name:` and `Status:` lines (`Clean` /
  `Modified`), not the document's single bare line.
- `NETWORK INTERFACE n` says `Dynamic IP:`; the document's example says
  `DynamicIP:`.
- Values come in lower case (`Monitor Out: fg`, `Video Format: auto
  detect`); the client sends booleans as `on`/`off` and choices in lower
  case, as the unit reports them and as the document's own Telnet examples
  send them.
- CONTROL holds at least one control the document doesn't list
  (`Active Unit`). Unknown controls are kept as strings and can be set
  (the unit's own name is accepted; an unknown name needs `validate=False`).
- DEVICE's `Video Format` is the format the unit is running; CONTROL's is
  the setting (`auto detect`).

From three Wireshark captures of Software Control on that 12 4K
(2026-11-12; Protocol.md in the library notes has the timings):

- **Sample Wall / Sample Floor never answer `yes`.** The unit ACKs, says
  nothing at all for about 4 s, then switches the cursor off itself,
  pushes the new colours and a whole `CONTROL DEFAULT:` block.
  `run_function` ends on `yes` (the document) or on CONTROL DEFAULT;
  `function_idle_timeout` (10 s) covers the silence. Whether other
  functions answer `yes` on a 12 4K isn't captured.
- **A cursor move with its cursor off** is ACKed, then refused with
  `MESSAGE: Control Locked - Enable 'Cursor' function`. CONTROL writes
  raise `UltimatteMessageError` on a MESSAGE instead of waiting for an
  echo.
- **While a cursor is on** the unit pushes the colour under it (`Wall
  Color Red/Green/Blue`, or `Floor Color ...`, one block each) about every
  40 ms; turning the cursor on is echoed about 550 ms after the ACK, after
  the first of those. Waits read through the stream, and it doesn't count
  as the unit making progress, so a missing answer still times out.
- **Writing the value a control already has is echoed**, and makes CURRENT
  FILE `Modified`.
- **Monitor Out** is reported by full name in lower case (`combined
  matte`, `internal matte`); there is no `matte` alias. Software Control
  offers only 6 of the document's 16 choices (Program, Foreground Input,
  Background Input, Combined Matte, Internal Matte, Fill Out), even with
  GM on and a mask assigned; the other 10 are unverified on a unit.
- **Assigning an image** to Background 1/2, Garbage Matte or Holdout
  Matte (`Frame Buffer:` / `Garbage Matte: <name>`) makes the unit set
  that input's `Input Offset: 10000` and `Input Level: 6878` itself; it
  doesn't turn `GM Enable` on. A JPG assigns like a PNG.
- Software Control asks for single blocks with a bare header spelled
  `Frame Buffer:`, `Image List:`, `File List:`, `Current File:`;
  `refresh()` sends those four the same way.
- Matte Density raw 2500 (its default) shows as 0 in Software Control,
  which `profile._ranges` agrees with (0..10000 → -100..300%).

### Guesses the document forces, all unverified

- A GPI LIST block is taken as the whole list for its GPI (every example in
  the document is), so it replaces that GPI's events.
- FILE LIST, IMAGE LIST and VIDEO FORMATS blocks replace the list; CONTROL,
  FRAME BUFFER, DEVICE and the network blocks merge ("containing only the
  items that have changed").
- Window Position Top/Bottom/Left/Right are "0-{Based on Video Format}":
  read as 0 to the running format's height (Top, Bottom) or width (Left,
  Right). With no running format known only 0 is enforced.
- Veil and Flare Correct Vertical Size are 0-6 on the 12 8K and 0-3 on the
  Ultimatte 12 at 1.2: checked as 0-6; the unit refuses what it can't take.
- The document's FRAME BUFFER example to disable a buffer says
  `Enable: on`; it is taken to mean `off`.
- Frame buffer mix, transition duration and camera id have no documented
  range; only negatives are refused.
- Whether a unit limits 9998 sessions is not documented. A unit that
  accepts and closes at once is reported as "no free connection on the
  unit, or not an Ultimatte".

### Checking it on a unit

ultimattewire is pure standard library, so a checkout needs no install:
run it from the repository root.

```sh
git clone https://github.com/lucas-romanenko/bmdwire.git && cd bmdwire
python -m ultimattewire.smoke 192.0.2.21                 # read only
python -m ultimattewire.smoke 192.0.2.21 --watch 30      # then change something on the unit
python -m ultimattewire.smoke 192.0.2.21 --write         # one reversible write
python -m ultimattewire.smoke 192.0.2.21 --image         # upload a grey still, assign to BG 1, undo
python -m ultimattewire.smoke 192.0.2.21 --image bg.jpg  # the same with a PNG or JPG of your own
python -m ultimattewire.smoke 192.0.2.21 --sample wall   # cursor on, Sample Wall, controls put back
python -m ultimattewire.smoke 192.0.2.21 --flush         # FLUSH SETTINGS, print the answer
```

Read only by default: it prints every block, lists where the unit differs
from the document (controls added or missing, values out of range, model
footnotes the unit doesn't follow), and sends a status dump request for
each documented block (these change nothing; the unit answers ACK or NAK).
`--write` refuses while Tally Active is on unless `--force`, nudges one
control (`--control`, default Matte Density) by one raw step, checks the
unit's update, and puts the original value back even if the first write
failed. `--sample wall|floor` writes to the keyer: it turns that cursor
on, runs the sample, prints how the unit ended it and how long it took,
and sets every control the sample changed back in one block (CONTROL
DEFAULT, which the unit rewrites, is not put back). `--json FILE` saves
the state.

## Protocol notes

The 9996 channel is the reverse-engineered part and the most useful thing
to read before changing anything there.

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

### Setting values: the 9998 block protocol

The 9998 channel is not only a prelude. After it, the unit accepts **blocks**:
an upper-case section header ending in a colon, then `key: value` lines, then
a **blank line, and the blank line is what submits the block.** Nothing
happens until it arrives, which is why single-line probes (`IDENTITY?`,
`help`, `ping`, with either line ending) draw no reply at all and look like
the unit ignoring the client. That is the one fact the whole feature rests on.

The unit answers `ACK\n\n` followed by the section echoed back, or a bare
`NAK\n\n` for an unknown section or a value it will not take.

Three properties worth knowing:

- **An empty block is a query.** Header, blank line, no fields: sets nothing
  and echoes the full section. Read and write are therefore one code path,
  and reading network state needs no prelude parsing.
- **The echo after a set carries only the fields that were sent**, not the
  section. A query echoes all eleven lines of `NETWORK INTERFACE 0`; a set of
  two fields echoes those two. So the echo proves the unit *accepted* the
  fields and says nothing about what it now holds. It must not be treated as
  verification.
- **`Static Addresses` is `<ip>/<dotted-netmask>` as one field**
  (`192.168.1.10/255.255.252.0`), not a prefix length and not two fields.
  Sending the address alone tells the unit to drop the mask, so `set_network`
  refuses an address without its mask before anything reaches the wire.

Changing an address makes the interface reapply, and for about a second the
unit answers normally while reporting `Current Addresses: 0.0.0.0/255.255.0.0`
with `Static Addresses` already holding the new value. Verifying against the
current fields in that window reads garbage; verifying against the static
fields declares success before the interface is running the value. So
`set_network` polls until the unit is live on what it was configured with,
meaning current present, not `0.0.0.0`, and equal to static. It raises rather than
reporting a success it cannot stand behind. A set that touches no address
reapplies nothing and is not made to wait.

`exchange`, `build_block` and `parse_interface` are public, so any other
section the unit exposes can be driven the same way.

There is deliberately no policy in the library: `set_network` writes what it
is told. Whether a change is safe belongs to the caller. Re-addressing a
unit over the network can put it out of reach until someone visits it.

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
  not implemented. The protocol document has no greeting, and `Ultimatte`
  keeps a session without one; that a unit cuts off a client that doesn't
  send it is from early captures and not confirmed.
- `read_preamble` is bounded per `recv` but has no overall deadline or size
  cap; a unit that never stops sending would keep it reading.
- Ultimatte 12 HD Mini and other models were not tested.
- `Ultimatte` (the text protocol client) has not met a unit; see
  [The text protocol](#the-text-protocol-tcp-9998).
- The display-range table is an interpretation of the manual and the panel
  UI, not wire data. It affects only the readable text dump.

## Development

```sh
git clone https://github.com/lucas-romanenko/bmdwire.git
cd bmdwire
pip install -e ".[test]"
python -m pytest ultimattewire/tests      # or plain `python -m pytest` for all four suites
```

The suite needs no hardware. CI runs all four suites on Python 3.10, 3.12 and 3.14 for every push and pull request.

## Related libraries

The same `pip install bmdwire` carries one library per Blackmagic device family, same shape, same author, all pure standard library except atemwire's small C extension:

- [atemwire](../atemwire/): ATEM switchers (UDP protocol, macros, profiles)
- [hyperdeckwire](../hyperdeckwire/): HyperDeck recorders (transport control, clip upload)
- [videohubwire](../videohubwire/): Videohub routers (routing, labels)

## License

MIT. See `LICENSE`.
