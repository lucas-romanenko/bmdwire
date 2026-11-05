# ultimattewire: complete library surface

An inventory of everything `ultimattewire` can do, built for mapping to
CLI commands. Every entry was read from the code; where its README or a
docstring says otherwise, the code wins and §5.3 lists the disagreement.
§1-§5 outside §1.3, §2.6 and §3.3 were first written for bmdctl from
bmdwire v1.7.0 (commit `31f9724`), where it recorded what the code does
with every kind of input by running it (bmdctl
`ultimatte/testdata/oracle/`); that code is unchanged since.

- **Source read:** `bmdwire` branch `ultimatte/text-protocol` (after
  v1.12.0), package `ultimattewire/`. Its code changed in 0.2.0.dev0
  (`5342705`, the network interface), in 1.7.0 (bmdwire#52, `39dded8`:
  `extract_model`, `parse_current_file`, the restore's docs) and on this
  branch: the documented 9998 protocol in full (`client.py`,
  `controls.py`, `smoke.py`; §1.3, §2.6, §3.3), `parse_interface` reading
  `DynamicIP` too, and `UltimatteNetworkError` under the new
  `UltimatteError`.
- **Two channels:** TCP 9998, a text channel (the prelude the unit sends on
  connect, then blocks), and TCP 9996, a binary channel (one request per
  connection: preset slots and two named resources).
- **9998 is documented, 9996 is not.** Blackmagic publishes "Ultimatte 12
  Ethernet Protocol v2.0" (September 2022) for the text channel; §1.3,
  §2.6 and §3.3 follow it, and say where an Ultimatte 12 4K was seen to
  differ. The binary channel's wire format was recovered from packet
  captures of Smart Remote 4 / Ultimatte Software Control archiving and
  restoring an Ultimatte 12 4K, and validated by round-tripping archives
  through the vendor app; the network interface from an Ultimatte 12 4K
  on protocol 2.1, software 2.1 (2026-09).
- **Verified on hardware:** archive, restore, the prelude reads and
  `read_network` on an Ultimatte 12 4K (software 2.3, 2026-10-27).
  **`Ultimatte` (§1.3, §2.6) has not met a unit yet**: it is tested
  against a fake built from the document (`tests/fake_ultimatte.py`),
  and `python -m ultimattewire.smoke` is the check to run on one. The
  HD, HD Mini and 8K were never tested.
- **Public surface** (`__init__.py`): `archive_unit_to_bytes`,
  `restore_unit_from_bytes`, `read_network`, `set_network`,
  `NetworkInterface`, `UltimatteNetworkError`; and `Ultimatte`,
  `UltimatteState`, `Update`, `Identity`, `VersionInfo`, `DeviceInfo`,
  `CurrentFile`, `GpiList`, `FrameBuffer`, `FrameBufferState`,
  `UltimatteError`, `UltimatteConnectionError`, `UltimatteTimeout`,
  `UltimatteNak`, `UltimatteMessageError`, `ControlValueError`,
  `UnsupportedError`. `network.exchange`, `build_block` and
  `parse_interface` are public in their module, and `controls` (the
  document's table, §2.6) is a public module.
- **Two styles.** The archive, restore and network functions are
  stateless: each opens its own connections and closes them. `Ultimatte`
  is one session that keeps the unit's state, opened and closed by the
  caller, with no thread or timer of its own.

| § | contents |
|---|---|
| 1 | Commands (writes): restore over 9996, network set over 9998, every documented 9998 command (`Ultimatte`) |
| 2 | Reads: the prelude and its parsers, binary reads, archive, network read, display ranges, `Ultimatte`'s state and updates |
| 3 | The channels' framing: 9996 frames, 9998 prelude and blocks, `Ultimatte`'s session |
| 4 | Connections: ports, timeouts, retry, errors |
| 5 | Gaps: protocol not wrapped, docs and code, traps |
| — | Counts |

## 1. Commands (writes)

### 1.1 `restore_unit_from_bytes(host, zip_bytes, connect_timeout=5.0, read_timeout=30.0)`

Pushes a Smart Remote archive (§2.4) back to a unit and returns `(ok,
info)`. The zip is read with `zipfile`; a file that isn't a zip raises
`BadZipFile`.

1. **Presets:** every member whose name starts `presets/` and doesn't end
   `/`, in `sorted()` order (code points), each with `upload_one` (§3.1)
   opcode `0x0100`, name `{prefix}/presets/{slot}`, where `slot` is the
   member's name after its first `/` (it may hold more slashes), and the
   member's bytes.
2. **`GPISettings`**, if a member has exactly that name: opcode `0x0103`,
   name `{prefix}/GPISettings`.
3. **`SavedSettings`**, if present: opcode `0x0103`, name
   `{prefix}/SavedSettings`. It overwrites the live state, so it goes
   last. The unit runs those settings at once, and what was changed on
   it and not saved is gone: `parse_current_file` (§2.1) says `Modified`
   when there is something to lose. Seen on a 12 4K with software 2.3
   (bmdwire#52): a restore of the unit's own archive, whose saved
   settings were its defaults, left it on "Ultimatte Defaults", from
   unsaved settings. Its Video Format setting stayed `auto detect`; with
   no input to detect, the format it ran fell from 1080p25 to its
   default, 525i59.94 NTSC (bmdwire#56).

`prefix` is `/private/var/folders/00/000000000000000000000000000000/T/Ultimatte Software Control-PYREST`,
the shape of the vendor app's temporary path (the unit appears to read
only the last segment and, for slots, the `presets/` anchor). Every other
member is ignored. The first upload that raises stops the run: `ok` is
False, and `info` is

```
{'presets': [(slot, ok, error_text_or_None), ...],
 'gpi':     (ok, error_text_or_None) or None if not sent,
 'saved':   (ok, error_text_or_None) or None if not sent,
 'stopped_at': 'preset:<slot>' | 'GPISettings' | 'SavedSettings' | None}
```

### 1.2 `set_network(host, index=0, *, address=None, netmask=None, gateway=None, dns=None, dynamic=None, readback_timeout=15.0, **kw)`

Writes the static network settings of `NETWORK INTERFACE <index>` and
returns the interface read back once it has settled.

- **Refused before the wire** (`ValueError`): `address` without `netmask`
  or the reverse (`address and netmask are one field on the wire — pass
  both or neither`), and nothing to set (`nothing to set`).
- **The block** (`build_block`, §3.2), its fields in this order, each
  only when given: `Dynamic IP: true|false` (`dynamic`), `Static
  Addresses: {address}/{netmask}` (one field, a dotted mask), `Static
  Gateway: {gateway}`, `Static DNS Servers: {' '.join(dns)}` (`dns=[]`
  clears the list, `None` leaves it alone). Sent with `exchange` (§3.2),
  which raises on NAK.
- **The read-back:** `read_network` again until settled, every 0.5 s
  (`READBACK_POLL`), for `readback_timeout` seconds (`time.time()`;
  always at least one read). An `UltimatteNetworkError` or `OSError` on
  the way is remembered and retried. Settled: always, when no address was
  set; otherwise the current address is present, not `0.0.0.0`, and the
  current address and netmask equal the static ones (an address change
  reapplies the interface, which reports `0.0.0.0/255.255.0.0` for about
  a second). Never settling raises `UltimatteNetworkError('{host}: the
  unit accepted {sorted(fields)} but did not settle within
  {readback_timeout:g}s — CHECK THIS UNIT ({last})')`, `sorted(fields)`
  a Python list of the field names, `last` the last error or `interface
  still reapplying (Current {address}/{netmask})`.
- **No policy:** it writes what it is told. Re-addressing a unit over the
  network can put it out of reach.
- `**kw` goes to every `exchange` (`connect_timeout`, `quiet_timeout`,
  `prelude_timeout`).

### 1.3 `Ultimatte` (`client.py`): every documented 9998 command

One session: `Ultimatte(host, port=9998, *, connect_timeout=5.0,
idle_timeout=3.0, function_idle_timeout=10.0, socket_factory=None)`,
`connect()` / `close()` or a `with` block (§3.3). Every command below
sends one block, waits for `ACK` (a `NAK` raises `UltimatteNak`), then
for the unit's update, applying every other block on the way, and
returns what the unit reported, not what was asked. Values are checked
before anything is sent (`ControlValueError`, `UnsupportedError`); none of
it has run against a unit yet.

| method | block sent | waits for | returns |
|---|---|---|---|
| `set_control(name, value, *, validate=True, wait=True)` | `CONTROL:` `<name>: <value>` | a CONTROL update holding the name | the value as the unit reports it, typed |
| `set_controls({name: value}, *, validate=True, wait=True)` | one `CONTROL:` with every line; one bad value sends nothing | every name in CONTROL updates | `{name: value}` |
| `offset_control(name, delta, *, validate=True, wait=True)` | `CONTROL:` `Offset <name>: <delta>` (range controls only) | the name | the new value |
| `run_function(name, *, idle_timeout=None)` | `CONTROL:` `<name>: yes` (the 20 function controls) | `<name>: yes` back ("the function is complete"), `function_idle_timeout` of silence at most | `None` |
| `refresh(header)` | `<HEADER>:` and the blank line (a status dump request) | the block | its lines |
| `load_file(name)` | `FILE:` `Load: <name>` | CURRENT FILE, or a MESSAGE (raises `UltimatteMessageError`) | `state.current_file` |
| `save_file(name)`, `delete_file(name)`, `rename_file(name, new_name)` | `FILE:` `Save:` / `Delete:` / `Rename:` + `To:` | FILE LIST, or a MESSAGE (raises) | `state.file_list` |
| `gpi_insert(gpi, filename, at=-1)` | `GPI:` `ID:`, `Insert:`, `At:` (-1 the end) | that GPI's GPI LIST, or a MESSAGE (raises) | its `GpiList` |
| `gpi_remove(gpi, index)` | `GPI:` `ID:`, `Remove:` (0 all) | as above | as above |
| `gpi_set_index(gpi, index)` | `GPI:` `ID:`, `Index:` | as above | as above |
| `set_frame_buffer(buffer, *, index=None, enable=None)` | `FRAME BUFFER:` `<buffer> Frame Buffer Index:` then `... Enable: on/off` | both keys in FRAME BUFFER updates | the `FrameBuffer` |
| `assign_image(buffer, image, enable=True)` | as above, the index of the `Frame Buffer n:` line naming the image | as above | as above |
| `set_frame_buffer_mix(layer, value)` | `FRAME BUFFER:` `BG`/`LY Frame Buffer Mix:` | the key | the mix |
| `set_transition_duration(layer, ms)` | `FRAME BUFFER:` `BG`/`LY Transition Duration:` (ms) | the key | the duration |
| `set_camera_id(n)` | `CAMERACONTROL:` `Camera Id:` (HD Mini only) | the key | `state.camera_id` |
| `send_block(header, lines=(), *, expect=None)` | anything | ACK (and the block `expect` names) | its lines, or `None` |

Checks before sending, from the document's table (`controls.py`, §2.6):
a range control takes an integer in its range (Window Position: 0 to the
running format's height for Top/Bottom, width for Left/Right; any value
when the format is unknown); On/Off takes a bool or `on`/`off`/`true`/
`false`/`yes`/`no`/`1`/`0`; a choice one of the document's (Video Format:
the unit's own VIDEO FORMATS list when it sent one); a function only
`yes`; Tally Active nothing. A control a footnote says the model lacks
(`*` the HD Mini, `**` the HD and HD Mini) or the protocol is too old for
(Output Range, 2.0) raises `UnsupportedError`; so do GPI commands on the
HD Mini and CAMERACONTROL anywhere else. A control in neither the
document nor the unit's CONTROL block raises `ControlValueError`;
`validate=False` sends any value as given (bools as `on`/`off`). Values go
out in lower case, as a 12 4K reports them. Names, file names and values
with a line break are refused. Frame buffers are `BG 1`, `LY 1`, `BG 2`,
`LY 2`, `GM`, `HM` or any the unit reported; an index is 0 to `Number Of
Frame Buffers`; mix and duration any integer from 0 (the document gives
no range); GPI IDs from 1, `At` from -1, `Remove` and `Index` from 0;
a camera id from 0.

`wait=False` returns at the ACK. A MESSAGE before the ACK is applied,
not raised. A timeout during a command closes the session (a late ACK
would be taken as the next command's).

## 2. Reads

### 2.1 The prelude (`_preamble.py`)

- **`read_preamble(host, connect_timeout=5.0, quiet_timeout=2.0)`**
  connects to 9998 (with the retry, §4) and reads 64 KiB at a time with
  `quiet_timeout` per read, until a read times out, the unit closes, or
  the bytes so far hold `END PRELUDE:`; then it reads on with 0.3 s per
  read until a read times out or the unit closes, closes the socket and
  returns everything, decoded UTF-8 with `errors='replace'`. It has no
  overall deadline or size cap.
- **`parse_section(text, header)`**: the regex
  `^{re.escape(header)}:[ \t]*\n(.*?)(?=\n[A-Z][A-Z0-9 ]*:\s*\n|\nEND PRELUDE:|\Z)`
  with `MULTILINE` and `DOTALL`, its group `strip()`-ed; `""` when the
  header isn't there. So a section runs from the line after its header to
  the next line that is an all-capitals header (ASCII capitals, digits and
  spaces, then a colon and only whitespace to a line end), or to `END
  PRELUDE:`, or to the end. The first line that is exactly the header
  (trailing spaces and tabs allowed) wins.
- **`parse_file_list(text)`**: the `FILE LIST` section's lines
  (`splitlines()`, so `\r`, `\v`, `\f`, U+001C-U+001E, U+0085, U+2028 and
  U+2029 split too), each stripped, the empty ones dropped: the preset
  slots' names.
- **`extract_model(text)`** (1.7.0): the `IDENTITY` section's `Model`
  (`_field`, below), `""` when it has none. A 12 4K on software 2.3 says
  `Ultimatte 12 4K`.
- **`parse_current_file(text)`** (1.7.0): `(name, status)`, the `CURRENT
  FILE` section's `Name` and `Status`, `""` each when absent: the file
  the live settings came from, and `Clean` while they match it or
  `Modified` once changed on the unit.
- **`_field(text, header, key)`**: `parse_section(text,
  header).splitlines()`, each line split at its first colon
  (`partition`); the first whose key is `key` once stripped gives its
  value, stripped; `""` when none does.
- **`extract_label(text)`**: the regex `^Label:\s*(.+)$` (`MULTILINE`):
  the first `Label:` line's value, stripped, every run of characters
  other than ASCII letters, digits, `.`, `_` and `-` replaced by one `_`
  (for a filename); `unknown` when there is no `Label:` line. `\s*` may
  cross a line end (§5.4).
- The prelude's other contents, as the README describes them: `Software
  Release:` (the firmware; not read by the library), `CONTROL:` and
  `CONTROL DEFAULT:` (`Name: raw` lines, read only to annotate, §2.5).

### 2.2 Binary reads (`binary_request`, §3.1)

| opcode | payload | returns |
|---|---|---|
| `0x0000` | a preset slot's name (from FILE LIST), UTF-8 | the slot's blob |
| `0x0003` | `GPISettings` or `SavedSettings` | the resource's bytes |

Blobs are opaque: moved verbatim, never parsed.

### 2.3 The network interface (`network.py`)

- **`read_network(host, index=0, **kw)`**: `exchange(host,
  build_block(index))`, the query form (an empty block sets nothing and
  echoes the section), parsed with `parse_interface`.
- **`parse_interface(text, index=0)`** fills `NetworkInterface` from
  `key: value` lines, each found by `^{key}:[ \t]*(.*)$` (`MULTILINE`),
  first match, stripped; a missing key is `""`.

| field | from | |
|---|---|---|
| `index` | the argument | |
| `name` | `Name` | |
| `mac` | `MAC Address` | |
| `priority` | `Priority` | `int()` when `isdigit()`, else `None` |
| `dynamic` | `Dynamic IP`, else `DynamicIP` (the document's spelling) | `True` for `true`, `False` for `false` (any case), else `None` |
| `address`, `netmask` | `Current Addresses` | the first of a comma- or space-separated list, split at `/` into address and dotted mask, each stripped; no `/` gives an empty mask |
| `gateway` | `Current Gateway` | |
| `dns` | `Current DNS Servers` | comma- or space-separated, as a list |
| `static_address`, `static_netmask` | `Static Addresses` | as `Current Addresses` |
| `static_gateway` | `Static Gateway` | |
| `static_dns` | `Static DNS Servers` | as `dns` |
| `raw` | the echo's text | |

The current fields are what the unit is using; the static ones, what it
is configured with. They differ on DHCP and while an interface reapplies.

### 2.4 Archive (`archive_unit_to_bytes(host, request_timeout=5.0)`)

1. `read_preamble` (connect timeout `request_timeout`, quiet 2 s). An
   empty one raises `IOError('empty preamble (unit unreachable or not
   responding)')`.
2. `label = extract_label`, `slots = parse_file_list`.
3. Each slot read with opcode `0x0000`; a failure is a warning
   (`preset {slot!r}: {e}`) and leaves the slot out. `GPISettings` and
   `SavedSettings` read with `0x0003`; a failure is a warning
   (`GPISettings: {e}`) and leaves the resource out, never a
   placeholder, which a restore would push into live hardware.
4. `annotate_preamble` of the prelude (§2.5).
5. The zip, in Smart Remote's order (its Restore rejects others), built
   with `zipfile` into memory:

| member | how |
|---|---|
| `images/` | a directory entry |
| `GPISettings` | the bytes, if read |
| `SavedSettings` | the bytes, if read |
| `quickfiles/` | a directory entry |
| `presets/` | a directory entry; Smart Remote requires it |
| `presets/{slot}` | one per slot read, in FILE LIST order |
| `{label}_config_readable.txt` | the annotated prelude, UTF-8; this library's addition, ignored by the vendor app |

Directory entries are `ZipInfo(name, date_time=now)` with
`external_attr = 0o40755 << 16`, stored, empty. Files are `writestr(name,
data)` into a `ZIP_DEFLATED` archive: deflated at zlib's default level,
`external_attr = 0o600 << 16`, the time of the write. As CPython 3.12
writes them (checked): no data descriptors, no extra fields, version made
by 2.0 on Unix (`create_system` 3; 0 under Windows), version needed 2.0,
flags 0, or `0x800` for a name that isn't ASCII; times are local, to two
seconds. Returns `(zip_bytes, info)`:

```
{'label': label, 'slots': slots (FILE LIST, duplicates kept),
 'slot_sizes': {slot: len}, 'gpi_size': len or 0, 'saved_size': len or 0,
 'warnings': [...]}
```

### 2.5 Display ranges (`profile/_ranges.py`)

- **`RANGES`**: 176 control names, each `(raw_min, raw_max, display_min,
  display_default, display_max, unit)`, from the Ultimatte 12 Operations
  Manual (February 2026) and Smart Remote 4 screenshots, not from the
  wire. 150 are raw 0-10000 shown as a percentage (0-100, 0-200,
  -100-100, or -100-300 for `Matte Density`), three as -2 to 2 pixels
  (the FG input's U, V and UV positions), and 23 as they are
  (`display_min` None): frames, pixels, lines and `Output Offset`'s
  subpixels.
- **`to_display(name, raw_value)`**: `(None, None)` for a name not in
  the table or a value `float()` refuses; `(raw, unit)` for an as-is one;
  else `dmin + (raw - rmin) / (rmax - rmin) * (dmax - dmin)`, unclamped.
- **`annotate_preamble(preamble)`**: the prelude's lines (`splitlines()`,
  each `rstrip('\r')`), with each `Name: value` line under `CONTROL:` or
  `CONTROL DEFAULT:` (a header is a line ending in `:` and not starting
  with a space) that has a range rewritten `f"{name}: {value:<...}"`
  padded to 40 characters, then ` (` the display value `int()`-ed
  (towards zero), the unit (no space before `%`) and `)`. A name starting
  `Offset ` is looked up without it. The result ends with a newline.

### 2.6 `Ultimatte`'s state (`client.py`, `controls.py`)

`connect()` reads the status dump to `END PRELUDE:` into `state`
(`UltimatteState`), and every block read afterwards keeps it current.

| field | block | form |
|---|---|---|
| `protocol_version`, `protocol` | PROTOCOL PREAMBLE `Version` | string; `(major, minor)` |
| `identity` | IDENTITY | `Identity(model, label, unique_id)`; `family` maps the model to `controls.MODEL_*` (`""` for a model the document doesn't name) |
| `network` | NETWORK | `{key: value}`, merged |
| `interfaces` | NETWORK INTERFACE n | `{n: NetworkInterface}` (`parse_interface`, §2.3), a push merged into the lines before |
| `version` | VERSION | `VersionInfo(product_id, hardware_version, software_version, software_release)` |
| `device` | DEVICE | `DeviceInfo(video_format, reference_source, inputs={line: state})`; `locked("FG")` / `input_status("REF In")` match `REFIn` and `REF In` alike |
| `video_formats` | VIDEO FORMATS | list, replaced |
| `controls`, `control_defaults` | CONTROL, CONTROL DEFAULT | `{name: value}` merged; typed by the table: range `int`, On/Off, Tally and functions `bool`, choices the string sent; anything that doesn't parse, and controls the document lacks, the string |
| `current_file` | CURRENT FILE | `CurrentFile(name, status)`: `Name:`/`Status:` lines (a 12 4K), else the first bare line (the document); `modified` |
| `file_list`, `image_list` | FILE LIST, IMAGE LIST | lists, replaced |
| `gpi_lists` | GPI LIST | `{id: GpiList(id, index, events)}`, each block replacing its GPI's events |
| `frame_buffer` | FRAME BUFFER | `FrameBufferState(count, buffers={name: FrameBuffer(name, enable, index)}, mix={BG, LY}, duration={BG, LY} ms, images={n: name}, other)`, merged; `image_number(name)` |
| `camera_id` | CAMERACONTROL `Camera Id` | int |
| `messages` | MESSAGE | list of `{key: value}` (a bare line under `""`), the last 50 |
| `other` | any block the document doesn't have | `{header: lines}` |
| `prelude_complete` | END PRELUDE | bool |
| `tally` | CONTROL `Tally Active` | bool or `None` |

`to_dict()` gives the lot as plain data (JSON-ready). Unknown lines in a
known block are ignored, unknown blocks kept raw, as the document asks.

- **`subscribe(callback, headers=None)`** calls `callback(Update)` for
  every block read from then on (only those headers, without the colon,
  if given) and returns the function that unsubscribes. `Update(header,
  changes, lines, state)`: `changes` is the block's items, typed (a dict;
  a list for the list blocks; a `GpiList`). Callbacks run in the caller's
  thread inside whichever method is reading; an exception in one is
  logged (`ultimattewire.client`) and not raised.
- **`poll(timeout=0.0)`** waits up to `timeout` of silence for a block,
  then takes what else is already waiting; returns the `Update`s.
- **`wait_for(predicate, idle_timeout=None)`** reads until
  `predicate(state)`; silence raises `UltimatteTimeout`.
- **`get_control(name)`** reads state; **`context()`** is what the checks
  know (model, protocol, running format, VIDEO FORMATS).

**`controls`**: `CONTROLS`, the document's 289 rows in its order (176
range, 79 On/Off, 1 read-only, 13 choices, 20 functions), each a
`Control(name, kind, minimum, maximum, choices, limit, min_protocol, note,
window)`; `lookup(name)` (any case and spacing); `model_family`,
`model_inputs`, `has_gpi`, `has_camera_control`; `parse_value`,
`format_value`, `format_offset`, `frame_size`, `maximum_for`,
`video_formats_for`.

**`python -m ultimattewire.smoke HOST [--port] [--idle-timeout] [--watch
SECONDS] [--write [--control NAME] [--force]] [--json FILE]`** prints
every block read, lists where the unit differs from the document, sends
a status dump request for each documented block (ACK/NAK), optionally
prints pushes, and with `--write` (refused while Tally Active is on,
unless `--force`) nudges one range control a step and puts it back,
restoring even when the first write fails. Exit 0, 1 (a write or restore
not confirmed), 2 (no session).

## 3. The channels' framing

### 3.1 TCP 9996, binary (`_protocol.py`)

One request per connection; the unit closes it after replying.

- **Read, `binary_request(host, opcode, payload, timeout=5.0)`:** send
  `[opcode: u16 BE][len: u32 BE][payload]` (a `str` payload encoded
  UTF-8), receive `[len: u32 BE][body]`. Fewer than four bytes of length
  (an immediate clean close included: a reply is owed) raise
  `IOError('short read on length header (got {n} of 4 bytes; unit closed
  before replying)')`; a length of 0 returns `b""`; a short body raises
  `IOError('short read on body (got {n} of {len} bytes)')`. `timeout` is
  the connect's and every read's.
- **Write, `upload_one(host, opcode, name, payload, connect_timeout=5.0,
  read_timeout=30.0)`:** send `[opcode: u16 BE][name_len: u32
  BE][name, UTF-8][data_len: u32 BE][data][0x00]` (the trailing zero is
  required: without it the unit doesn't ACK), receive the ACK, six zero
  bytes, reading until six arrive or the unit closes. A read timing out:
  `IOError('timeout waiting for ACK (got {n} of 6 bytes)')`; nothing
  before the close: `IOError('connection closed with no ACK bytes (frame
  rejected)')`; any non-zero byte: `IOError('non-zero ACK from unit
  (likely error): {hex}')`. Fewer than six zero bytes then a close is
  success (the unit closes that fast).

| opcode | direction | |
|---|---|---|
| `0x0000` | read | a preset slot |
| `0x0003` | read | a named resource |
| `0x0100` | write | a preset slot (`.../presets/<slot>`) |
| `0x0103` | write | a named resource (`.../GPISettings`, `.../SavedSettings`) |

### 3.2 TCP 9998, text (`_preamble.py`, `network.py`)

- On connect the unit sends the prelude (§2.1), ending `END PRELUDE:`;
  a client that doesn't go on with the vendor app's interactive greeting
  may be cut off part way.
- **Blocks:** after the prelude the unit takes a block: an upper-case
  section header ending in a colon, `key: value` lines, then a **blank
  line, which submits it**; nothing happens before the blank line. It
  answers `ACK\n\n` and the section echoed back, or a bare `NAK\n\n` (an
  unknown section, or a value it won't take). An empty block (header and
  blank line) is a query, echoing the whole section; a set's echo carries
  only the fields sent, so it proves acceptance, not what the unit holds.
- **`build_block(index=0, **fields)`**: `NETWORK INTERFACE {int(index)}:`,
  then `key: value` lines in the order given, then the blank line;
  `section_name(index)` is the header without its colon.
- **`exchange(host, block, connect_timeout=5.0, quiet_timeout=2.0,
  prelude_timeout=2.0)`:** connect (with the retry), read the prelude
  (64 KiB reads with `prelude_timeout` each, until a timeout, a close or
  `END PRELUDE:`; nothing is parsed), send the block, read the reply:
  64 KiB reads with `quiet_timeout` each until a timeout or a close, or
  as soon as the text so far starts `NAK`, or starts `ACK`, holds two
  blank lines and its last character that isn't whitespace is a colon.
  Close, then: the reply stripped of leading whitespace starting `NAK`
  raises `UltimatteNetworkError('unit refused the block (NAK):\n{block,
  stripped}')`; not starting `ACK` raises `UltimatteNetworkError('no ACK
  from {host} (got {reply[:80]!r})')`; otherwise it returns what follows
  `ACK`, leading newlines dropped.

### 3.3 TCP 9998, `Ultimatte`'s session (`client.py`)

- **Connect:** `socket_factory((host, port), timeout=connect_timeout)`,
  once more after 0.25 s on any `OSError`; then the dump, block by block,
  until `END PRELUDE:` (with or without its blank line).
- **Blocks:** split at a blank line (`\n\n` or `\r\n\r\n`; CR stripped
  from each line); blank lines between blocks skipped; UTF-8, errors
  replaced. `ACK` and `NAK` are blocks of their own.
- **No-progress waits.** Every wait (the dump, an ACK, an update, a
  function's `yes`) ends after `idle_timeout` (`function_idle_timeout`)
  seconds with nothing received, counted from the last byte, so a long
  dump that keeps coming never times out. No thread, no timer, no
  keep-alive: nothing is read or sent unless a method is called.
- **Errors:** `UltimatteConnectionError` (also a `ConnectionError`):
  refused ("nothing listening on the Ultimatte text port"), no answer to
  the connect, closed before a word ("no free connection on the unit, or
  not an Ultimatte"), closed mid dump, closed or lost later, not
  connected. `UltimatteTimeout` (also a `TimeoutError`): dump stalled,
  silence from the start, no ACK, no update; a command's timeout closes
  the session. `UltimatteNak` (`.block`), `UltimatteMessageError`
  (`.message`, `.block`). All are `UltimatteError`, as is
  `UltimatteNetworkError`.

## 4. Connections

- Ports: `CONTROL_PORT` 9998, `SETTINGS_PORT` 9996.
- Timeouts: `CONNECT_TIMEOUT` 5 s, `READ_TIMEOUT` 30 s (writes' ACK),
  the prelude's quiet 2 s (`PREAMBLE_QUIET_TIMEOUT`,
  `PRELUDE_QUIET_TIMEOUT`), a reply's quiet 2 s
  (`REPLY_QUIET_TIMEOUT`), the read-back 15 s (`READBACK_TIMEOUT`).
- **`_connect_with_retry(host, port, timeout=5.0)`**: two attempts at
  `socket.create_connection`, 0.25 s apart (`_CONNECT_RETRY_DELAY`), for
  a cold unit that refuses or times out the first packet; the second
  failure raises as it is. Every connection both channels open goes
  through it.
- **Errors:** `IOError` (`OSError`) for the binary channel's failures and
  an empty prelude; `UltimatteNetworkError` for a NAK, no ACK, or a set
  that never settles; `ValueError` for `set_network`'s refusals and a
  priority `int()` can't read; `zipfile.BadZipFile` for a restore of
  something not a zip; the socket's own errors otherwise.
- No logging: results and warnings are returned to the caller.

## 5. Gaps

### 5.1 In the protocol, not in ultimattewire

- **Loading and removing media pool images.** The document: "The Telnet
  interface does not currently provide loading or removing images in the
  Media Pool. This has to be done from either a Smart Remote 4, or using
  the Software Control application." Not implemented and not guessed: it
  waits for a capture of Software Control doing it (likely 9996).
- On 9996: every opcode but the four (the README notes the gaps between
  `0x0000` and `0x0003`, and between `0x0100` and `0x0103`); `images/`
  and `quickfiles/` (always written empty: a unit holding images or quick
  files would not have them archived).
- On 9998, nothing the document lists. Blocks a unit sends that the
  document doesn't have land in `state.other`; `send_block` sends any.

### 5.2 Implemented, but not exposed

`read_preamble`, `parse_section`, `parse_file_list`, `extract_label`,
`extract_model`, `parse_current_file`, `binary_request`, `upload_one`, `_connect_with_retry`, `to_display`,
`annotate_preamble` and `RANGES` are module functions not exported from
the package.

### 5.3 Docs and comments that say something the code doesn't do

- `_preamble.py`'s and the README's "may close the connection partway
  through" without the vendor's greeting: nothing in the protocol
  document has a greeting, and `Ultimatte` keeps its session open
  without one (unverified on hardware).
- `network.py`'s module docstring says a write's echo is "the unit telling
  you what it now holds, and callers should believe the echo"; the README
  and `set_network`'s own docstring say the echo carries only the fields
  sent and must not be treated as verification, which is what the code
  does (it reads back).
- The README says the ranges cover "about 150 control values"; the table
  has 176 (150 of them percentages).

### 5.4 Behaviour a CLI will trip over (verified in code)

1. **A CR LF prelude loses its sections:** a header must end in `\n`
   after optional spaces and tabs, so `FILE LIST:\r\n` matches nothing,
   and an archive of such a unit has no presets. `extract_label`'s
   value keeps working (the `\r` is stripped).
2. **`Label:` with nothing after it takes the next line**, since `\s*`
   crosses the line end; `Label:` with only spaces sanitizes to `""`, so
   the readable dump is named `_config_readable.txt` and the label is
   empty.
3. **A `CONTROL` value of `inf` or `nan` fails the archive:**
   `float()` takes it, `int()` of the display value raises
   (`OverflowError`, `ValueError`), and `archive_unit_to_bytes` raises
   after every slot and resource has been read.
4. **`float()` is Python's:** surrounding whitespace, underscores between
   digits (`1_000`) and other scripts' decimal digits are taken; `0x10`
   isn't. Display values aren't clamped (`-50` shows `-102%`).
5. **A reply reads to the quiet timeout** unless the echo's last line ends
   in a colon (an empty value): a read of an interface whose last field
   has a value takes the 2 s quiet timeout on top of the prelude's.
6. **A priority that is a digit but not decimal** (`²`) raises
   `ValueError` from `int()`, failing the read.
7. **Duplicate FILE LIST names** are read twice and archived once (the
   dict keeps the first name's place and the last read's bytes); `slots`
   keeps both. Duplicate zip member names in a restore are pushed twice,
   each with the last member of that name.
8. **No size cap on reads:** a binary reply's length is trusted (up to
   4 GiB) and the prelude is read until the unit stops.
9. **A restore aborts at the first failure,** having already written the
   presets before it: a unit can be left with new presets and old
   settings.

## Counts

| | |
|---|---|
| Public names | 23 in the package: 6 before (`archive_unit_to_bytes`, `restore_unit_from_bytes`, `read_network`, `set_network`, `NetworkInterface`, `UltimatteNetworkError`) and 17 for the text protocol |
| Opcodes (9996) | 4: read slot, read resource, write slot, write resource |
| Blocks (9998) | 19 read (the 16 of the dump, END PRELUDE among them; MESSAGE, `ACK`, `NAK`), 6 sent (CONTROL, FILE, GPI, FRAME BUFFER, CAMERACONTROL, `NETWORK INTERFACE n`) and the status dump request of any |
| Controls (the document's table) | 289: 176 range, 79 On/Off, 1 read only, 13 choices, 20 functions |
| `Ultimatte` commands | 18 methods that send (§1.3) |
| `NetworkInterface` fields | 14 |
| Display ranges | 176 |
| Archive members | 5 fixed (3 directories, 2 resources), one per preset, the readable dump |
