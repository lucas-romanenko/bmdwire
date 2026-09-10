# atemwire

This is a fork of [pyatem](https://git.sr.ht/~martijnbraam/pyatem), Martijn
Braam's Python library for the Blackmagic Design ATEM switcher protocol. The
package was renamed from pyatem to atemwire to avoid confusion with upstream. It
branched from upstream commit `8f45831` (2026-03-14, six commits after the
0.13.0 tag) and was then developed inside a broadcast control application for
several months against live 1 M/E and Constellation switchers. This tree is
the library part of that work, extracted and cleaned up: the UDP transport
and protocol core with a series of reliability fixes, a declarative
wire-format layer with corrected and extended message coverage, a full macro
bytecode codec with macro upload, a save/restore implementation of ATEM
Software Control's "Save Switcher State" XML format, and a thread-safe
connection wrapper with a per-IP connection pool. The license is unchanged:
LGPL-3.0-only (see `LICENSE`, `LICENSE-gpl3.txt` and `NOTICE`).

```python
from atemwire import ATEM, probe

with ATEM('192.0.2.10') as atem:
    atem.set_program(5)
    atem.cut()
    print(atem.program_source, atem.video_mode)

print(probe('192.0.2.10'))   # {'video_format': ..., 'atem_model': ..., ...}
```

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

## Not included

These upstream send commands were removed because nothing in the fork
exercised them. Their receive-side fields are still parsed where upstream
had them. Restoring one means adding a `Send` subclass in the matching
`atemwire/messages/` module.

| Code | Upstream class | Area |
|---|---|---|
| `*XFC` | TransferCompleteCommand | TCP-proxy transfer |
| `AiVM` | AutoInputVideoModeCommand | Video mode |
| `CAMI` | AudioInputCommand | Legacy (pre-Fairlight) audio |
| `CAMM` | AudioMasterPropertiesCommand | Legacy audio |
| `CAMm` | AudioMonitorPropertiesCommand | Legacy audio |
| `CCmd` | CameraControlCommand | Camera control |
| `CMvI` | MultiviewInputCommand | Multiviewer |
| `CMvP` | MultiviewPropertiesCommand | Multiviewer |
| `CRMS` | RecordingSettingsSetCommand | Recording |
| `CRSS` | StreamingServiceSetCommand | Streaming |
| `CSBP` | SupersourceBoxPropertiesCommand | SuperSource |
| `CSSc` | SupersourcePropertiesCommand | SuperSource |
| `CTPr` | TransitionPreviewCommand | Transition preview |
| `CTPs` | TransitionPositionCommand | Manual T-bar position |
| `RcTM` | RecorderStatusCommand | Recording start/stop |
| `SALN` | SendAudioLevelsCommand | Legacy audio meters |
| `SRcl` | ClearStartupStateCommand | Startup state |
| `SRsv` | SaveStartupStateCommand | Startup state |
| `STAB` | StreamingAudioBitrateCommand | Streaming |
| `SToD` | SetTimeOfDayCommand | Clock |
| `StrR` | StreamingStatusSetCommand | Streaming start/stop |

Also not included: the TCP-proxy and USB transports (`AtemProtocol` raises
`NotImplementedError` for `tcp://` URLs and USB devices), the camera control
module, the converter/firmware/dissector tooling, the emulator, and the
Videohub client.

## Install

Requires Python 3.10 or newer and a C compiler for the `atemwire.mediaconvert`
extension (BT.709 conversion and RLE encoding).

```sh
pip install .
# with Pillow, needed only by Profile media-pool image export:
pip install ".[images]"
```

To run the tests:

```sh
pip install ".[test]"
python -m pytest
```

## Caveats

- `CKMs` (upstream keyer rectangular mask write, `atemwire/messages/upstream_keyer.py`) was drafted from the DSK mask command and the KeBP field layout and is not yet Wireshark-validated against a switcher. It is a write command, so verify it on your model before relying on it.
- `FEna` (fade-to-black enable) is reverse-engineered and is only sent in its ME1 form.
- The profile format is pinned to the version 2.1 XML emitted by a 1 M/E Constellation HD; other models may expose sections it does not model.
