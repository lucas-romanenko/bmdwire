# Profile Save / Restore

This document describes the save/restore feature built on top of
`atemwire.profile` and `atemwire.macrotransfer`: what it does, how the
pipeline is wired together, what API surface it exposes, and what is
known not to work.

For the underlying XML and bytecode wire-format details, see:

- [`PROFILE_FORMAT.md`](PROFILE_FORMAT.md): the `<Profile>` XML schema as
  emitted by ATEM Software Control's *Save Switcher State* command.
- [`MACRO_FORMAT.md`](MACRO_FORMAT.md): the macro recording / playback /
  upload wire protocol.

---

## What the feature does

Save and restore a complete ATEM switcher configuration to a single file
that is **byte-identical compatible with Blackmagic ATEM Software
Control's "Save Switcher State"** command. A profile saved by this
library opens cleanly in Software Control; a profile saved by Software
Control loads cleanly through this library.

This is, to our knowledge, the first publicly-implemented support for
the `<Profile>` format outside of Blackmagic's own tools.

A profile bundles, per ATEM:

| Section | What's covered | Notes |
|---|---|---|
| Mix-effect blocks | Program, Preview, Next-Transition mask, Transition Style + per-style params (mix/dip/wipe/stinger/DVE), Fade-to-Black, USK 1-4 | Per-element granularity via `MixEffectOptions`, one block per M/E |
| Downstream Keys | DSK 1-2 sources, mask, rate, on-air state | |
| Color Generators | CG 1-2 hue/saturation/luma | |
| Auxiliaries | Aux routing | |
| Settings | FtB enabled, multiview layout, button mapping | Only `ftbEnabled` is applied on restore |
| Video Mode | Resolution + frame rate | Destructive: outputs drop ~3 s on apply |
| Inputs | Input long/short labels (rename) | |
| Fairlight Audio Mixer | Master fader, per-strip fader/gain/mix-type/EQ enable + bands, master EQ bands, per-strip compressor/limiter/expander, master compressor/limiter | Master has no expander |
| Media Players | Media player slot bindings (still or clip) | |
| Media Pool | Per-slot still images (PNG) + metadata | Images are optional and captured by a separate call |
| Macros | Up to 100 macro slots; bytecode form | First public support outside Blackmagic |
| HyperDecks | Deck IP + switcher input per slot | Saved **and** restored (CXMS, 2026-06-09) |
| Camera Control, Talkback, Multiview routing, Counters | Saved to XML, **not** restored | No setter ops available yet |

---

## How it works

### The key insight

The XML is created **client-side** by Software Control. The switcher
itself has no concept of a profile: it just emits state-dump packets.
Software Control receives those packets, serializes them to XML, and on
restore walks the XML and replays the corresponding wire commands. The
state going out and coming in is the same wire-protocol vocabulary
either way.

`atemwire.profile` follows the same model: `Profile.from_atem` reads
atemwire's mixerstate (the parsed result of those same packets) and
serializes; `Profile.apply` walks the XML and dispatches to the
per-feature operation modules under `atemwire.messages` (the same
vocabulary used for live control commands). This is what makes a
byte-identical round-trip achievable without firmware cooperation.

### Pipeline

```
       SAVE                                        RESTORE
       ----                                        -------

  Profile.from_atem(atem, SaveOptions)        Profile.apply(atem, ApplyOptions)
       |                                           |
       |  wait_state_settled(atem)                 |  _apply_video_mode -> _apply_inputs ->
       |  _build_mix_effect_blocks                 |  _apply_settings_flags -> _apply_color_generators ->
       |  _build_downstream_keys                   |  _apply_auxiliaries -> _apply_transition ->
       |  _build_color_generators                  |  _apply_upstream_keyers (per-key gate) ->
       |  _build_auxiliaries                       |  _apply_downstream_keys -> _apply_fade_to_black ->
       |  _build_settings + _build_video_mode      |  _apply_media_players -> _apply_fairlight ->
       |  _build_hyperdecks                        |  _apply_macros (bytecode upload) ->
       |  _build_fairlight                         |  _apply_hyperdecks ->
       |  _build_media_players                     |  _apply_program_preview (LAST: cuts to air)
       |  _build_media_pool_stills                 |
       |  _build_camera_control                    |  Returns ApplyResult{applied, skipped, errors}
       |  _build_macro_pool                        |
       |   \_ macrotransfer                        |  Macros via:
       |       .download_macro_bytecode            |   macrotransfer.encode_single_op per <Op>
       |       .decode_macro_bytecode              |   -> upload_macro_bytecode (FTSD mode=0x0300)
       |       (146 op codes -> <Op> elements)     |
       |  _build_macro_control + _build_counters   |
       v                                           v
  Profile.to_xml()  (Software Control format)  (media-pool image bytes, if any,
       |                                        are pushed by the caller afterwards;
  download_media_pool_images(atem)              see "Media-pool images" below)
   (per-slot download_still -> PNG)
```

`from_atem` first calls `atemwire.ready.wait_state_settled`: on a freshly
opened connection the state dump keeps arriving in bursts for a second
or two after the handshake, and a profile built before it settles would
be missing sections.

### Accepted connection objects

`Profile.from_atem` accepts an `ATEM` instance or anything exposing a
`mixerstate` dict (an `ATEMConnection`, a bare `AtemProtocol`).
`Profile.apply` resolves whatever it is given to a send-capable
connection; a caller that already holds a connected `AtemProtocol` can
pass it directly and the package adapts it internally rather than
opening a parallel `ATEMConnection`.

Bulk transfers need a worker-pumped connection: `download_media_pool_images`
requires an `ATEM` or `ATEMConnection` (it rides `download_still`), and
macro download in `from_atem` uses `download_macro` when available,
falling back to the self-pumping `macrotransfer.download_macro_bytecode`
for a bare protocol.

### Macro path (separate)

Macros are an exception. The macro store on the switcher holds binary
opcode bytecode, not raw wire commands. The save side **downloads** the
bytecode via the file-transfer protocol (`FTSU` -> `FTDa` chunks ->
`FTUA` -> `FTDC`, store id `0xFFFF`) and decodes it to XML `<Op>`
children. The restore side **uploads** newly-encoded bytecode via the
file-transfer protocol (`FTSD` mode `0x0300`, uncompressed + pre-erase,
the only mode the macro store accepts -> `FTDa` chunks -> `FTFD`).

Both paths are bidirectional and round-trip byte-identical. The 146
known op codes cover everything Software Control emits for the
operator-accessible feature set on a 1 M/E Constellation HD.

For details, see [`MACRO_FORMAT.md`](MACRO_FORMAT.md).

### Why macros are NOT recorded on apply

The original implementation used the recording path (`MSRc` ->
replay each `<Op>` as a wire command -> `MAct` stop). The switcher
records whatever lands on the wire while in record mode. That works
but it has a major side effect: **every macro op drives live state
through the switcher as it's recorded.** Restoring a profile would
flicker program/preview, toggle keyers on/off, run transitions, etc.

The bytecode upload path lands the macro directly in its slot with
zero side effects. That is what `Profile.apply` uses today; the
recording-based ops (`macro_start_recording`, `macro_stop_recording`,
`macro_sleep`, ...) remain in `atemwire.messages.macros` for callers who
explicitly need them.

Two refinements of the upload path:

- **Per-op partial upload.** Each `<Op>` is encoded individually with
  `encode_single_op`. Ops the encoder does not recognise are dropped
  from that macro's bytecode and reported in the `ApplyResult` line;
  the remaining ops still upload, so the macro lands in its slot with
  most of its functionality intact rather than leaving the slot empty.
- **Empty macros upload empty bytecode.** A `<Macro>` with no encodable
  ops (or no `<Op>` children at all) is uploaded as a zero-length
  transfer so its name and description still land. Software Control's
  own "Restore from State" does the same, and it avoids the `MSRc` /
  `MAct` cycle that makes connected Software Control clients paint the
  red record border.

Upload failures (an `FTDE`, a timeout) are recorded in
`ApplyResult.errors`; dropped ops are a partial success and stay in
`applied`.

---

## Public API surface

```python
from atemwire import Profile, ApplyOptions, SaveOptions
from atemwire.profile import download_media_pool_images

# Save side
profile = Profile.from_atem(atem)              # everything-on default
profile = Profile.from_atem(atem, SaveOptions(macros=False))
xml_str = profile.to_xml()
profile.to_file('/tmp/state.xml')

# Media-pool images (save side; a separate call)
images = download_media_pool_images(atem)
# [{'slot': 0, 'name': '...', 'png_bytes': b'...', 'from_cache': False}, ...]

# Round-trip
Profile.from_xml(xml_str).to_xml()             # diff-equivalent

# Load + apply
profile = Profile.from_file('/tmp/state.xml')
result = profile.apply(atem)                   # leaves Program/Preview alone

opts = ApplyOptions()
opts.mes[0].program = opts.mes[0].preview = True   # also cut M/E 1 to the saved buses
result = profile.apply(atem, opts)

result = profile.apply(atem, ApplyOptions.macros_only())   # <MacroPool> and nothing else

print(result.summary())   # "applied: 9, skipped: 5, errors: 0"
print(result.applied)     # ['video_mode (-> 1080p50)', 'settings.ftbEnabled', ...]
print(result.skipped)     # ['program_preview: gated off (...)', ...]
print(result.errors)      # ['video_mode: ValueError: ...']
```

`Profile.from_xml` refuses input containing a DTD / `<!ENTITY>`
declaration or a NUL byte (an entity-expansion guard; genuine Software
Control XML never contains either), raising `ValueError`. Unknown
attributes and elements are otherwise preserved verbatim.

### `SaveOptions` (everything defaults to `True`)

| Field | Gates emission of |
|---|---|
| `mes: List[MixEffectOptions]` | One `<MixEffectBlock index="N">` per M/E; see below |
| `downstream_keys`, `color_generators`, `fairlight`, `camera_control` | Switcher-global elements |
| `media_pool_metadata`, `media_players` | `<MediaPool>` (slot names + paths) and `<MediaPlayers>` |
| `media_pool_images` | Nothing in the XML: a caller-facing flag meaning "also call `download_media_pool_images`" |
| `auxiliaries`, `counters` | I/O |
| `settings`, `video_mode`, `hyperdecks`, `macros` | Others (`macros` covers `<MacroPool>` and `<MacroControl>`) |

### `MixEffectOptions` (per M/E, shared by save and apply)

Six per-element flags for one `<MixEffectBlock>`: `program`, `preview`,
`next_transition`, `transition_style`, `fade_to_black`, and `usk[i]`
(one per keyer). Each flag controls exactly the XML element it names;
there is no coalescing. A fully deselected M/E emits / applies no block.

Both option classes size `mes` to `PLATFORM_MAX_MES` (4) by default so a
no-args call means "everything on" on any model; save and apply only
ever loop the connected switcher's real M/E count, so extra entries are
inert. Indices beyond the list are treated as fully deselected.

### `ApplyOptions` (selective defaults)

| Field | Default | Notes |
|---|---|---|
| `mes[i].program`, `mes[i].preview` | `False` | Cuts-to-air gate |
| `mes[i].next_transition`, `mes[i].transition_style`, `mes[i].fade_to_black` | `True` | |
| `mes[i].usk` | `[True]*4` | Per-keyer gate |
| `restore_downstream_keys`, `restore_color_generators`, `restore_audio` | `True` | |
| `restore_video_mode` | `True` | **Destructive**: ~3 s output drop on resync |
| `restore_inputs`, `restore_macros`, `restore_aux`, `restore_media_players`, `restore_settings_flags` | `True` | |
| `restore_hyperdecks` | `True` | Deck IP + switcher input via CXMS |
| `restore_fly_keyframes` | `False` | Restoring a keyframe drives the live USK DVE through its geometry (the ATEM has no direct keyframe write), so it is visible on air |
| `restore_media_pool_images` | `True` | A signal to the caller; `apply` itself never touches image bytes |
| `restore_multiview`, `restore_camera_control`, `restore_talkback` | `False` | No setter ops available; enabling reports the gap |

`ApplyOptions.macros_only()` returns an options object with every flag
off except `restore_macros`, for callers whose intent is fixed at
"exactly the macros, never anything else"; it lists every field
explicitly so a future True-by-default addition stays off there.

### `ApplyResult`

```python
@dataclass
class ApplyResult:
    applied: List[str]     # section names that ran
    skipped: List[str]     # "section: reason" strings
    errors: List[str]      # "section: ExceptionType: message" strings

    def summary(self) -> str: ...
```

`apply()` is best-effort: a failure in one section appears in `errors`
but does not abort the rest of the walk.

Skip reporting follows two rules. Sections with no implementation
(multiview, camera control, talkback) always add a `skipped` line: the
gap reason when the caller opted in, the generic
`gated off (option default)` when not. Implemented sections that are
gated off add a `skipped` line only where one is configured (video
mode, inputs, audio, macros, program/preview); the rest are silent.

### Apply order

Order matters because some sections set up state others depend on:

1. Video mode (changes resolution; skipped if the switcher is already
   at the profile's mode, otherwise a warning is logged and apply
   sleeps ~3 s after the change so later commands are not dropped
   during the resync)
2. Inputs / labels
3. Settings flags (FtB enable)
4. Color generators
5. Auxiliaries
6. Transition style + per-style params, and the next-transition mask
   (gated independently, per M/E)
7. USK type -> per-type params (chroma needs type=Chroma set first);
   fly keyframes, when enabled, run before the live DVE / fly geometry
   so the intended live geometry is restored over the top
8. DSK
9. Fade-to-black rate
10. Media player slot assignments
11. Fairlight (master + strips + EQ + dynamics)
12. Macros (bytecode upload; after inputs because the encoder resolves
    post-rename symbolic source names such as `Camera5` at apply time)
13. HyperDeck bindings (slots saved as `0.0.0.0` are cleared, so the
    switcher ends up matching the profile exactly)
14. **Program / Preview** (last: they are the cuts-to-air gate; within
    the step, preview fires before program so a paired restore lands on
    the intended bus configuration)

Every per-M/E step loops only the `<MixEffectBlock>` elements the
connected switcher actually has, so a profile from a larger model
applies its first M/Es and ignores the rest.

---

## Media-pool images

The XML carries only metadata for the media pool: each `<Still>` has an
`index`, a `name`, and a `path` of the form `ATEM Media Pool/<name>.png`,
which is Software Control's local on-disk convention (nothing on the
switcher). The image data is handled by two separate steps.

**Save.** `download_media_pool_images(atem, progress_callback=None,
cancel_check=None, timeout_per_slot=30.0, cache_get=None, cache_put=None)`
downloads every used slot as a PNG and returns a list of
`{slot, name, png_bytes}` dicts (plus a `from_cache` flag). It rides the
connection's normal packet loop (`ATEMConnection.download_still`, a
native interleaved transfer), so control traffic and state events keep
flowing during each slot (~2 s measured per 1080p still). The frame size
comes from the live video mode (falling back to 1920x1080). Per-slot
failures are logged and skipped, so the caller gets a partial list;
`cancel_check()` returning True stops before the next slot;
`progress_callback(slot, completed, total)` fires after each slot. The
optional `cache_get(slot, hash_hex)` / `cache_put(slot, hash_hex, png)`
hooks let a caller dedupe slots that share the same MPfe hash within one
save; the library does not own a cache.

To produce something Software Control's restore accepts unmodified,
write the XML next to an `ATEM Media Pool/` folder holding
`<name>.png` for each entry (a ZIP with `<basename>.xml` at its root and
`ATEM Media Pool/<name>.png` entries extracts to exactly that layout).

**Restore.** `Profile.apply` never uploads image data.
`ApplyOptions.restore_media_pool_images` is a signal to the caller: when
it is set and the profile's `<MediaPool><Stills>` entries reference
image files, the caller supplies those bytes and pushes each one into
the slot named by the entry's `index` through its own upload path after
`apply` returns.

---

## Cloning one switcher's configuration to another

Save the source with the default `SaveOptions()` (everything on) and
call `download_media_pool_images` for the stills, then apply the profile
to the target with the default `ApplyOptions()`, which restores
everything except Program/Preview; the caller then uploads the stills
and sets Program/Preview to a known-good source. `restore_video_mode` is
on by default and **will** change the target's resolution if it differs
from the source (outputs drop for ~3 s while the ATEM resyncs), and is a
no-op when both already run the same mode. Inputs renamed on the source
carry over because macros are applied after labels and the macro
encoder resolves symbolic source names at apply time.

---

## What doesn't work yet

These appear in `ApplyResult.skipped` with the reason
`"... ops not implemented (gap)"` or are simply never applied. They are
documented limitations, not bugs:

- **HyperDeck binding**: deck IP + switcher input are saved and
  restored (CXMS, 2026-06-09). Auto-roll / frame-delay are not yet
  wire-mapped and are not written.
- **Camera Control**: saves the parameters, no apply-side setter ops.
- **Multiview routing**: saves the layout, no apply-side setter ops.
- **Talkback**: no apply-side ops.
- **Counters / display-clock**: saved, not applied.
- **Settings flags other than `ftbEnabled`** (`abDirect`, `cameraAux`,
  button mapping, ...): saved, not applied.
- **Fly keyframes**: restorable, but off by default because the restore
  is visible on air (see `restore_fly_keyframes`).
- **SuperSource**: not covered; the reference format comes from a
  1 M/E switcher that has no `<SuperSource>` element.

### Cross-model assumptions

The format is the one emitted by an ATEM 1 M/E Constellation HD
(majorVersion=2, minorVersion=1). Other ATEM models likely emit a strict
superset of the XML. The parser is permissive (unknown attributes
preserved verbatim, unknown elements ignored), so loading a save from a
bigger ATEM should not crash, but apply will silently no-op anything
outside the supported set. Per-M/E sections are driven by the connected
switcher's real M/E count, up to `PLATFORM_MAX_MES` (4).

---

## Tests

All under `atemwire/tests/`; no live ATEM required (`pytest atemwire/tests`).

| File | What it covers |
|---|---|
| `test_profile_roundtrip.py` | Format compliance, the reference-XML byte-identity round-trip, apply-defaults sanity |
| `test_profile_granular.py` | Each M/E grid flag in isolation, save-side and apply-side, with a recording connection double that captures emitted commands |
| `test_profile_apply_restore.py` | Synthetic profile XML fed to each `_apply_*` function; asserts the expected wire op fired with the value converted from Software Control's units |
| `test_apply_macros.py` | Macro apply policy: clean uploads, partial uploads (encoder gaps), all-dropped and empty-macro metadata-only landing, transfer errors |
| `test_apply_options_macros_only.py` | The `macros_only()` preset |
| `test_profile_media_pool_native.py` | `download_media_pool_images` over the native transfer |
| `test_profile_protocol_adapter.py` | Applying through a bare `AtemProtocol` |
| `test_profile_xml_hardening.py` | The DTD / NUL guards in `from_xml` |
| `test_macro_decoder.py` | Per-helper decoder tests + the module-load symmetry guard |
| `test_macro_encoder.py` | Per-op encoder tests including byte-identical round-trips on live ATEM captures |

The byte-identity pin is the reference Save Switcher State export
(Constellation HD, v2.1). It is not shipped with the library; the tests
that need it skip cleanly when it is absent.

---

## How to extend

### Adding a new section to save

1. Decide if it has a setter operation in the relevant
   `atemwire/messages/<feature>.py`. If not, add it: declare a `Send`
   (or `Recv`) class using the DSL field types (`u8`, `u16`, etc.) and
   a free-function operation wrapper next to it.
2. Add a `_build_<section>(root, mx)` function in
   `atemwire/profile/save.py`. Read from the `mx` (mixerstate) dict; emit
   sub-elements on `root`.
3. Add a flag on `SaveOptions` defaulting `True`.
4. Wire it into `Profile.from_atem` after the existing build calls.
5. Add a round-trip test in `atemwire/tests/test_profile_granular.py`.

### Adding a new section to apply

1. Make sure the operation(s) exist under `atemwire.messages`.
2. Add `_apply_<section>(conn, root, result)` in
   `atemwire/profile/apply.py`. Read XML attributes; call operations
   through `_safe` so a failure records into the `ApplyResult` instead
   of aborting.
3. Add a `restore_<section>` field on `ApplyOptions` with a default
   that matches the operational risk (`True` if the op is safe and the
   intended use case wants it).
4. Add a row to the `sections` list in `Profile.apply`, in the
   documented order.
5. Add tests in `atemwire/tests/test_profile_granular.py` and (if needed)
   round-trip tests in `atemwire/tests/test_profile_roundtrip.py`.

### Adding a macro op

The `_KNOWN_OPS` table in `atemwire/macrotransfer/__init__.py` maps each
op code to a 3-tuple `(xml_id, decoder, encoder)`. The module-load
symmetry guard (`test_known_ops_table_entries_are_3_tuples` and
`test_known_ops_symmetry_module_load_guard` in `test_macro_decoder.py`)
ensures no half-implementations.

The discovery workflow (proven across 99 ops added in one pass on
2026-04-30):

1. Have an operator record a comprehensive macro on a real ATEM with
   Software Control, exporting the XML.
2. Or record the same single-field operation in isolation into a spare
   slot on a live switcher, download the bytecode, and inspect it.
3. Or, with a comprehensive XML in hand, walk the XML's `<Op>` children
   in parallel with the bytecode bytes: every position-aligned pair
   gives `op_code -> xml_id`, and the param bytes derive the encoding.
4. Add a decoder + encoder pair in the per-feature codec file under
   `atemwire/macrotransfer/` (each is usually 4-10 lines using one of the
   existing layout helpers in `_helpers.py`).
5. Add an entry to `_KNOWN_OPS`.
6. Add a parametrized round-trip test in
   `atemwire/tests/test_macro_encoder.py`.

---

## Cross-reference

| Topic | File |
|---|---|
| `<Profile>` XML schema | [`PROFILE_FORMAT.md`](PROFILE_FORMAT.md) |
| Macro wire protocol & bytecode format | [`MACRO_FORMAT.md`](MACRO_FORMAT.md) |
| Profile package source | `atemwire/profile/` (`__init__.py`, `options.py`, `save.py`, `apply.py`) |
| Macro package source | `atemwire/macrotransfer/` |
| Tests | `atemwire/tests/test_profile_*.py`, `test_apply_macros.py`, `test_apply_options_macros_only.py`, `test_macro_*.py` |
