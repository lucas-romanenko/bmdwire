# atemwire

Python library for Blackmagic Design **ATEM** switchers: the native UDP
protocol, a thread-safe connection pool, a full macro bytecode codec, and
save/restore of ATEM Software Control's "Save Switcher State" XML.

[![CI](https://github.com/lucas-romanenko/bmdwire/actions/workflows/ci.yml/badge.svg)](https://github.com/lucas-romanenko/bmdwire/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/atemwire.svg)](https://pypi.org/project/atemwire/) [![License: LGPL-3.0](https://img.shields.io/badge/license-LGPL--3.0-blue.svg)](LICENSE) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg) [![Latest tag](https://img.shields.io/github/v/tag/lucas-romanenko/bmdwire?filter=atemwire-v*&label=release)](https://github.com/lucas-romanenko/bmdwire/tags)

atemwire is a fork of [pyatem](https://git.sr.ht/~martijnbraam/pyatem), Martijn
Braam's ATEM protocol library, renamed to avoid confusion with upstream. It
branched from upstream commit `8f45831` (2026-03-14, six commits after the
0.13.0 tag) and was then developed for months inside a broadcast control
application against live 1 M/E and Constellation switchers. This repository is
the library part of that work, extracted and cleaned up: the transport and
protocol core with a series of reliability fixes, a declarative wire-format
layer with corrected and extended message coverage, the macro codec with macro
upload, the profile save/restore, and a connection wrapper with a per-IP pool.
The license is unchanged: LGPL-3.0-only.

```python
from atemwire import ATEM, probe

with ATEM('192.0.2.10') as atem:
    atem.set_program(5)
    atem.cut()
    print(atem.program_source, atem.video_mode)

print(probe('192.0.2.10'))   # {'video_format': ..., 'atem_model': ..., ...}
```

## Status

- **Pre-release** (`0.15.0.dev0`): the API is the one a production application
  uses every day, but names may still move before 1.0.
- Runs in production driving a fleet of ATEM switchers from a Django/Channels
  app: live switching, media-pool uploads, still capture, profile save and
  restore, HyperDeck bindings.
- Developed and verified against 1 M/E and Constellation switchers; the
  profile format is pinned to the version 2.1 XML a 1 M/E Constellation HD
  emits. Other models are expected to work for switching and media, and each
  new write command is Wireshark-checked against a real switcher before it is
  trusted (see Caveats for the two that are not yet).

## Install

From PyPI:

```sh
pip install atemwire
```

Only pre-release versions exist so far (0.15.0.dev0). pip installs a pre-release when it is the only release there is, so no `--pre` is needed; pin the version in a requirements file (`atemwire==0.15.0.dev0`) so a later release cannot change your install under you. To install straight from a GitHub tag instead (git needed on the machine):

```sh
pip install "atemwire @ git+https://github.com/lucas-romanenko/bmdwire.git@atemwire-v0.15.0.dev0#subdirectory=atemwire"
```

Python 3.10 or newer. A C compiler is required: the `atemwire.mediaconvert` extension (BT.709 conversion and RLE encoding) builds during install. Add the `images` extra for Pillow, used only by the profile media-pool image export:

```sh
pip install "atemwire[images]"
```

## What you get

- **Transport that survives real networks.** Correct 15-bit sequence space,
  gap-free ACKs, go-back-N retransmission serving, in-order delivery, a
  clean session goodbye, connect timeouts, and a transport thread that cannot
  die silently. Optional per-packet tracing with `ATEMWIRE_PACKET_TRACE=1`.
- **A declarative message layer.** Every command and field is a small DSL
  class dispatched by its 4-char wire code, with corrected offsets where
  upstream was wrong and coverage for Fairlight dynamics and master EQ, USK
  mask and pattern, stinger settings, HyperDeck bindings, flying keys, macro
  play status and device identity.
- **Every upstream send command**, including the multiviewer, SuperSource,
  streaming, recording, legacy-audio, camera-control and startup-state
  commands upstream had, restored in 0.15 with upstream's byte layouts.
- **Macros, both directions.** A bytecode decoder and encoder covering 146
  op codes, macro download and upload over the file-transfer channel, and
  ATEM Software Control compatible `<MacroPool>` XML.
- **Profiles.** `Profile` saves and applies the "Save Switcher State" XML with
  per-section options, including media-pool images.
- **A pool you can share.** `ATEMConnection` runs the protocol on a worker
  thread with a command queue; `acquire_connection` gives many callers one
  session per switcher with reference counting, so a web app, an uploader and
  a thumbnail watcher can ride the same connection without freezing each other.
- **Fast media transfers.** Per-frame store locking that interleaves with
  other clients, roughly tenfold upload throughput over upstream, and a
  hardened C extension for colour conversion.

## What's different from upstream

### Transport fixes (`atemwire/transport.py`)

- Sequence arithmetic uses the ATEM's real 15-bit space (wrap at 0x8000); 16-bit math desynced ACKs on every wrap.
- ACKs advance only to the highest gap-free sequence, so a packet lost mid-burst is retransmitted instead of skipped.
- Retransmission requests are served go-back-N from a bounded buffer; upstream logged them and did nothing.
- The request sequence is read from header bytes 6-7, not from the ACK field at bytes 8-9.
- Reliable packets are delivered to consumers strictly in sequence order; late retransmits no longer corrupt file chunks.
- Transient send errors no longer kill the UDP thread, and a dying thread always wakes `loop()` with a sentinel.
- `connect()` can restart a dead transport thread and clears the previous session's receive bookkeeping.
- Datagrams from other peers, malformed frames and serialisation errors are dropped instead of raising out of the thread.
- A connect to an unreachable switcher can now time out; the pre-handshake sentinel is returned to the caller.
- `close_session()` sends the protocol goodbye so the switcher does not keep an abandoned session open.
- A `Wakeup` sentinel lets another thread break the receive loop so queued commands drain immediately.
- Optional per-packet trace logging (`ATEMWIRE_PACKET_TRACE=1`) plus rx/tx counters for diagnostics.

### Wire corrections (`atemwire/messages/`)

- All commands and fields are declared in a small DSL (`messages/_dsl.py`) and dispatched through a registry keyed by 4-char wire code.
- DVE transition rate is CTDv byte 4 / TDvP byte 2; upstream's byte 3 / byte 1 is the separate logo-wipe rate.
- CFMP master EQ enable lives at offset 1, and its EQ gain is a sign-extended i32 at offset 4.
- CFSP per-strip EQ gain and dynamics gain are i32; the i16 read clamped negative dB to +20.
- Fairlight meter levels (FMLv/FDLv) decode linearly as hundredths of a dB; the old curve read about half the true value.
- Two upstream field-code typos fixed: `RMTS` is `RTMS`, `TsPr` is `TrPr`; upstream's duplicate `AMIP` class is resolved.
- Scaled fields round to nearest instead of truncating.
- New commands: Fairlight dynamics and master EQ band (CICP, CILP, CIXP, CMCP, CMLP, CMBP), USK mask and pattern (CKMs, CKPt), stinger settings (CTSt), HyperDeck binding (CXMS), fade-to-black enable (FEna), macro sleep (MSlp), clear still (CSTL).
- New fields: the dynamics echoes (AICP, AILP, AIXP, AMBP, AMLP, MOCP), flying-key state and keyframes (KeFS, KKFP, KePt), macro play status (MRPr), HyperDeck binding (RXMS), 3G-SDI level (V3sl), device identity (WhoI).
- Every message module also carries the operation wrappers and mixerstate readers for its feature; `atemwire/_state.py` assembles them into one snapshot dict.
- The DSL covers the whole vocabulary since 0.15: `string` fields take a mask bit, a `tail` field carries a variable-length block (camera control is a plain declaration), and `tests/test_docstring_tables_match_declarations.py` checks every class's docstring offset table against its declarations, so the documented layout and the wire layout cannot drift apart again.

### Macro and profile work (`atemwire/macrotransfer/`, `atemwire/profile/`)

- Macro bytecode decoder and encoder covering 146 op codes (upstream decoded 2); unknown ops round-trip verbatim.
- Macro download and upload over the file-transfer channel, including the `0x0300` upload mode the macro store requires.
- ATEM Software Control compatible `<MacroPool>` XML in both directions; see `atemwire/docs/MACRO_FORMAT.md`.
- `Profile` saves and applies the "Save Switcher State" XML format with per-section `SaveOptions` / `ApplyOptions`; see `atemwire/docs/PROFILE_FORMAT.md` and `PROFILE_SAVE_RESTORE.md`.
- Profile XML parsing rejects DTDs and entity expansion.

### Transfer improvements (`atemwire/protocol.py`, `atemwire/connection.py`)

- `aggressive_drain=True` drains the whole send queue per trigger, roughly ten times the still upload throughput.
- Download chunks are collected in a list and joined once; the old `bytes +=` was quadratic.
- The store lock is released after every frame and the next transfer starts on the lock-state echo, so other clients interleave.
- Fatal transfer errors release the lock, pop the task and raise a `file-transfer-error` event instead of wedging the lane.
- Straggler transfer packets after an abort are ignored; `abort_transfers()` recovers from an unanswered upload request.
- Clear-still is queued through the lock discipline (`queue_clear`); some models ignore a bare CSTL without the lock.
- `ATEMConnection` runs the protocol on a worker thread with a command queue and blocking `download_still` / `download_macro`.
- `ATEMInstanceManager` / `acquire_connection` pool one session per switcher IP with reference counting and a teardown grace period.
- The C extension validates input lengths, clamps every conversion, releases buffers on all paths and raises instead of aborting on a reserved RLE word.

## Restored in 0.15

Every upstream send command is present. The 20 that the fork had dropped as
unused came back in 0.15: multiviewer routing and layout, SuperSource boxes
and art, streaming and recording settings and start/stop, legacy audio
strips, master and monitor, camera control, transition preview and T-bar
position, auto video mode, startup state, clock. They are declared in the DSL
with upstream's exact byte layouts and pinned byte-for-byte against upstream's
output in `tests/test_restored_upstream_commands.py`, but this fork has not
yet re-verified them against a switcher; see Caveats.

## Not included

Upstream's OpenSwitcher extras are not part of this package: the TCP-proxy
and USB transports (`AtemProtocol` raises `NotImplementedError` for
`tcp://` URLs and USB devices) and the `*XFC` proxy message that went with
them, the camera control *module* (the `CCmd` send command is here), the
converter / firmware / dissector tooling, the emulator, and the Videohub
client (see [videohubwire](../videohubwire/)).

## Caveats

- The 20 commands restored in 0.15 (listed under Restored in 0.15) reproduce
  upstream's wire layouts exactly and are not yet Wireshark-validated on a
  switcher by this fork. Upstream drove real hardware with them; treat them
  as upstream did and verify a write on your model before relying on it.
- `CKMs` (upstream keyer rectangular mask write, `atemwire/messages/upstream_keyer.py`) was drafted from the DSK mask command and the KeBP field layout and is not yet Wireshark-validated against a switcher. It is a write command, so verify it on your model before relying on it.
- `FEna` (fade-to-black enable) is reverse-engineered and is only sent in its ME1 form.
- The profile format is pinned to the version 2.1 XML emitted by a 1 M/E Constellation HD; other models may expose sections it does not model.

## Development

```sh
git clone https://github.com/lucas-romanenko/bmdwire.git
cd bmdwire/atemwire
pip install -e ".[test]"
python -m pytest
```

The editable install matters: it builds the C extension in place. A plain `pip install .` puts the extension in site-packages, and `python -m pytest` run from the checkout then imports the source tree without it and fails on `atemwire.mediaconvert`.

The suite needs no hardware. CI runs it on Python 3.10, 3.12 and 3.14 for every push and pull request.

## Related libraries

One library per Blackmagic device family, same shape, same author, all pure standard library except atemwire's small C extension:

- [hyperdeckwire](../hyperdeckwire/): HyperDeck recorders (transport control, clip upload)
- [ultimattewire](../ultimattewire/): Ultimatte keyers (archive and restore)
- [videohubwire](../videohubwire/): Videohub routers (routing, labels)

## License

LGPL-3.0-only, unchanged from upstream. See `LICENSE`, `LICENSE-gpl3.txt` and
`NOTICE`.
