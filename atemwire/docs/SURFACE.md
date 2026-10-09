# atemwire: complete library surface

An inventory of everything the `atemwire` library can do, built for mapping to CLI commands.
Every entry was taken from the code, not from the docs. Where a doc or comment disagrees with the
code, the code wins, and the disagreement is listed in §5.3.

- **Source read:** `bmdwire` 1.2.0 (`github.com/lucas-romanenko/bmdwire`, commit `028ac26`),
  package `atemwire/`. atemwire is byte-identical to 1.1.0, which WebATEM pins in
  `requirements.txt`, except for its README.
- **Method:** the `Send`/`Recv` classes are declarative, so their layouts, codes, mask bits and
  constructor signatures were dumped by importing the package (C extension stubbed). Hand-parsed
  classes were instantiated on zeroed payloads to list their attributes. Everything else (ranges,
  enums, unit conversions, flows) was read from the source. Paths in this file are relative to
  `atemwire/`; those starting with `atem_control/` are in WebATEM
  (`github.com/lucas-romanenko/webatem`), the library's main consumer.
- **Changes since:** the body describes 1.2.0; fixes made after it are marked in §5.4.
- **Stability:** per `atemwire/__init__.py`, the public layers are `atemwire.ATEM`, `probe`,
  `Profile`/`ApplyOptions`/`ApplyResult`, `atemwire.state`, `atemwire.messages.*`,
  `atemwire.pool`, `atemwire.profile` and `atemwire.ready`. `protocol`, `transport`, `helpers` and
  anything underscored "may change in a minor release". `atemwire.macrotransfer` is in neither list.

| § | contents |
|---|---|
| 1 | Commands (writes): 82 wire commands and their operation wrappers, by feature area |
| 2 | State (reads): 92 parsed packets, how `mixerstate` is keyed, the `build_full_state` snapshot, the read/write cross-reference |
| 3 | Transfers and special operations: stills, macros and bytecode, profiles, image conversion, video modes |
| 4 | Connection lifecycle: facade, pool, connection, bare protocol, transport, events, confirming a write |
| 5 | Gaps: known but unimplemented, implemented but unexposed, doc/code mismatches, traps |
| — | Counts |

## 1. Commands (writes)

Every command is a `Send` subclass in `atemwire/messages/<module>.py`. Building one and calling
`conn.send(cmd)` puts it on the wire; `cmd.get_command()` returns the bytes (8-byte header and
payload). Each table row is one `Send` class. The **params** column gives the class's constructor
arguments in wire units: `name:type=range`, with `?` marking an optional argument. On masked
packets an omitted optional argument leaves that field unchanged on the switcher. The **notes**
column names the operation function(s) in the same module that wrap the command: `op(conn, …)`,
usually in display units. A CLI should call the operations, not the classes, because the
conversions live there.

Conventions used in every table:

- `me` is the M/E index, 0-based (`index` on the wire). `keyer` is the USK index, 0-based. `dsk` is
  the DSK index, 0-based.
- `source` is a switcher source ID (u16). The library has no public source table. The live list comes
  from `InPr` (`all_input_sources(mx)`); see §2.3. The only name table is the private
  `macrotransfer/_helpers.py:_INTERNAL_SOURCE_NAMES` (0 Black, 1000 Bars, 2001/2002 Color1/2,
  3010/3011 MP1/MP1Key, 3020/3021 MP2/MP2Key, 1301 XLRMic, 1401 ExternalTRS, 4000/10010 ProgramOut,
  4010/10011 PreviewOut, 7001/7002 CleanFeed1/2, 5010/5011 ME1Program/Preview, 8001–8006
  Auxiliary1–6, 9000 MultiViewer1; 1–20 → Camera1–20).
- `rate_str` is `'seconds:frames'` (e.g. `'1:12'`) or a bare frame count. Ops resolve it with
  `helpers.parse_rate(rate_str, state.display_fps(mx))`. Display fps is the field rate below
  48 fps, and half the field rate at 48 fps and above (50 → 25, 59.94 → 30). A malformed string
  silently becomes `fps` frames (`helpers.py:46-51`).
- "restored upstream" marks a class `messages/*.py` says came back from upstream pyatem in 0.15 and
  "has NOT been re-verified against a switcher in this fork". Its bytes are pinned by
  `tests/test_restored_upstream_commands.py`; whether a switcher honours them is unverified.
- "not re-exported" means the class is reachable only as `atemwire.messages.<module>.<Class>`, not
  as `atemwire.messages.<Class>`. Operations are never re-exported. They always live at
  `atemwire.messages.<module>.<op>`.

### 1.1 mixeffect — `messages/switching.py`

| name | opcode | params | notes |
|---|---|---|---|
| `ProgramInputCommand` | `CPgI` | `index:u8=me`, `source:u16` | op `set_program(conn, source, me=0)` |
| `PreviewInputCommand` | `CPvI` | `index:u8=me`, `source:u16` | op `set_preview(conn, source, me=0)` |
| `CutCommand` | `DCut` | `index:u8=me` | op `cut(conn, me=0)` |
| `AutoCommand` | `DAut` | `index:u8=me` | op `auto(conn, me=0)`; uses the active transition style/rate |

### 1.2 aux — `messages/switching.py`

| name | opcode | params | notes |
|---|---|---|---|
| `AuxSourceCommand` | `CAuS` | `index:u8=aux (0-based)`, `source:u16` | mask byte is the constant 1. Op `set_aux_output(conn, aux, source)`; `aux` is 0-based, but the state dict keys it `aux1…auxN` |

### 1.3 transition — `messages/transition.py`

| name | opcode | params | notes |
|---|---|---|---|
| `TransitionSettingsCommand` | `CTTp` | `index:u8=me`, `style?:u8=0 Mix\|1 Dip\|2 Wipe\|3 DVE\|4 Stinger`, `next_transition?:u8=bitfield bit0 BKGD, bit1–4 Key1–4` | ops `set_transition_style(conn, style, me)`; `set_next_transition_layers(conn, *, background, key1..key4, me)` (absolute); `toggle_transition_key(conn, key 0-based, me)` and `toggle_transition_background(conn, me)` (read-modify-write from mixerstate). A zero layer mask is never sent (the ATEM rejects it); the op returns silently |
| `MixSettingsCommand` | `CTMx` | `index:u8=me`, `rate:u8=frames` | op `set_mix_rate(conn, rate_str, me)` |
| `DipSettingsCommand` | `CTDp` | `index:u8=me`, `rate?:u8=frames`, `source?:u16` | ops `set_dip_rate`, `set_dip_source` |
| `WipeSettingsCommand` | `CTWp` | `index:u8=me`, `rate?:u8=frames`, `pattern?:u8=0–17`, `width?:u16=0–10000`, `source?:u16 (border fill)`, `symmetry?:u16=0–10000`, `softness?:u16=0–10000`, `positionx?:u16=0–10000`, `positiony?:u16=0–10000`, `reverse?:bool`, `flipflop?:bool` | u16 mask. Ops `set_wipe_rate`, `set_wipe_pattern(int)`, `set_wipe_fill_source`, `set_wipe_reverse`, `set_wipe_flip_flop`, `set_wipe_width/softness/symmetry(percent 0–100 → ×100, clamped)`, `set_wipe_position_x/y(unit 0–1 → ×10000, clamped)`. Pattern names (ASC XML) are in `macrotransfer/_helpers.py:_WIPE_PATTERN_NAMES`: 0 LeftToRightBar, 1 TopToBottomBar, 2 HorizontalBarnDoor, 3 VerticalBarnDoor, 4 CornersInFourBox, 5 RectangleIris, 6 DiamondIris, 7 CircleIris, 8 TopLeftBox, 9 TopRightBox, 10 BottomRightBox, 11 BottomLeftBox, 12 TopCentreBox, 13 RightCentreBox, 14 BottomCentreBox, 15 LeftCentreBox, 16 TopLeftDiagonal, 17 TopRightDiagonal |
| `DveSettingsCommand` | `CTDv` | `index:u8=me`, `logo_rate?:u8=frames`, `rate?:u8=frames`, `style?:u8`, `fill_source?:u16`, `key_source?:u16`, `key_enable?:bool`, `key_premultiplied?:bool`, `key_clip?:u16=0–1000`, `key_gain?:u16=0–1000`, `key_invert?:bool`, `reverse?:bool`, `flipflop?:bool` | u16 mask. Byte 3 / bit 0 is the logo-wipe rate and byte 4 / bit 1 the DVE rate; this was corrected against ASC, and the class docstring says not to revert it. Ops `set_dve_rate`, `set_dve_style(int)`, `set_dve_fill_source`, `set_dve_key_source`, `set_dve_enable_key`, `set_dve_pre_multiplied`, `set_dve_invert_key`, `set_dve_reverse`, `set_dve_flip_flop`, `set_dve_clip/gain(percent → ×10, clamped 0–1000)`. No op sets `logo_rate`. Known styles (`_DVE_PATTERN_NAMES`, hardware-confirmed): 16–23 SqueezeTopLeft, SqueezeTop, SqueezeTopRight, SqueezeLeft, SqueezeRight, SqueezeBottomLeft, SqueezeBottom, SqueezeBottomRight; 24–31 the same eight as Push…; 34 GraphicLogoWipe. 0–15, 32, 33 and 35+ are unmapped |
| `StingerSettingsCommand` | `CTSt` | `index:u8=me`, `mediaplayer?:u8=MP slot 1–4`, `key_premultiplied?:bool`, `key_clip?:u16=0–1000`, `key_gain?:u16=0–1000`, `key_invert?:bool`, `preroll?:u16=frames`, `duration?:u16=frames`, `triggerpoint?:u16=frames`, `rate?:u16=frames` | u16 mask; a 20-byte payload (the old 22-byte one was silently rejected). Ops `set_stinger_source(MP slot number, not a source ID)`, `set_stinger_rate` = `set_stinger_mix_rate` (duplicates), `set_stinger_clip_duration`, `set_stinger_trigger_point`, `set_stinger_pre_roll` (all `rate_str`), `set_stinger_clip/gain(percent ×10, NOT clamped)`, `set_stinger_pre_multiplied`, `set_stinger_invert_key` |
| `TransitionPreviewCommand` | `CTPr` | `index:u8=me`, `enabled:bool` | restored upstream; not re-exported. Op `set_transition_preview(conn, enabled, me)` |
| `TransitionPositionCommand` | `CTPs` | `index:u8=me`, `position:u16=0–10000` | restored upstream; not re-exported. Op `set_transition_position(conn, position, me)`, clamped 0–10000. This is the T-bar |
| (op only) | — | — | `set_transition_rate(conn, rate_str, me)` reads `transition-settings.style` and sends the matching `CTMx`/`CTDp`/`CTWp`/`CTDv`/`CTSt` rate; it raises `ValueError` on an unknown style |

### 1.4 keyer-usk — `messages/upstream_keyer.py`

Every row takes `index:u8=me` and `keyer:u8` first.

| name | opcode | params | notes |
|---|---|---|---|
| `KeyOnAirCommand` | `CKOn` | `enabled:bool` | ops `set_usk_on_air(conn, keyer, enabled, me)`, `toggle_usk(conn, keyer, me)` (read-modify-write) |
| `KeyFillCommand` | `CKeF` | `source:u16` | op `set_usk_fill_source` |
| `KeyCutCommand` | `CKeC` | `source:u16` | op `set_usk_key_source` |
| `KeyTypeCommand` | `CKTp` | `type?:u8=0 Luma\|1 Chroma\|2 Pattern\|3 DVE`, `fly_enabled?:bool` | ops `set_usk_type(int)`, `set_usk_fly_enabled` |
| `KeyPropertiesLumaCommand` | `CKLm` | `premultiplied?:bool`, `clip?:u16=0–1000`, `gain?:u16=0–1000`, `invert_key?:bool` | ops `set_usk_luma_clip/gain(percent → ×10, clamped)`, `set_usk_luma_invert`, `set_usk_luma_pre_multiplied`, `configure_usk_luma(conn, keyer, *, clip, gain, pre_multiplied, invert, me)` (one packet) |
| `KeyPropertiesAdvancedChromaCommand` | `CACK` | `foreground?:u16`, `background?:u16`, `key_edge?:u16`, `spill?:u16`, `flare?:u16`, `brightness?:i16`, `contrast?:i16`, `saturation?:u16`, `red?:i16`, `green?:i16`, `blue?:i16` | u16 mask; every field is the display value ×1000 (no clamp). Ops `set_usk_chroma_foreground/background/key_edge/spill/flare_suppression` (unit 0–1), `set_usk_chroma_brightness/contrast/red/green/blue` (signed, roughly −1..1 per `state.value_from_thousandths`), `set_usk_chroma_saturation` (0–2.0) |
| `KeyPropertiesAdvancedChromaColorpickerCommand` | `CACC` | `cursor?:bool`, `preview?:bool`, `x?:i16=±16000`, `y?:i16=±9000`, `size?:u16=620–9925`, `Y?:u16`, `Cb?:i16`, `Cr?:i16` | ops `set_usk_chroma_sample(enabled)`, `set_usk_chroma_preview`, `set_usk_chroma_sample_position(x, y unit 0–1 → (v−0.5)×32000 / ×18000)`, `set_usk_chroma_sample_size(unit → ×10000, clamped 620–9925)`, `set_usk_chroma_sampled_color(*, y, cb, cr unit → ×10000)` (for restore; live video under an enabled cursor can overwrite it) |
| `KeyPropertiesPatternCommand` | `CKPt` | `pattern?:u8`, `size?:u16=0–10000`, `symmetry?:u16=0–10000`, `softness?:u16=0–10000`, `position_x?:u16=0–10000`, `position_y?:u16=0–10000`, `invert_pattern?:bool` | ops `set_usk_pattern_style(int)`, `set_usk_pattern_size/symmetry/softness(percent ×100, clamped)`, `set_usk_pattern_position_x/y(unit ×10000, clamped)`, `set_usk_pattern_invert` |
| `KeyPropertiesMaskCommand` | `CKMs` | `enabled?:bool`, `top?:i16`, `bottom?:i16`, `left?:i16`, `right?:i16` | edges ×1000 (display ±9 top/bottom, ±16 left/right). The docstring says "Not Wireshark-validated yet". Ops `set_usk_mask_enabled`, `set_usk_mask_top/bottom/left/right(display, ×1000, no clamp)` |
| `KeyPropertiesDveCommand` | `CKDV` | `size_x?:i32`, `size_y?:i32`, `pos_x?:i32`, `pos_y?:i32`, `rotation?:i32`, `border_enabled?:bool`, `shadow_enabled?:bool`, `border_bevel_enabled?:u8 (bevel type)`, `outer_width?:u16=0–1600`, `inner_width?:u16=0–1600`, `outer_softness?:u8=0–100`, `inner_softness?:u8=0–100`, `bevel_softness?:u8=0–100`, `bevel_position?:u8=0–100`, `border_opacity?:u8=0–100`, `border_hue?:u16`, `border_saturation?:u16`, `border_luma?:u16`, `angle?:u16=0–3590`, `altitude?:u8=10–100`, `mask_enabled?:bool`, `mask_top?:i16=±9000`, `mask_bottom?:i16=±9000`, `mask_left?:i16=±16000`, `mask_right?:i16=±16000`, `rate?:u8=frames` | u32 mask, 26 bits; `__init__(index, keyer, **kwargs)`. Helper method `set_border_color_rgb`. Ops `set_usk_dve_position_x/y(display ×1000; clamped only to the i32 range so off-screen fly positions survive)`, `set_usk_dve_size_x/y(0–2 → 0–2000, clamped)`, `set_usk_dve_rotation(deg ×10)`, `set_usk_dve_border_enabled`, `set_usk_dve_border_hue/saturation/luma(×10; the "SCALE CONTRACT" comment explains the asymmetric read)`, `set_usk_dve_border_opacity`, `set_usk_dve_border_outer_width/inner_width(×100)`, `set_usk_dve_border_outer_softness/inner_softness/bevel_softness/bevel_position`, `set_usk_dve_masked`, `set_usk_dve_top/bottom/left/right(×1000)`, `set_usk_dve_light_direction(deg ×10)`, `set_usk_dve_light_altitude`, `set_usk_dve_shadow`, `set_usk_dve_rate(rate_str)`. No op sets `border_bevel_enabled` |
| `KeyerKeyframeSetCommand` | `SFKF` | `keyframe:u8=1 A\|2 B` | op `set_keyer_fly_keyframe(conn, keyer, 'a'\|'b'\|'full'\|'runToInfinite', me)` maps through `helpers.KEYFRAME_BY_NAME` (a=1, b=2, full=3, runToInfinite=4); the wire docstring documents only A/B |
| `KeyerKeyframeRunCommand` | `RFlK` | `run_to:u8=1 A\|2 B\|3 Full\|4 Infinite` (or `'A'`,`'B'`,`'Full'`,`'Infinite'`), `direction?:u8` | mask bit 1 gates `direction`; `run_to` has no mask bit. Ops `run_flying_key_keyframe(conn, keyer, 'a'\|'b'\|'full', me)`, `run_flying_key_infinite_direction(conn, keyer, direction int, me)`. Direction encodings disagree: the op docstring says 1 TL … 5 centre … 9 BR, while the macro table `_FLYKEY_LOCATION_NAMES` has 0 CentreOfKey, 1 TopLeft, 2 TopCentre, 3 TopRight, 4 MiddleLeft, 6 MiddleRight, 7 BottomLeft, 8 BottomCentre, 9 BottomRight, and no 5 |

### 1.5 keyer-dsk — `messages/downstream_keyer.py`

Every row takes `index:u8=dsk` first. Ops take `dsk_idx=0` as their last keyword.

| name | opcode | params | notes |
|---|---|---|---|
| `DkeyOnairCommand` | `CDsL` | `on_air:bool` | ops `set_dsk_on_air(conn, enabled, dsk_idx)`, `toggle_dsk` (read-modify-write) |
| `DkeyAutoCommand` | `DDsA` | — | op `dsk_auto` |
| `DkeyTieCommand` | `CDsT` | `tie:bool` | op `set_dsk_tie` |
| `DkeyRateCommand` | `CDsR` | `rate:u8=frames` | op `set_dsk_rate(rate_str)` |
| `DkeySetFillCommand` | `CDsF` | `source:u16` | op `set_dsk_fill_source` |
| `DkeySetKeyCommand` | `CDsC` | `source:u16` | op `set_dsk_key_source` |
| `DkeyGainCommand` | `CDsG` | `premultiplied?:bool`, `clip?:u16=0–1000`, `gain?:u16=0–1000`, `invert?:bool` | ops `set_dsk_pre_multiplied`, `set_dsk_clip/gain(percent ×10, clamped)`, `set_dsk_invert_key`, `configure_dsk_gain(conn, *, clip, gain, pre_multiplied, invert, dsk_idx)` (one packet) |
| `DkeyMaskCommand` | `CDsM` | `enabled?:bool`, `top?:i16`, `bottom?:i16`, `left?:i16`, `right?:i16` | edges ×1000. Ops `set_dsk_mask_enabled`, `set_dsk_mask_top/bottom/left/right(display ×1000)` |

### 1.6 fade-to-black — `messages/fade_to_black.py`

| name | opcode | params | notes |
|---|---|---|---|
| `FadeToBlackCommand` | `FtbA` | `index:u8=me` | op `fade_to_black(conn, me=0)`; toggles FTB |
| `FadeToBlackConfigCommand` | `FtbC` | `index:u8=me`, `frames:u8` | op `set_ftb_rate(conn, rate_str, me)` |
| `FadeToBlackEnableCommand` | `FEna` | `index:u8=me`, `enable:bool` | reverse-engineered, with a custom `get_command`. "Disable ME2" is byte-identical to "enable ME1", so the op `set_ftb_disabled(conn, disabled, me)` ignores `me` and always targets ME1 |

### 1.7 color-gen — `messages/color_generator.py`

| name | opcode | params | notes |
|---|---|---|---|
| `ColorGeneratorCommand` | `CClV` | `index:u8=generator 0–1`, `hue?:u16=0–3599 (display 0–359.9°, scale 10)`, `saturation?:u16=0–1000 (display 0–1, scale 1000)`, `luma?:u16=0–1000 (display 0–1)` | the DSL scales, so the class takes display units. Classmethod `from_rgb(index, r, g, b)` with RGB 0–1 (hue = h×359). Ops `set_color_generator_hue(conn, generator, degrees)`, `set_color_generator_saturation/luma(conn, generator, percent 0–100, clamped)` |

### 1.8 media (pool and players) — `messages/media.py`

| name | opcode | params | notes |
|---|---|---|---|
| `CaptureStillCommand` | `Capt` | — | captures program to the next free still slot (the ATEM chooses it); the result shows up as `MPfe`. Op `capture_still(conn)` |
| `ClearStillCommand` | `CSTL` | `slot:u8 (0-based)` | a bare clear is ignored by some models (a 1 M/E Production Studio 4K) unless the session holds the still-store lock. Op `clear_still(conn, slot)` is the bare send; use the locked `ATEMConnection.clear_still` (§3) |
| `MediaplayerSelectCommand` | `MPSS` | `index:u8=media player`, `still?:u8` XOR `clip?:u8` | exactly one of `still`/`clip` (`ValueError` otherwise); `source_type` 1 = still, 2 = clip is derived. Ops `set_media_player_still(conn, player, slot)`, `set_media_player_clip(conn, player, slot)` |

### 1.9 macro — `messages/macros.py`

| name | opcode | params | notes |
|---|---|---|---|
| `MacroActionCommand` | `MAct` | `index:u16=slot (0xFFFF = none)`, `action:u8=0 run\|1 stop\|2 stop-record\|3 insert-user-wait\|4 continue\|5 delete` | keyword-only constructor. Ops `macro_run(conn, slot 0-based)`, `execute_macro(conn, number 1-based)`, `macro_stop(conn)` (index 0xFFFF), `macro_stop_recording(conn)`. No op wraps actions 3, 4 or 5 |
| `MacroRecordCommand` | `MSRc` | `index:u16=slot`, `name:str`, `description:str=''` | variable length, padded to 4 bytes. Op `macro_start_recording(conn, slot, name, description='')` |
| `MacroSleepCommand` | `MSlp` | `frames:u32` | only meaningful while recording. Op `macro_sleep(conn, frames)` |

Macro upload, download and bytecode encode/decode are transfers; see §3.

### 1.10 fairlight — `messages/fairlight.py`

Strip addressing: `source:u16` is the `InPr` source index (cameras 1…N, 1301 XLR mic, 1401 TRS
jack). `channel` is `-1` for a stereo strip and `0`/`1` for split-mono; the wire carries split
byte `0x01`/`0xff` and a channel byte. The first two classes and the EQ classes take **wire ints**
(×100). The dynamics classes `CICP`/`CILP`/`CIXP`/`CMCP`/`CMLP` take **display floats** and
multiply by 100 themselves. The ops below take display units in every case.

| name | opcode | params | notes |
|---|---|---|---|
| `FairlightMasterPropertiesCommand` | `CFMP` | `eq_enable?:bool`, `eq_gain?:i32=±2000`, `dynamics_gain?:u16=0–2000`, `volume?:i32=−10000–1000`, `afv?:bool` | op `set_fairlight_master(conn, *, eq_enable, eq_gain_db ±20, dynamics_makeup_db 0–20, volume_db −100–+10 (−inf → −100), afv)` |
| `FairlightStripPropertiesCommand` | `CFSP` | `source:u16`, `channel:int`, `delay?:u8=frames`, `gain?:i32`, `eq_enable?:bool`, `eq_gain?:i32`, `dynamics_gain?:i32`, `balance?:i16=±10000`, `volume?:i32`, `state?:u8=1 Off\|2 On\|4 AFV` | u16 mask (bit 2 unused). Op `set_fairlight_strip(conn, source, channel, *, input_gain_db −100–+6, eq_enable, eq_gain_db ±20, dynamics_makeup_db 0–20, pan ±100, fader_gain_db −100–+10, mix_option 'Off'\|'On'\|'AudioFollowVideo'\|1\|2\|4, delay_frames)` |
| `FairlightEqBandPropertiesCommand` | `CEBP` | `source:u16`, `channel:int`, `band:u8=0–5`, `enabled?:bool`, `band_filter?:u8`, `band_range?:u8`, `frequency?:u32=Hz`, `gain?:i32`, `q?:i16` | filter (shape) bitfield 1 LowShelf, 2 LowPass, 4 BandPass, 8 Notch, 16 HighPass, 32 HighShelf; range bitfield 1 Low, 2 MidLow, 4 MidHigh, 8 High. Op `set_fairlight_eq_band(conn, source, channel, band, *, enabled, shape name\|int, frequency_range name\|int, frequency_hz clamped 30–21700, gain_db ±20, q clamped 0.30–10.30)` |
| `FairlightMasterEqBandPropertiesCommand` | `CMBP` | `band:u8=0–5`, `enabled?`, `band_filter?`, `band_range?`, `frequency?:u16=Hz`, `gain?:i32`, `q?:u16` | the master counterpart, reverse-engineered from ASC. Op `set_fairlight_master_eq_band(conn, band, *, …)` with the same clamps |
| `FairlightCompressorPropertiesCommand` | `CICP` | `source:u16`, `channel:int=-1`, `enabled?:bool`, `threshold?:dB`, `ratio?:float`, `attack?:ms`, `hold?:ms`, `release?:ms` | u16 mask, bits 8–13. Op `set_fairlight_compressor(conn, source, channel=-1, *, enabled, threshold_db −60–0, ratio 1.2–20, attack_ms 0.7–100, hold_ms 0–4000, release_ms 50–4000)` (live-verified ranges, not clamped) |
| `FairlightLimiterPropertiesCommand` | `CILP` | `source`, `channel=-1`, `enabled?`, `threshold?`, `attack?`, `hold?`, `release?` | op `set_fairlight_limiter(…, threshold_db −60–0, attack_ms 0.7–30, hold_ms 0–4000, release_ms 50–4000)` |
| `FairlightExpanderPropertiesCommand` | `CIXP` | `source`, `channel=-1`, `enabled?`, `mode?:0 expander\|1 gate\|'gate'\|'expander'`, `threshold?`, `range?`, `ratio?`, `attack?`, `hold?`, `release?` | op `set_fairlight_expander(…, mode, threshold_db −50–0, range_db 0–60, ratio 1.0–3.0, attack_ms 0.5–100, hold_ms 0–4000, release_ms 50–4000)` |
| `FairlightMasterCompressorPropertiesCommand` | `CMCP` | `enabled?`, `threshold?`, `ratio?`, `attack?`, `hold?`, `release?` | threshold is a sign-extended i32@4 (the class docstring explains why). Op `set_fairlight_master_compressor(conn, *, …)` |
| `FairlightMasterLimiterPropertiesCommand` | `CMLP` | `enabled?`, `threshold?`, `attack?`, `hold?`, `release?` | op `set_fairlight_master_limiter(conn, *, …)` |
| `SendFairlightLevelsCommand` | `SFLN` | `enable:bool` | opts in to `FMLv`/`FDLv` meters; must be re-sent after every reconnect. Op `enable_fairlight_levels(conn, enable=True)` |

### 1.11 audio-legacy (pre-Fairlight mixer) — `messages/audio_legacy.py`

All four are restored upstream and not re-exported.

| name | opcode | params | notes |
|---|---|---|---|
| `AudioInputCommand` | `CAMI` | `source:u16`, `balance?:i16=±10000`, `volume?:u16=0–65381`, `on?:bool`, `afv?:bool` | `on`/`afv` become `mix_option` 0 off / 1 on / 2 AFV (`on` wins). Op `set_audio_input(conn, source, balance, volume, on, afv)` |
| `AudioMasterPropertiesCommand` | `CAMM` | `volume?:u16=0–65381`, `afv?:bool` | op `set_audio_master` |
| `AudioMonitorPropertiesCommand` | `CAMm` | `enabled?`, `volume?:u16`, `mute?`, `solo?`, `solo_source?:u16`, `dim?`, `dim_volume?:u16` | op `set_audio_monitor(conn, **props)` |
| `SendAudioLevelsCommand` | `SALN` | `enable:bool` | opts in to `AMLv`. Op `enable_audio_levels` |

### 1.12 camera-control — `messages/camera_control.py`

| name | opcode | params | notes |
|---|---|---|---|
| `CameraControlCommand` | `CCmd` | `destination:u8 (255 = broadcast)`, `category:u8`, `parameter:u8`, `relative:bool=False`, `datatype:u8=0 bool\|1 i8\|2 i16\|3 i32\|4 i64\|5 string\|128 fixed16`, `data:list` | restored upstream; not re-exported. Data is zero-padded to 8 bytes; fixed16 = `int(v×2048)`. The library has no category/parameter table. Op `camera_control(conn, destination, category, parameter, relative, datatype, data)` |

### 1.13 hyperdeck — `messages/hyperdeck.py`

| name | opcode | params | notes |
|---|---|---|---|
| `HyperdeckSettingsCommand` | `CXMS` | `slot:u16=0–9 (topology.hyperdecks)`, `network_address?:str IPv4`, `switcher_input?:u16` | reverse-engineered and live-validated; not re-exported. `'0.0.0.0'` clears a slot. Bytes 10–15 (auto-roll, frame delay) are not mapped and are sent as 0. Op `set_hyperdeck_settings(conn, *, slot, network_address, switcher_input)`. The helper reader `plan_binding_push(mx, deck_ip, switcher_input, *, exclude_slots)` returns `(slot, needs_write)` |

This is the deck **binding** only. Deck transport through the switcher is not implemented (§5).

### 1.14 multiviewer — `messages/multiviewer.py`

Both are restored upstream and not re-exported.

| name | opcode | params | notes |
|---|---|---|---|
| `MultiviewInputCommand` | `CMvI` | `index:u8=multiviewer`, `window:u8`, `source:u16` | op `set_multiviewer_input(conn, window, source, index=0)` |
| `MultiviewPropertiesCommand` | `CMvP` | `index:u8`, `layout?:u8=4-bit bitfield (quadrant N split into 4)`, `swap?:bool` | ops `set_multiviewer_layout`, `set_multiviewer_swap` |

### 1.15 supersource — `messages/supersource.py`

Both are restored upstream and not re-exported.

| name | opcode | params | notes |
|---|---|---|---|
| `SupersourceBoxPropertiesCommand` | `CSBP` | `index:u8=supersource`, `box:u8=0–3`, `enabled?:bool`, `source?:u16`, `x?:i16=−4800–4800`, `y?:i16=−3400–3400`, `size?:u16=70–1000`, `masked?:bool`, `top?:u16=0–18000`, `bottom?:u16=0–18000`, `left?:u16=0–32000`, `right?:u16=0–32000` | u16 mask. Op `set_supersource_box(conn, box, index=0, **props)` |
| `SupersourcePropertiesCommand` | `CSSc` | `index:u8`, `fill_source?:u16`, `key_source?:u16`, `layer?:u8=0 background\|1 foreground`, `premultiplied?:bool`, `clip?:u16=0–1000`, `gain?:u16=0–1000`, `invert?:bool` | op `set_supersource_properties(conn, index=0, **props)` |

### 1.16 recording — `messages/recording.py`

Both are restored upstream and not re-exported.

| name | opcode | params | notes |
|---|---|---|---|
| `RecordingSettingsSetCommand` | `CRMS` | `filename?:str[128 B]`, `disk1?:u32`, `disk2?:u32`, `record_in_camera?:bool` | op `set_recording_settings(conn, filename, disk1, disk2, record_in_camera)` |
| `RecorderStatusCommand` | `RcTM` | `recording:bool` | ops `start_recording(conn)`, `stop_recording(conn)` |

### 1.17 streaming — `messages/streaming.py`

All three are restored upstream and not re-exported.

| name | opcode | params | notes |
|---|---|---|---|
| `StreamingServiceSetCommand` | `CRSS` | `name?:str[64 B]`, `url?:str[512 B]`, `key?:str[512 B]`, `bitrate_min?:u32=bps`, `bitrate_max?:u32=bps` | the two bitrates share one mask bit and must be passed together (`ValueError` otherwise). Op `set_streaming_service` |
| `StreamingAudioBitrateCommand` | `STAB` | `bitrate_min:u32`, `bitrate_max:u32` | in practice always 128k. Op `set_streaming_audio_bitrate` |
| `StreamingStatusSetCommand` | `StrR` | `streaming:bool` | ops `start_streaming`, `stop_streaming` |

### 1.18 settings (inputs, video mode, system) — `messages/input_video.py`, `messages/system_info.py`

| name | opcode | params | notes |
|---|---|---|---|
| `InputPropertiesCommand` | `CInL` | `source_index:u16`, `label?:str[≤20 B]`, `short_label?:str[≤4 B]`, `port_type?:u16` | custom mask: a key is applied whenever its argument is not None, so `''` is a valid label. Op `set_input_label(conn, source, *, long_name, short_name)` (truncates to 20/4 bytes). No op sets `port_type`. Fixed internal sources (Black, Bars, Key Mask, Program, Preview) are not renamable |
| `VideoModeCommand` | `CVdM` | `mode:u8=0–29` | **destructive**: outputs resync and clients may be dropped. Op `set_video_mode(conn, mode int\|name)`. Names (`input_video.VIDEO_MODE_NAMES`): 0 NTSC/525i5994, 1 PAL/625i50, 2 NTSC_widescreen, 3 PAL_widescreen, 4 720p50, 5 720p5994, 6 1080i50, 7 1080i5994, 8 1080p2398, 9 1080p24, 10 1080p25, 11 1080p2997, 12 1080p50, 13 1080p5994, 14 2160p2398, 15 2160p24, 16 2160p25, 17 2160p2997, 18 2160p50, 19 2160p5994, 20 4320p2398, 21 4320p24, 22 4320p25, 23 4320p2997, 24 4320p50, 25 4320p5994, 26 1080p30, 27 1080p60, 28 720p60, 29 1080i60. The supported subset per model is in `_VMC` |
| `AutoInputVideoModeCommand` | `AiVM` | `enable:bool` | restored upstream; not re-exported. Op `set_auto_video_mode(conn, enable)` |
| `SaveStartupStateCommand` | `SRsv` | — | restored upstream; not re-exported. Op `save_startup_state(conn)` |
| `ClearStartupStateCommand` | `SRcl` | — | restored upstream; not re-exported. Op `clear_startup_state(conn)` |
| `SetTimeOfDayCommand` | `SToD` | `time:datetime\|int unix seconds` | restored upstream; not re-exported. Op `set_time_of_day(conn, when=None)` (None = now) |
| `TimeRequestCommand` | `TiRq` | — | asks for `Time`; doubles as a NOP/keepalive. No op |

### 1.19 lock and file-transfer (the wire under §3) — `messages/lock.py`, `messages/file_transfer.py`

These are the building blocks of the transfer flows. A CLI reaches them through the §3 entry
points, never one by one.

| name | opcode | params | notes |
|---|---|---|---|
| `LockCommand` | `LOCK` | `store:u16`, `state:bool (True request, False release)` | full-store lock |
| `PartialLockCommand` | `PLCK` | `store:u16`, `slot:u16` | per-slot lock; constant trailer `ff 01` |
| `TransferDownloadRequestCommand` | `FTSU` | `transfer:u16=id`, `store:u16`, `slot:u32` | switcher → client |
| `TransferUploadRequestCommand` | `FTSD` | `transfer:u16`, `store:u16`, `slot:u16`, `length:u32=bytes`, `mode:u16=1 write-RLE\|256 write\|512 erase` | client → switcher. Class constants `MODE_WRITE_RLE`, `MODE_WRITE`, `MODE_ERASE` |
| `TransferDataCommand` | `FTDa` | `transfer:u16`, `data:bytes` | one chunk |
| `TransferFileDataCommand` | `FTFD` | `transfer:u16`, `hash:bytes[16]`, `name?:str`, `description?:str` | slot metadata at the end of an upload |
| `TransferAckCommand` | `FTUA` | `transfer:u16`, `slot:u16` | acks a downloaded chunk |

## 2. State (reads)

### 2.1 How state is stored

`AtemProtocol.mixerstate` is a dict filled by `AtemProtocol.save_field_data` (`protocol.py:272`). A
pooled `ATEMConnection` exposes the same dict as `conn.mixerstate`. For each incoming packet:

1. If the 4-char code has a `Recv` class **and** a `PRETTY` name, the payload is parsed into that
   class and stored under the `PRETTY` key (e.g. `'program-bus-input'`). The attributes in the
   tables below are attributes of that object.
2. If the class has a `KEY_FORMAT`, the leading bytes become nested dict keys:
   `mx['key-on-air'][me][keyer]`. Fairlight classes that carry `strip_id` replace the first key
   with the `"<source>.<subchannel>"` string (`protocol.py:489-498`), so strips live at
   `mx['fairlight-strip-properties']['1301.0']`. With no `KEY_FORMAT` the object is stored bare,
   and the last packet wins.
3. Every other code, including all 27 codes in `AtemProtocol._UNMAPPED_PRETTY`, is stored as
   **raw bytes under the 4-char wire code** (`mx['RCPS']`), unindexed, so the last packet wins. See
   §5.3.
4. Each store raises `change:<key>:<first-index>`, `change:<key>:*` (indexed) or `change:<key>`
   (bare), then `change` with `(key, contents)`. See §4.

There are two read layers on top of `mixerstate`:

- **Readers**: 88 functions `reader(mx, …)` in `atemwire.messages.<module>` that turn wire values into
  display units, with defaults when a packet hasn't arrived.
- **`atemwire.state.build_full_state(conn)`**: one dict for the whole switcher (§2.6), built
  from the readers. It does **not** cover multiviewer, SuperSource, recording, streaming, tally,
  camera control, the legacy audio mixer, media-pool slots, media-player selection, locks, time
  or meters. Those exist only in `mixerstate` and the module readers.

Indexing abbreviations: **me** = per M/E; **me,keyer** = per keyer; **dsk**; **src** = per source ID
(u16); **strip** = per Fairlight strip id `"src.sub"`; **slot** = per media/macro/hyperdeck slot;
**mv,win**; **ss,box**; **bare** = a single object. Types are the parsed Python types (wire type in
brackets where it matters). "Written by" names the §1 command(s) whose effect lands in this packet.

### 2.2 mixeffect, aux, transition

| state key | opcode | attributes | indexed | written by |
|---|---|---|---|---|
| `program-bus-input` | `PrgI` | `index:u8`, `source:u16` | me | `CPgI`, `DCut`/`DAut` (effect) |
| `preview-bus-input` | `PrvI` | `index`, `source:u16`, `in_program:bool` | me | `CPvI` |
| `aux-output-source` | `AuxS` | `index`, `source:u16` | aux | `CAuS` |
| `transition-settings` | `TrSS` | `index`, `style:u8`, `style_next:u8`, `next_transition_bkgd/key1/key2/key3/key4:bool`, the same five with `_next` (pending) | me | `CTTp` |
| `transition-preview` | `TrPr` | `index`, `enabled:bool` | me | `CTPr` |
| `transition-position` | `TrPs` | `index`, `in_transition:bool`, `frames_remaining:u8`, `position:u16 0–10000` | me | `CTPs`, `DAut` (effect) |
| `transition-mix` | `TMxP` | `index`, `rate:u8` | me | `CTMx` |
| `transition-dip` | `TDpP` | `index`, `rate:u8`, `source:u16` | me | `CTDp` |
| `transition-wipe` | `TWpP` | `index`, `rate`, `pattern:u8`, `width`, `source`, `symmetry`, `softness`, `positionx`, `positiony` (u16 0–10000), `reverse`, `flipflop` | me | `CTWp` |
| `transition-dve` | `TDvP` | `index`, `logo_rate:u8`, `rate:u8`, `style:u8`, `fill_source`, `key_source`, `key_enable`, `key_premultiplied`, `key_clip`, `key_gain` (u16 0–1000), `key_invert`, `reverse`, `flipflop` | me | `CTDv` |
| `transition-stinger` | `TStP` | `index`, `mediaplayer:u8`, `key_premultiplied`, `key_clip`, `key_gain`, `key_invert`, `preroll`, `duration`, `triggerpoint`, `rate` (u16 frames) | me | `CTSt` |
| `fade-to-black-state` | `FtbS` | `index`, `done:bool`, `transitioning:bool`, `frames_remaining:u8` | me | `FtbA` (effect) |
| `fade-to-black` | `FtbP` | `index`, `rate:u8` | me | `FtbC` |
| `fade-to-black-enabled` | `FEna` | `enabled:bool`, `disabled:bool` (a byte heuristic, ambiguous on multi-M/E) | bare | `FEna` |

### 2.3 keyers, color, inputs, media, macros

| state key | opcode | attributes | indexed | written by |
|---|---|---|---|---|
| `key-on-air` | `KeOn` | `index`, `keyer`, `enabled:bool` | me,keyer | `CKOn` |
| `key-properties-base` | `KeBP` | `index`, `keyer`, `type:u8`, `enabled:bool`, `fly_enabled:bool`, `fill_source:u16`, `key_source:u16`, `mask_enabled:bool`, `mask_top/bottom/left/right:i16 ×1000` | me,keyer | `CKTp`, `CKeF`, `CKeC`, `CKMs` |
| `key-properties-luma` | `KeLm` | `index`, `keyer`, `premultiplied`, `clip:u16 0–1000`, `gain:u16 0–1000`, `key_inverted` | me,keyer | `CKLm` |
| `key-properties-advanced-chroma` | `KACk` | `index`, `keyer`, `foreground`, `background`, `key_edge`, `spill_suppress`, `flare_suppress` (u16 ×1000), `brightness:i16`, `contrast:i16`, `saturation:u16`, `red/green/blue:i16` (all ×1000) | me,keyer | `CACK` |
| `key-properties-advanced-chroma-colorpicker` | `KACC` | `index`, `keyer`, `cursor:bool`, `preview:bool`, `x:i16`, `y:i16`, `size:u16`, `Y:float=(wire−625)/8544`, `Cb:float=(wire−5000)/5000`, `Cr:float`; method `get_rgb()` | me,keyer | `CACC` |
| `key-properties-pattern` | `KePt` | `index`, `keyer`, `pattern:u8`, `size`, `symmetry`, `softness`, `position_x`, `position_y` (u16 0–10000), `invert:bool` | me,keyer | `CKPt` |
| `key-properties-dve` | `KeDV` | `index`, `keyer`, `size_x`, `size_y`, `pos_x`, `pos_y`, `rotation` (i32), `border_enabled`, `shadow_enabled`, `border_bevel:u8`, `border_outer_width`, `border_inner_width` (u16), `border_outer_softness`, `border_inner_softness`, `border_bevel_softness`, `border_bevel_position`, `border_opacity` (u8), `border_hue:float deg (÷10)`, `border_saturation:float 0–1 (÷1000)`, `border_luma:float 0–1`, `light_angle:u16`, `light_altitude:u8`, `mask_enabled`, `mask_top/bottom/left/right:i16`, `rate:u8` | me,keyer | `CKDV` |
| `key-properties-fly` | `KeFS` | `index`, `keyer`, `is_a_set`, `is_b_set`, `run_to_infinite_index:u8`, `at_keyframe_a`, `at_keyframe_b`, `at_keyframe_full`, `at_keyframe_infinite` | me,keyer | `SFKF`, `RFlK` (effect) |
| `key-properties-fly-keyframe` | `KKFP` | `index`, `keyer`, `keyframe:u8 (1 A, 2 B)`, `size_x`, `size_y` (u32), `pos_x`, `pos_y`, `rotation` (i32), border widths/softness/bevel/opacity, `border_hue/saturation/luma` (scaled like `KeDV`), `light_angle`, `light_altitude`, `mask_top/bottom/left/right:i16` | me,keyer,keyframe | `SFKF` stores the live DVE; no command writes keyframe geometry directly |
| `dkey-properties-base` | `DskB` | `index`, `fill_source:u16`, `key_source:u16` | dsk | `CDsF`, `CDsC` |
| `dkey-properties` | `DskP` | `index`, `tie:bool`, `rate:u8`, `premultiplied`, `clip:u16`, `gain:u16` (0–1000), `invert_key`, `masked`, `top/bottom/left/right:i16 ×1000` | dsk | `CDsT`, `CDsR`, `CDsG`, `CDsM` |
| `dkey-state` | `DskS` | `index`, `on_air:bool`, `is_transitioning:bool`, `is_autotransitioning:bool`, `frames_remaining:u8` | dsk | `CDsL`, `DDsA` (effect) |
| `color-generator` | `ColV` | `index`, `hue:float deg`, `saturation:float 0–1`, `luma:float 0–1`; `get_rgb()` | generator | `CClV` |
| `input-properties` | `InPr` | `index:u16`, `name:str`, `short_name:str`, `default_name:bool`, `source_ports:u16`, `external_port_type:u16`, `port_type:u8` (0 external, 1 black, 2 bars, 3 color, 4 MP, 5 MP key, 6 SuperSource, 7 passthrough, 128 M/E out, 129 aux out, 130 key mask, 131 multiview out), `available_aux`, `available_multiview`, `available_supersource_art`, `available_supersource_box`, `available_key_source`, `available_aux1`, `available_aux2`, `available_usb`, `available_me1..me4` (bool) | src | `CInL` (names; `port_type` field) |
| `mediaplayer-file-info` | `MPfe` | `type:u8 (0 still)`, `index:u16`, `is_used:bool`, `hash:bytes[16] MD5`, `name:bytes` | slot (index only, see §5.4) | `Capt`, `CSTL`, still upload (§3) |
| `mediaplayer-selected` | `MPCE` | `index`, `source_type:u8 (1 still, 2 clip)`, `slot:u8` | media player | `MPSS` |
| `macro-properties` | `MPrp` | `index:u16`, `is_used:bool`, `is_invalid:bool`, `name:bytes`, `description:bytes` | slot | `MSRc` + stop-record, `MAct` 5 (delete), macro upload (§3) |
| `macro-record-status` | `MRcS` | `is_recording:bool`, `index:u16` | bare | `MSRc`, `MAct` 2 |
| `macro-play-status` | `MRPr` | `running:bool`, `waiting:bool`, `is_looping:bool`, `index:u16 (0xFFFF idle)` | bare | `MAct` 0/1/4 |

### 2.4 audio (Fairlight, legacy, meters)

| state key | opcode | attributes | indexed | written by |
|---|---|---|---|---|
| `fairlight-master-properties` | `FAMP` | `eq_enable:bool`, `eq_gain:i16 ×100 dB`, `dynamics_gain:u16 ×100`, `volume:i32 ×100`, `afv:bool` | bare | `CFMP` |
| `fairlight-strip-properties` | `FASP` | `index:u16`, `is_split:u8 (0xff split)`, `subchannel:u8`, `delay:u8`, `gain:i16`, `eq_enable:bool`, `eq_gain:i16`, `dynamics_gain:u16`, `pan:i16`, `volume:i16`, `state:u8 (1 Off, 2 On, 4 AFV)`, `strip_id:str` | strip | `CFSP` |
| `atem-eq-band-properties` | `AEBP` | `index`, `is_split`, `subchannel`, `band_index:u8`, `band_enabled:bool`, `band_possible_filters:u8`, `band_filter:u8`, `band_freq_range:u8`, `band_frequency:u16`, `band_gain:i32`, `band_q:u16`, `strip_id` | strip, band | `CEBP` |
| `atem-master-eq-band-properties` | `AMBP` | `band_index`, `band_enabled`, `band_possible_filters`, `band_filter`, `band_freq_range`, `band_frequency`, `band_gain`, `band_q` | band | `CMBP` |
| `fairlight-compressor-properties` | `AICP` | `index`, `is_split`, `subchannel`, `enabled`, `threshold:i32`, `ratio:u16`, `attack`, `hold`, `release` (i32, all ×100), `strip_id` | strip | `CICP` |
| `fairlight-limiter-properties` | `AILP` | `index`, `is_split`, `subchannel`, `enabled`, `threshold`, `attack`, `hold`, `release` (i32 ×100), `strip_id` | strip | `CILP` |
| `fairlight-expander-properties` | `AIXP` | `index`, `is_split`, `subchannel`, `enabled`, `mode:u8 (0 expander, 1 gate)`, `threshold:i32`, `range:u16`, `ratio:u16`, `attack`, `hold`, `release` (×100), `strip_id` | strip | `CIXP` |
| `fairlight-master-compressor-properties` | `MOCP` | `enabled`, `threshold:i16`, `ratio:u16`, `attack`, `hold`, `release` (i32, ×100) | bare | `CMCP` |
| `fairlight-master-limiter-properties` | `AMLP` | `enabled`, `threshold:i16`, `attack`, `hold`, `release` (×100) | bare | `CMLP` |
| `fairlight-audio-input` | `FAIP` | `index:u16`, `type:u8`, `number:u8`, `split:u8`, `level:u8` (analog level) | src | — (read-only) |
| `fairlight-tally` | `FMTl` | `num:u16`, `tally:{strip_id: bool}` | bare | — |
| `fairlight-headphones` | `FMHP` | `volume:i32`, `unmuted:bool` | bare | — |
| `fairlight-solo` | `FAMS` | `solo:bool`, `channel:u8`, `is_split_lr:u8`, `subchannel:u8` | bare | — |
| `fairlight-strip-delete` | `FASD` | (raw only) | bare | — (never acted on; §5.4) |
| `fairlight-meter-levels` | `FMLv` | `is_split`, `subchannel`, `index`, `strip_id`, `input:(lvlL, lvlR, peakL, peakR)`, `expander_gr`, `compressor_gr`, `limiter_gr`, `output:(4)`, `level:(4)` (dB floats) | **bare** (last strip wins; listen to `change:fairlight-meter-levels`) | `SFLN` opt-in |
| `fairlight-master-levels` | `FDLv` | `input:(4)`, `compressor_gr`, `limiter_gr`, `output:(4)`, `level:(4)` (dB) | bare | `SFLN` opt-in |
| `audio-input` | `AMIP` | `index:u16`, `type:u8`, `number:u8`, `plug:u8` (`plug_name()`: 0 Internal, 1 SDI, 2 HDMI, 3 Component, 4 Composite, 5 SVideo, 32 XLR, 64 AES, 128 RCA), `state:u8`, `volume:u16`, `balance:i16`, `strip_id` | src | `CAMI` |
| `audio-mixer-master-properties` | `AMMO` | `volume:u16`, `afv:bool` | bare | `CAMM` |
| `audio-mixer-monitor-properties` | `AMmO` | `enabled`, `volume:u16`, `mute`, `solo`, `solo_source:u16`, `dim`, `dim_volume:u16` | bare | `CAMm` |
| `audio-mixer-tally` | `AMTl` | `num`, `tally:{"src.0": bool}` | bare | — |
| `audio-meter-levels` | `AMLv` | `count`, `master:(4 dB)`, `monitor:(4 dB)`, `input:{src: (4 dB)}` | bare | `SALN` opt-in |

### 2.5 outputs, devices, system

| state key | opcode | attributes | indexed | written by |
|---|---|---|---|---|
| `multiviewer-properties` | `MvPr` | `index`, `layout:u8`, `flip:bool`, `u1`, `top_left_small`, `top_right_small`, `bottom_left_small`, `bottom_right_small` | mv | `CMvP` |
| `multiviewer-input` | `MvIn` | `index`, `window`, `source:u16`, `vu:bool` (VU supported), `safearea:bool` (supported) | mv,win | `CMvI` |
| `multiviewer-vu` | `VuMC` | `index`, `window`, `enabled:bool` | mv,win | — (no command) |
| `multiviewer-safe-area` | `SaMw` | `index`, `window`, `enabled:bool` | mv,win | — (no command) |
| `supersource-properties` | `SSrc` | `index`, `fill_source`, `key_source`, `layer:u8`, `premultiplied`, `clip:u16`, `gain:u16`, `inverted` | ss | `CSSc` |
| `supersource-box-properties` | `SSBP` | `index`, `box`, `enabled`, `source`, `x:i16`, `y:i16`, `size:u16`, `masked`, `mask_top/bottom/left/right:u16` | ss,box | `CSBP` |
| `hyperdeck-settings` | `RXMS` | `index:u16`, `network_address:str`, `input:u16`, `configured:bool` | slot | `CXMS` |
| `camera-control-data-packet` | `CCdP` | `destination`, `category`, `parameter`, `datatype:u8`, `length`, `data:tuple` (fixed16 → float) | dest,cat,param | `CCmd` |
| `recording-disk` | `RTMD` | `index:u32`, `time_available:u32 s`, `status:u16`, `volumename:str`, `is_attached`, `is_ready`, `is_recording`, `is_deleted` | **bare** (last disk wins) | — |
| `recording-settings` | `RMSu` | `filename:str`, `disk1:int\|None`, `disk2:int\|None`, `record_in_cameras:bool` | bare | `CRMS` |
| `recording-status` | `RTMS` | `status:u16`, `time_available:int\|None`, `is_recording`, `is_stopping`, `disk_full`, `disk_error`, `disk_unformatted`, `has_dropped` | bare | `RcTM` |
| `recording-duration` | `RTMR` | `hours`, `minutes`, `seconds`, `frames`, `has_dropped_frames` | bare | — |
| `streaming-service` | `SRSU` | `name:str`, `url:str`, `key:str`, `min:u32`, `max:u32` | bare | `CRSS` |
| `streaming-audio-bitrate` | `STAB` | `min:u32`, `max:u32` | bare | `STAB` |
| `streaming-status` | `StRS` | `status:i16` (observed: −1 unknown, 0 nothing, 1 idle, 2 connecting, 4 on-air, 22/36 stopping) | bare | `StrR` |
| `streaming-stats` | `SRSS` | `bitrate:u32`, `cache:u16` | bare | — |
| `tally-index` | `TlIn` | `num`, `tally:[(program, preview)]` | bare | — |
| `tally-source` | `TlSr` | `num`, `tally:{src: (program, preview)}` | bare | — |
| `video-mode` | `VidM` | `mode:u8`, `resolution:int`, `interlaced:bool`, `rate:float`, `widescreen:bool`; `get_label()`, `get_resolution()`, `get_pixels()`. **Raises `ValueError` on a mode outside 0–29** | bare | `CVdM` |
| `video-mode-capability` | `_VMC` | `modes:[{modenum, mode:VideoModeField, multiview:[…], downscale:[…], reconfigure:bool}]` | bare | — |
| `auto-input-video-mode` | `AiVM` | `enabled:bool`, `detected:bool` | bare | `AiVM` |
| `sdi-3g-level` | `V3sl` | `level:u8 (0 Level B, 1 Level A)` | bare | — (no command) |
| `firmware-version` | `_ver` | `major:u16`, `minor:u16`, `version:str` | bare | — |
| `product-name` | `_pin` | `name:str`, `model:u8` | bare | — |
| `device-identity` | `WhoI` | `device_id`, `ip`, `hostname`, `name` (str) — not sent by all firmware | bare | — (not re-exported) |
| `topology` | `_top` | `me_units`, `sources`, `downstream_keyers`, `aux_outputs`, `mixminus_outputs`, `mediaplayers`, `multiviewers`, `rs485`, `hyperdecks`, `dve`, `stingers`, `supersources` (u8), `multiviewer_routable:bool` | bare | — |
| `mixer-effect-config` | `_MeC` | `index`, `keyers:u8` | me | — |
| `mediaplayer-slots` | `_mpl` | `stills:u8`, `clips:u8` | bare | — |
| `time` | `Time` | `hours`, `minutes`, `seconds`, `frames`, `dropframe:bool`; `total_seconds()` | bare | `TiRq` (request), `SToD` (sets the clock) |
| `time-config` | `TCCc` | `mode:u8 (0 freerun, 1 time-of-day)` | bare | — (no command) |
| `lock-obtained` | `LKOB` | `store:u16` | consumed by the transfer engine, **not stored** | `LOCK`/`PLCK` |
| `lock-state` | `LKST` | `store:u16`, `state:bool`, `u1` | bare | `LOCK` |
| `file-transfer-*` | `FTDa`/`FTDE`/`FTDC`/`FTCD` | `transfer`, `size`/`status`/`count`… | consumed by the transfer engine, **not stored** | §3 |
| `transfer-complete` | `*XFC` | `store`, `slot`, `upload:bool` | consumed (TCP-proxy transport only) | — |
| `InCm` | `InCm` | raw bytes (end of the initial state dump) | bare | — |

### 2.6 `build_full_state(conn)` (the assembled snapshot)

`atemwire.state.build_full_state(connection) -> dict` (`state.py:1038`) reads `connection.mixerstate`
and `connection.last_run_macro_index`. It returns `{'is_connected': False}` when there is no
connection or the mixerstate is empty. Otherwise:

```
is_connected: True
mes: [ per M/E, sized by me_count(mx) ]
  program, preview: int
  transition: {style, rate 's:ff', in_transition, position (raw 0–10000), frames_remaining,
               selection {background, key1..key4}, wipe_pattern, dip_source,
               mix_rate, dip_rate, wipe_rate, dve_rate, stinger_rate ('s:ff'),
               wipe_fill_source, wipe_flip_flop, wipe_position_x/y (0–1), wipe_reverse,
               wipe_softness/symmetry/width (%),
               stinger {source (MP slot), clip_duration, trigger_point, mix_rate, pre_roll (frames),
                        clip, gain (%), pre_multiplied, invert_key, *_str ('s:ff')},
               dve {fill_source, key_source, enable_key, clip, gain (%), pre_multiplied,
                    invert_key, style (int|None), reverse, flip_flop}}
  usk: {states: [bool], types: [int], data: [per keyer: type, fill_source, key_source,
        luma_* (4), chroma_* (≈22 incl. sample position/size/colour), pattern_* (7),
        mask_* (5), dve_* (26), fly_* (7)]}
  ftb: {active, rate_str, in_transition, frames_remaining, disabled}
dsks: [ {on_air, tie, rate, rate_str, in_transition, is_auto_transitioning, frames_remaining,
         fill_source, key_source, mask_enabled, mask_top/bottom/left/right, pre_multiplied,
         clip, gain (%), invert_key} ]
colorGenerators: {idx: {hue, saturation (%), luma (%), hex}}
macros: [ {number 1-based, name} ] — FIRST 12 SLOTS ONLY
lastRunMacro: {index, name}
auxOutputs: {aux1..auxN: source}            (N from _top, else 6)
hyperdecks: [ {slot, network_address, input, configured} ]
audio: {present, master {…, eq_bands[6]}, strips [ {source, channel, subchannel, split,
        strip_id, name, short_name, mix_type, mix_state, volume_db, input_gain_db, pan,
        delay_frames, eq_enable, eq_gain_db, eq_bands[6], dynamics_makeup_db, soloed,
        compressor, limiter, expander, input_type, analog_level} ],
        headphones {present, volume_db, unmuted}, solo {any_soloed, strip_id}}
videoMode: {format, id, fps}
videoModes: [ {id, label} ]
inputLabels: {inputs: [], outputs: [], media: []}   ({source, long, short})
sources: [ {source, long, short, port_type, available_aux, available_key_source,
            available_multiview, available_me1..me4} ]
topology: {meCount, usksPerMe[], dsks, auxOutputs, inputs, outputs, mediaPlayers,
           mediaPoolStills, mediaPoolClips, colorGenerators, multiviewWindows,
           fairlightStrips, macros}
atem_name: WhoI name or _pin product name
```

Reader functions not used by `build_full_state`, which a CLI can call directly:
`transition.dve_style`, `upstream_keyer.usk_fly_keyframe(mx, me, k, which='A'|'B')`,
`media.mediaplayer_slots/slot_info/selected`, `macros.macro_play_running/macro_play_index`,
`fade_to_black.*` (also composed), `system_info.video_resolution/sdi_3g_level`,
`hyperdeck.plan_binding_push`, `fairlight.fairlight_master_compressor/limiter`,
`input_video.input_label`. Helpers in `state`: `me_count(mx)`, `me_keyer_count(mx, me)`,
`display_fps(mx)`, `decode_name`, `md5_hex`.

### 2.7 Read/write cross-reference

**Packets with no write command (read-only):** `_ver`, `_pin`, `WhoI`, `_top`, `_MeC`, `_mpl`,
`_VMC`, `V3sl`, `TCCc`, `TlIn`, `TlSr`, `AMTl`, `FMTl`, `FAIP`, `FMHP`, `FAMS`, `FASD`, `VuMC`,
`SaMw`, `RTMD`, `RTMR`, `SRSS`, `FMLv`, `FDLv`, `AMLv`, `InCm`, `KKFP` (only snapshotted from the
live DVE by `SFKF`), and the transfer/lock notifications (`LKOB`, `LKST`, `FTDa`, `FTDE`, `FTDC`,
`FTCD`, `*XFC`). Within writable packets these fields are reported but never written: `InPr`
(`source_ports`, `default_name`, `available_*`, the `port_type` category), `MvIn` (`vu`,
`safearea` capability flags), `TrSS` (pending `_next` fields), `TrPs` (`in_transition`,
`frames_remaining`), `DskS` (transition flags), `FtbS`, `KeFS`, every `*_possible_filters`,
`AMIP` (`type`, `plug`), `MPfe` (`hash`, `type`).

**Commands with no state of their own:** `MSlp` (recorded into a macro), `SRsv`, `SRcl`, `SFLN`
and `SALN` (they switch meter streams on; the toggle isn't reported), `TiRq` (reply is `Time`),
`SToD`, and the pure actions whose effect shows up elsewhere: `DCut`, `DAut`, `FtbA`, `DDsA`,
`RFlK`, `Capt`. Also `LOCK`, `PLCK`, `FTSU`, `FTSD`, `FTDa`, `FTFD`, `FTUA`, which only show up
in the transfer flow. At field level, the `CKDV`/`CTDv` fields are all echoed, but `CInL`'s
`port_type` writes the *external* port type (`InPr.external_port_type`), not the
`InPr.port_type` category.

## 3. Transfers and special operations

### 3.0 The transfer engine (shared by everything below)

`AtemProtocol` runs **one transfer lane per session** (`protocol.py`). It has a per-store queue of
`TransferTask`s (`_transfer.py:9`), one transfer in flight, and a lock table. Store ids in code:
**0 = stills** (`FTDC` bytes are RLE-decoded) and **0xFFFF = macros** (lock-exempt, `FTSU` magic
`0x03`, `FTSD` mode `0x0300`). No code uses any other store.

- **Locking:** a still transfer requests `PLCK(store, slot)`, and a still clear requests
  `LOCK(store, True)`. Work starts on `LKOB`. After each frame the lock is released
  (`LOCK(store, False)`), and the `LKST` echo starts the next queued task. If another session's
  release is seen while work is queued, the lock is re-requested at once. A lock request is not
  repeated within 1.0 s (`LOCK_REREQUEST_SECONDS`). There is no timer-driven retry: a dropped
  request is re-sent only on the next event.
- **`FTDE` status handling:**
  - 1 (try again): re-send the request with the same tid.
  - 5 (no lock): re-request the lock.
  - 6 (another session collided): release, keep the task, re-request on the next release.
  - Any other status: fatal for that task. The task is dropped, the lock released, and
    `file-transfer-error` raised.

  Statuses 1, 5 and 6 never reach callers.
- **Upload pacing:** the switcher grants budgets with `FTCD(size, count)`. Chunks go out as `FTDa`
  (an RLE header word is never split), then `FTFD(name, description, md5)` once the send queue
  drains. Download chunks are acked one by one with `FTUA(tid, 0)`.
- **The protocol layer has no timeouts.** A transfer ends on `FTDC`, a fatal `FTDE`, or
  `abort_transfers()`. Timeouts live in the callers below.
- Lane control: `AtemProtocol.abort_transfers()` clears the lane and releases held locks;
  `release_locks_now()` writes the unlocks straight to the socket and is meant for teardown.

### 3.1 Stills

| operation | entry point | parameters | behaviour |
|---|---|---|---|
| **download** | `ATEMConnection.download_still` (`connection.py:188`); on the facade, `atem.raw.download_still` | `(slot:int, *, timeout:float=30.0, progress_callback:Callable[[float],None]=None) -> bytes` | Blocking and serialised per connection. Flow: `PLCK(0,slot)` → `LKOB` → `FTSU` → `FTDa`×N/`FTUA` → `FTDC` → unlock. Returns the **RLE-decoded YCbCrA 4:2:2 frame** (w×h×4 bytes; actually a `bytearray`). Convert it with `imaging.atem_to_rgb`. Raises `ConnectionDeadError`, `RuntimeError` (fatal `FTDE`) or `TimeoutError` (after `abort_transfers`) |
| **upload** | **no high-level entry point**. Only `AtemProtocol.upload` (`protocol.py:682`) | `(store, index, data, compress=True, compressed=False, name=None, description=None, size=None, task=None) -> None` | Non-blocking: it only queues, and someone must pump `loop()`. `data` is a YCbCrA frame from `imaging.rgb_to_atem`. The MD5 is taken over the uncompressed frame. Flow: `PLCK` → `LKOB` → `FTSD(mode 1 = RLE)` → `FTCD` → `FTDa`… → `FTFD` → `FTDC` → `upload-done(store, slot)`. Progress arrives as `upload-progress(store, slot, percent, done, total)`. `size` is unused. `compressed=True` needs `compress=False`. `aggressive_drain=True` (constructor only, so not reachable through the pool) drains 10 packets per trigger instead of 5. The working reference caller is in WebATEM: `atem_control/uploader.py` (its own `AtemProtocol(ip, aggressive_drain=True)`, pumps until `upload-done`, then checks the `MPfe` hash) |
| **clear (locked)** | `ATEM.clear_still` / `ATEMConnection.clear_still` (`connection.py:196`); underneath, `AtemProtocol.queue_clear(store, index)` / `dequeue_clear(task)` | `(slot:int, *, timeout:float=5.0) -> None` | `LOCK(0,True)` → `LKOB` → `CSTL` → `clear-dispatched` → unlock. Returns once the clear has been **dispatched**, not confirmed; watch `MPfe.is_used` to confirm. Use this, not the bare `media.clear_still` op (§1.8) |
| capture | `media.capture_still(conn)` | — | single packet `Capt`, listed for completeness |

**Formats and conversion** (`atemwire.mediaconvert`, a C extension; wrappers in `imaging.py`):

- `rgb_to_atem(data, width, height, premultiply=False) -> bytes`
  - Takes RGBA8888 and returns the switcher's 10-bit YCbCrA 4:2:2. Each 2 pixels become two
    big-endian u32 words, `A[12] | Cb/Cr[10] | Y[10]`.
  - BT.709 with limited range: Y 64–940, Cb/Cr 44–960, alpha 61–937.
  - Chroma is taken from the **second pixel of each pair only**, not averaged.
  - `premultiply` multiplies RGB by alpha first.
- `atem_to_rgb(data, width, height) -> bytes`: the inverse, returning RGBA8888. Alpha 0 round-trips
  to 12.
- **Both converters parse `width`/`height` and never use them.** They walk the byte length, which
  must be a multiple of 8 (`ValueError` otherwise). Output length equals input length.
- `rle_encode(data) -> bytes` (C) and `rle_decode(data) -> bytearray` (pure Python) work on 8-byte
  words. The first word of a run is written literally. If it then repeats at least 3 more times, the
  repeats become `FE×8` + a big-endian u64 count + the word (`mediaconvertmodule.c:266-298`). Input that
  contains the word `FEFEFEFEFEFEFEFE` raises.
- **atemwire does no image decoding, resizing or fitting**, and has no Pillow dependency outside
  the profile PNG export. The bytes passed in must already be exactly w×h×4 for the current
  mode. WebATEM's `atem_control/uploader.py:292` `_prepare_frame` does the fitting: Pillow
  open → RGBA → LANCZOS fit-inside (letterbox/pillarbox) on a transparent canvas of the mode's
  size → `rgb_to_atem`, with premultiply only for `.png`.

**Video modes.** The frame size comes from `VideoModeField.get_resolution()` (`video_resolution(mx)`):

| resolution | frame | modes |
|---|---|---|
| 525 | 720×480 | 0 525i59.94 4:3, 2 525i59.94 16:9 |
| 625 | 720×576 | 1 625i50 4:3, 3 625i50 16:9 |
| 720 | 1280×720 | 4 720p50, 5 720p59.94, 28 720p60 |
| 1080 | 1920×1080 | 6 1080i50, 7 1080i59.94, 29 1080i60, 8 1080p23.98, 9 1080p24, 10 1080p25, 11 1080p29.97, 26 1080p30, 12 1080p50, 13 1080p59.94, 27 1080p60 |
| 2160 | 3840×2160 | 14–19: 23.98, 24, 25, 29.97, 50, 59.94 |
| 4320 | 7680×4320 | 20–25: same rates |

The labels above are `get_label()` output, which keeps the decimal point. The names
`set_video_mode` accepts (`VIDEO_MODE_NAMES`) drop it (`1080p5994`). The modes a given switcher
supports are in `_VMC`, read with `available_video_modes(mx)`.

**Clips and audio: not implemented.** There is no clip or audio upload or download, and no
thumbnail transfer (a "thumbnail" means a full frame download). Clip and audio state arrives only
as raw `MPCS`, `RCPS` and `MPAS` bytes.

### 3.2 Macro download, upload and bytecode — `atemwire.macrotransfer`

`macrotransfer/__init__.py:435-442` exports `download_macro_bytecode`, `decode_macro_bytecode`,
`encode_single_op`, `encode_macro_bytecode`, `upload_macro_bytecode` and `MACRO_STORE`. The
package docstring in `atemwire/__init__.py` does not list `atemwire.macrotransfer` among its stable
layers, so whether this module is covered by the version number is unstated.

| entry point | signature | notes |
|---|---|---|
| `ATEMConnection.download_macro` | `(slot:int, *, timeout:float=10.0) -> bytes` | `connection.py:254`. The pooled path: serialised by the connection's transfer lock, and the worker pumps the loop. Returns `b''` for an empty slot. Raises `ConnectionDeadError`, `TimeoutError`, or `RuntimeError` (rejected by `FTDE`) |
| `download_macro_bytecode` | `(protocol, slot:int, *, timeout=10.0, progress_callback=None) -> bytes` | `macrotransfer/_io.py:42`. For a **bare** `AtemProtocol`: it pumps `protocol.loop()` itself. `progress_callback` never fires (the protocol suppresses progress for store `0xFFFF`). A fatal `FTDE` surfaces as `TimeoutError` |
| `upload_macro_bytecode` | `(protocol, slot:int, name:str, description:str, bytecode:bytes, *, timeout=10.0) -> None` | `_io.py:105`. Does **not** pump the loop; on a pooled connection pass `conn.protocol`, and the worker pumps. Raises `RuntimeError` (not connected, or fatal `FTDE`) or `TimeoutError`. Name and description are packed `64s`/`128s` (truncated, with no guaranteed NUL). There is **no upload method on `ATEMConnection` or `ATEM`** |
| `decode_macro_bytecode` | `(raw:bytes, mx:dict=None) -> list[dict]` | each op is `{'id': <ASC XML op id>, <attr>: str, …}`. `mx` is accepted but unused |
| `encode_single_op` | `(op:dict) -> bytes` | raises `ValueError` for an unknown id or a missing attribute; some bad values leak `OSError`/`struct.error` (§5.4) |
| `encode_macro_bytecode` | `(ops:list[dict]) -> bytes` | concatenation; aborts on the first error |

**Flow.** Store `0xFFFF` takes **no lock** (`protocol.py:840`).

- **Download:** `FTSU(tid, 0xFFFF, slot)` → `FTDa` chunks, each acked with `FTUA` → `FTDC`. The raw
  bytes are not RLE-decoded.
- **Upload:** `FTSD(tid, 0xFFFF, slot, len, mode 0x0300)` → `FTCD` budget → `FTDa` chunks →
  `FTFD(name, description, md5 of the bytecode)` → `FTDC` → `upload-done`. A zero-length upload
  sends `FTFD` inline. The name and description of an existing slot are in
  `mx['macro-properties'][n]` (raw bytes); `macros.macro_entry` returns only `{is_used, name}`.

**Bytecode.** A macro is a concatenation of ops, with no header, count or padding. Each op is
`u16 LE total_len` (header included), then `u16 LE op_code`, then params. Everything is
**little-endian**, unlike the big-endian live protocol. Fixed-point values are Q16.16 (`raw/65536`),
written with at most 6 decimals. Decoded attributes are strings (`'True'`/`'False'` for bools).
Ops are identified by **u16 codes, not 4-char commands**.

**146 op codes, all encode + decode** (`_KNOWN_OPS`, verified by import): upstream_keyer 56,
switching 40, fairlight 35, downstream_keyer 10, media 2, hyperdeck 2, flow 1. Any other code
round-trips as `{'id': 'Unknown_0xNNNN', 'rawParams': hex}`.

| file | codes → ASC XML op ids |
|---|---|
| switching (40) | 0x0002 ProgramInput, 0x0003 PreviewInput, 0x001F AuxiliaryInput, 0x0004 Cut, 0x0005 Auto, 0x00A7 FadeToBlack, 0x00A5 FadeToBlackRate, 0x0202 FadeToBlackEnabled, 0x000C VideoMode, 0x0083 TransitionStyle, 0x0084 TransitionSource, 0x0087 TransitionMixRate, 0x0088 TransitionDipRate, 0x0089 TransitionDipInput, 0x007C TransitionWipeRate, 0x007D TransitionWipePattern, 0x007E TransitionWipeBorderWidth, 0x007F TransitionWipeBorderSoftness, 0x0080 TransitionWipeBorderFillInput, 0x0081 TransitionWipeAndDVEReverse, 0x0082 TransitionWipeAndDVEFlipFlop, 0x0015/0x0016 TransitionWipeX/YPosition, 0x003A TransitionDVERate, 0x0034 TransitionDVEPattern, 0x00D7 TransitionDVEFillInput, 0x00D8 TransitionDVECutInput, 0x00D9 TransitionDVECutInputEnable, 0x008C TransitionStingerSourceMediaPlayer, 0x008D …ClipDuration, 0x008E …TriggerPoint, 0x008F …MixRate, 0x0090 …PreRoll, 0x0092 …DVEClip, 0x0093 …DVEGain, 0x0094 …DVEInvert, 0x0095 …DVEPreMultiply, 0x001C/0x001D/0x001E ColorGeneratorHue/Saturation/Luminescence |
| upstream_keyer (56) | 0x0025 KeyCutInput, 0x0026 KeyFillInput, 0x0027 KeyOnAir, 0x0028 KeyType, 0x0029 LumaKeyClip, 0x002A LumaKeyGain, 0x002B KeyFlyEnable, 0x002C LumaKeyInvert, 0x002D LumaKeyPreMultiply, 0x002F KeyMaskEnable, 0x0030–0x0033 KeyMaskTop/Bottom/Left/Right, 0x0035 DVEKeyMaskEnable, 0x0036–0x0039 DVEKeyMaskTop/Bottom/Left/Right, 0x0046 DVEAndFlyKeyRate, 0x0047/0x0048 DVEAndFlyKeyX/YSize, 0x004A/0x004B DVEAndFlyKeyX/YPosition, 0x004D DVEKeyShadowEnable, 0x0058 DVEKeyShadowDirection, 0x0059 DVEKeyShadowAltitude, 0x004E DVEKeyBorderEnable, 0x005A–0x005C DVEKeyBorderHue/Saturation/Luminescence, 0x005E/0x005F DVEKeyBorderOuter/InnerWidth, 0x0060/0x0061 DVEKeyBorderOuter/InnerSoftness, 0x0062 DVEKeyBorderOpacity, 0x012C–0x0136 AdvancedChromaKey ForegroundLevel/BackgroundLevel/KeyEdge/SpillSuppress/FlareSuppress/ForegroundBrightness/ForegroundContrast/ForegroundColour/ForegroundRed/ForegroundGreen/ForegroundBlue, 0x0137 …SamplingModeEnabled, 0x0138 …PreviewEnabled, 0x0139–0x013B …CursorXPosition/YPosition/Size, 0x0050 FlyKeySetKeyFrame, 0x0054 FlyKeyRunToKeyFrame, 0x0052 FlyKeyRunToFull, 0x0056 FlyKeyRunToInfinity |
| downstream_keyer (10) | 0x0096 DownstreamKeyFillInput, 0x0097 …CutInput, 0x0098 …Rate, 0x0099 …Auto, 0x009A …OnAir, 0x009B …Tie, 0x009C …Clip, 0x009D …Gain, 0x009E …MaskEnable, 0x00A4 …PreMultiply |
| fairlight (35) | prefix `FairlightAudioMixer`: 0x014B InputSourceFaderGain, 0x014A InputSourceMixType, 0x016B MasterOutFaderGain, 0x0147 InputSourceInputGain, 0x0156 InputSourceDynamicsGain, 0x014D InputSourceEqualiserGain, 0x014E–0x0153 InputSourceEqualiserBand Enabled/Shape/Range/Frequency/Gain/QFactor, 0x015F–0x0164 InputSourceCompressor Enabled/Threshold/Ratio/Attack/Hold/Release, 0x0165–0x0169 InputSourceLimiter Enabled/Threshold/Attack/Hold/Release, 0x0157 InputSourceExpanderEnabled, 0x0158 …GateModeEnabled, 0x0159–0x015E InputSourceExpander Threshold/Range/Ratio/Attack/Hold/Release, 0x0184–0x0187 HeadphoneOut Gain/MasterGain/TalkbackGain/SidetoneGain |
| media (2) | 0x00DA MediaPlayerSourceStillIndex, 0x00E1 MediaPlayerSourceStill |
| hyperdeck (2) | 0x0110 HyperDeckNetworkAddress (IPv4 stored byte-reversed), 0x011A HyperDeckInput |
| flow (1) | 0x0007 MacroSleep (`frames`) |

Attribute names follow ASC's macro XML (`mixEffectBlockIndex`, `keyIndex`, `input`, `rate`, …).
Sources encode and decode through the private `_INTERNAL_SOURCE_NAMES` table (§1 conventions),
with `Camera1…20` for 1–20. The enum tables (transition style, wipe pattern, DVE pattern, key
type, fly-key location, Fairlight mix type, EQ shape and range) are the same values listed in §1.


### 3.3 Profile save and restore — `atemwire.profile`

The top-level `atemwire` exports `Profile`, `ApplyOptions` and `ApplyResult`.
`atemwire.profile.__all__` adds `SaveOptions`, `MixEffectOptions`, `download_media_pool_images`,
`PROFILE_MAJOR_VERSION` (2) and `PROFILE_MINOR_VERSION` (1).

| entry point | signature | notes |
|---|---|---|
| `Profile.from_atem` | `(atem, options:SaveOptions=None) -> Profile` | Takes anything with `.mixerstate` or `.raw.mixerstate`. First calls `ready.wait_state_settled` (polls only; returns within 3 s at most), then downloads every used macro slot in turn (up to 10 s each). Worst case ≈ 3 s + 10 s × used macros. **With a bare `AtemProtocol` the macros are saved with metadata only and no ops**; the error is swallowed |
| `Profile.from_xml` / `from_file` | `(xml:str\|bytes)` / `(path)` | refuses NUL bytes, `<!DOCTYPE` and `<!ENTITY`; the root tag and version are not validated |
| `Profile.to_xml` / `to_file` | `() -> str` / `(path)` | ASC-style XML |
| properties | `root`, `major_version`, `minor_version`, `product`, `video_mode` | `video_mode` is the raw XML string |
| `Profile.macros()` | `-> list[{index, name, description, ops}]` | ops in the §3.2 dict form |
| `Profile.apply` | `(atem, options:ApplyOptions=None) -> ApplyResult` | Takes an `ATEM`, anything with `.send`, or an `AtemProtocol` (wrapped so `send` is synchronous). Setters only queue. It blocks only for a 3 s sleep after a video-mode change and for macro uploads (10 s each, which **need someone pumping the loop**). Sections run in order: video_mode, inputs, settings_flags, color_generators, aux, transition, upstream_keyers, downstream_keys, fade_to_black, media_players, audio, multiview (placeholder), macros, camera_control (placeholder), hyperdecks, talkback (placeholder), program_preview. There is no model or version guard, and no try/except around sections: a non-`ValueError` raised by a macro encoder aborts the whole apply |
| `download_media_pool_images` | `(atem, progress_callback=None, cancel_check=None, timeout_per_slot=30.0, cache_get=None, cache_put=None) -> list[{slot, name, png_bytes, from_cache}]` | `download_still` per used slot → `atem_to_rgb` → Pillow PNG (the `[profile]` extra). Not called by `from_atem` |

**`SaveOptions`** (all default True): `mes: list[MixEffectOptions]` (×4), `downstream_keys`,
`color_generators`, `fairlight`, `camera_control`, `media_pool_metadata`, `media_pool_images`
(never read by the library), `media_players`, `auxiliaries`, `counters`, `settings` (also gates
`<Inputs>`), `video_mode`, `hyperdecks`, `macros`.

**`MixEffectOptions`** fields are `program`, `preview`, `next_transition`, `transition_style`,
`fade_to_black` and `usk: list[bool]` (×4). Classmethods `none()` and `apply_default()` (the latter
has program and preview off).

**`ApplyOptions`**:
- `mes` defaults to `apply_default()` ×4, so cuts to air are off by default.
- Default True: `restore_hyperdecks` (clears slots saved as 0.0.0.0), `restore_downstream_keys`,
  `restore_color_generators`, `restore_audio`, `restore_video_mode` (destructive), `restore_inputs`,
  `restore_macros`, `restore_aux`, `restore_media_players`, `restore_settings_flags` (ftbEnabled
  only), and `restore_media_pool_images` (never read).
- Default False: `restore_fly_keyframes`, plus `restore_multiview`, `restore_camera_control` and
  `restore_talkback`. Those last three have no implementation.
- Classmethod `macros_only()`.

**`ApplyResult`**: `applied`, `skipped` and `errors` (lists of strings), and `summary()`.

**What the XML covers.** Save writes 14 top-level sections:
- `MixEffectBlocks`: program, preview, next transition, style, mix/dip/wipe/stinger/DVE
  parameters, and per-key luma/chroma/pattern/DVE/fly parameters plus key frames A/B. Also FTB.
- `DownstreamKeys`, `ColorGenerators` (fixed at 2), `Auxiliaries`.
- `Settings`: Inputs, ftbEnabled, SDI3GOutputLevel, MultiViews, and hardcoded Talkback, MixMinus,
  ButtonMapping and Clips.
- `VideoMode`, `HyperDecks` (fixed at 10).
- `FairlightAudioMixer`: master, EQ, dynamics, inputs, sources with EQ and dynamics, and headphone
  output 0.
- `MediaPlayers`; `MediaPool` (still names and paths only, **no pixels**).
- `CameraControl` (**entirely hardcoded defaults**).
- `MacroPool` (decoded ops), `MacroControl` (loop hardcoded False), `Counters` (hardcoded).

Apply reads 10 of them. It **never applies** `MediaPool`, `CameraControl`, `MacroControl` or
`Counters`; from `Settings` it applies only Inputs and ftbEnabled. It also skips these fields:
nextSelection, nextStyle, previewTransition, transitionPosition, isFullyBlack, DVE logoRate,
borderStyle, AudioInput configuration and analog level, headphones, Input@externalPortType and the
HyperDeck autoRoll fields. Apply emits 43 distinct control opcodes (`CVdM CInL FEna CClV CAuS CTTp
CTMx CTDp CTWp CTSt CTDv CKTp CKeF CKeC CKOn CKMs CKLm CKPt SFKF CKDV CACK CACC CDsF CDsC CDsR CDsM
CDsG CDsL CDsT FtbC MPSS CFMP CMBP CMCP CMLP CFSP CEBP CICP CILP CIXP CXMS CPvI CPgI`) plus the
macro transfer. No packets are hand-built in `profile/`.

Profile enum tables (`profile/_enums.py`):
- `WIPE_PATTERN_NAMES`, `DVE_EFFECT_NAMES` (16–31 only), `USK_TYPE_NAMES`, `TRANS_STYLE_NAMES`,
  `MIX_OPTION_NAMES`, `EQ_SHAPE_NAMES` and `EQ_FREQ_RANGE_NAMES` hold the same values as §1.
- `EXT_PORT_TYPE_NAMES` is 1 SDI, 2 HDMI, 4 Component, 8 Composite, 16 SVideo.

### 3.4 Other special operations

| operation | entry point | notes |
|---|---|---|
| identity probe | `atemwire.probe(ip) -> {'video_format', 'atem_model', 'connection_status', ['error']}` | through the pool; a cold probe can take up to the 15 s connect timeout and leaves the session open for the 2 s grace period |
| HyperDeck bind planning | `hyperdeck.plan_binding_push(mx, deck_ip, switcher_input, *, exclude_slots=()) -> (slot\|None, needs_write)` | pure function; the write is `set_hyperdeck_settings` |
| full snapshot | `state.build_full_state(conn)` / `ATEM.snapshot()` | §2.6 |
| meters | `fairlight.enable_fairlight_levels`, `audio_legacy.enable_audio_levels` | opt-in streams (`FMLv`/`FDLv`, `AMLv`) at ~10 Hz; re-arm after every reconnect |
| macro record by replay | `macro_start_recording` → ops → `macro_sleep` → `macro_stop_recording` | the live way to build a macro without bytecode |

## 4. Connection lifecycle

### 4.1 The three ways in

| layer | entry point | what it gives you |
|---|---|---|
| facade | `atemwire.ATEM(ip)` (`atem.py`), context manager; `close()` is idempotent | A pooled session. Blocks up to 15 s on a cold connect (`RuntimeError` on failure); a warm connect joins at once. **Operations:** `atem.<op>(*args)` forwards to the `messages.<module>.<op>(conn, *args)` functions of 17 modules (`atem.py:72-89`). The `hyperdeck`, `lock`, `file_transfer`, `tally` and `meters` modules are not forwarded, so `set_hyperdeck_settings` is not on the facade. **Reads:** properties `connected`, `video_mode`, `product_name`, `program_source`, `preview_source`, `in_transition`, `transition_style`, `transition_rate`, `ftb_active`; methods `get_program_source(me)`, `get_preview_source(me)`, `get_in_transition(me)`, `get_transition_style(me)`, `get_transition_rate(me)`, `get_ftb_active(me)`; `snapshot()` (= `build_full_state`); `clear_still(slot, timeout=5.0)` (locked); `raw` → the `ATEMConnection` |
| pool | `atemwire.pool.acquire_connection(ip)` (context manager → `ATEMConnection`); `ATEMInstanceManager.peek_connection / get_instance / release_instance / list_instances` | One session per switcher per process, ref-counted. When the last ref goes, a 2 s grace timer disconnects. Nothing reaps a session whose ref is still held. A dead worker is evicted. `close_all_sessions_at_exit()` is registered with atexit. Single-process only |
| connection | `atemwire.connection.ATEMConnection(instance_id)` | `connect(ip) -> bool` (blocks up to `CONNECT_TIMEOUT = 15 s`), `disconnect()`, `is_connected`, `mixerstate`, `protocol`, `send(command)`, `download_still`, `download_macro`, `clear_still`, `set_on_died(cb)`, `last_run_macro_index`. It has its own worker thread `AtemConn-<id>`, which pumps `protocol.loop()` every 10 ms and sends queued commands in batches of up to 1200 bytes per packet. A command that fails to render or is over-size is **dropped with a log line**, not raised |
| bare protocol | `atemwire.protocol.AtemProtocol(ip, port=9910, *, aggressive_drain=False)` | `connect()` sends SYN without blocking. **You** pump `loop()` (one packet per call). Also `send_commands([cmds])` (≤1300 bytes, else `ValueError`), `send_raw`, `on(event, cb) -> id`, `off(event, id)`, `mixerstate`, `connected`, `initialized`, plus the transfer methods (§3). It is **not thread-safe**. **It has no close method**; teardown is `release_locks_now()` → `transport.close_session()` → `transport.sock.close()` → `transport.thread_queue.close()` (`connection.py:723-755`). Passing a `tcp://` ip or `usb=` raises `NotImplementedError` |

Readiness and identity:
- `ready.wait_ready(protocol, *, timeout=8.0, pump_interval=0.01, extra_settle=0.0, stop_event=None) -> mixerstate`
  pumps until the initial dump is complete and `video-mode` is present. It raises `TimeoutError`, or
  `WaitAborted` when `stop_event` is set. The connection uses `timeout=15`.
- `ready.wait_state_settled(atem, timeout=3.0) -> None` polls (it does not pump) until 8 indicator
  keys are present, or until the key count stops growing for 0.5 s. It returns **silently** on
  timeout.
- `probe(ip)`: see §3.4.

### 4.2 Transport (UDP, `transport.py`)

- The switcher is on UDP **9910**. The handshake is a SYN with `0x01` and session id `0x1337`; the
  switcher answers with a SYN (`0x02` = accepted) and the client ACKs it. The session id is then
  adopted from the switcher. The handshake ACK is sent from `loop()`, so it needs pumping.
- Every reliable packet is ACKed with the contiguous high-water mark, by the transport's **own
  UDP thread**. A session therefore survives an unpumped loop; events only wait.
- Duplicates are dropped. Out-of-order packets are parked (up to 512 ahead). Retransmission is
  **only on the switcher's request** (go-back-N, max 128). There is no client-side retransmit
  timer, and the switcher's ACK number is never read.
- Goodbye: `close_session()` sends a SYN with `0x04`, fire-and-forget.
- **There is no client keepalive.** `TiRq` is sent once, on ready. Silence is fine because ACKs
  flow from the UDP thread.
- **Reconnect:** after 5 s with no packets, the transport re-SYNs and delivers `None`. The protocol
  then raises `disconnected` and **empties `mixerstate`**; when the new dump completes, `connected`
  fires again. Meanwhile `ATEMConnection.is_connected` **stays True**; since `edb5fa0`,
  `is_ready` and `ATEM.connected` go False for the gap (§5.4 item 2).
- Environment: `ATEMWIRE_PACKET_TRACE=1|true|yes|on` turns on the `atemwire.trace.packet` and
  `atemwire.trace.drain` loggers. No other environment variable is read.

### 4.3 Events

Subscribe with `protocol.on(event, cb)`; for a pooled session use `conn.protocol.on`. Callbacks
run on whichever thread pumps `loop()` (the worker, for a pooled session). An exception in a
callback is logged and swallowed.

| event | args | when |
|---|---|---|
| `connected` | — | initial dump complete (the packet after `InCm`) |
| `disconnected` | — | transport timeout or forced reconnect; `mixerstate` is cleared |
| `change` | `(key, contents)` | every stored field |
| `change:<key>` | `(contents)` | bare fields |
| `change:<key>:<first-index>` and `change:<key>:*` | `(contents)` | indexed fields (Fairlight: the strip id) |
| `transfer-progress` | `(store, slot, fraction)` | still download; under-reports (compressed vs raw size); never for macros |
| `upload-progress` | `(store, slot, percent, done, total)` | upload chunks; can exceed 100 |
| `download-done` | `(store, slot, data)` | `FTDC` on a download |
| `upload-done` | `(store, slot)` | `FTDC` on an upload |
| `file-transfer-error` | `(FileTransferErrorField)` | fatal `FTDE` only |
| `clear-dispatched` | `(store, slot)` | locked clear sent (local, not a switcher confirmation) |

`LKOB`, `FTCD`, `FTDa`, `FTDE`, `FTDC` and `*XFC` produce **no** `change` events.

### 4.4 Knowing a command took effect

**Nothing reports per command.** `send()` and every op return `None`. The switcher's
transport-level ACK is not tracked, and dropped commands are only logged. The only confirmation is
the state echo: subscribe to `change:<pretty-key>[:<index>]` (e.g.
`change:program-bus-input:0` after `CPgI` on M/E 1), or poll `mixerstate` / a reader until the
value matches. Commands with no echo (§2.7) cannot be confirmed at all. Transfers are the exception:
`upload-done` and `download-done` come from the switcher's `FTDC`, and `file-transfer-error` from
`FTDE`.

### 4.5 Threads

Each pooled switcher runs:
- a worker thread (daemon);
- a UDP thread `atem-udp` (daemon);
- a grace `Timer` once the last ref is released.

The `ATEM` facade and `ATEMConnection.send` are safe from any thread. `AtemProtocol` is not, yet
`download_still`, `download_macro`, `clear_still` and `upload_macro_bytecode` call it from the
caller's thread while the worker pumps it.

### 4.6 Test doubles

`atemwire.testing` provides `FakeAtemProtocol(ip=None, *, connect_delay=0.0, loop_blocks=True, fail_connect=False, fail_loop_after=None, fail_loop_exception=None)`
and `FakeTransport`. Install them by monkeypatching `atemwire.connection.AtemProtocol` (see
`tests/conftest.py`). The fake records sends in `send_log`. It has no transfer methods and no
simulated switcher state beyond a 1080p50 `video-mode`.

## 5. Gaps

### 5.1 Known to the library, not implemented

**Codes the switcher sends that atemwire recognises but does not parse** (`protocol.py:50-78`,
`_UNMAPPED_PRETTY`). They have no `Recv` class and no write command. Their raw bytes land in
`mixerstate['<code>']` (§2.1).

| area | codes |
|---|---|
| media player clips and audio | `MPCS` mediaplayer-clip-source, `RCPS` mediaplayer-clip-status (play/loop/position), `MPAS` mediaplayer-audio-source, `MPSp` mediaplayer-space |
| HyperDeck **transport** (beyond binding) | `RXCP` hyperdeck-status, `RXSS` hyperdeck-storage, `RXCC` hyperdeck-clip-count |
| talkback and mix-minus | `ATMP` talkback-mixer-properties, `TMIP` talkback-mixer-input-properties, `MMOP` mix-minus-output-properties |
| display clock | `DCPV` displayclock-properties, `DSTV` displayclock-set-time |
| multiviewer extras | `MvVM` multiview-video-mode-capability, `StMv` safe-area type, `VuMo` VU opacity |
| streaming extras | `SAth` streaming-authentication, `SRST` streaming-time |
| misc | `CCst` camera-control-settings, `FMPP` fairlight-properties, `Powr` power-status, `TcLk` timecode-lock |
| capability descriptors | `_DVE`, `_FAC` (Fairlight audio config), `_MAC` (macro config: the slot count is inferred from `MPrp` instead), `_MvC`, `_SSC`, `_TlC` |

**State parsed, but with no write command:**
- `V3sl` (3G-SDI level), `TCCc` (timecode mode).
- `FMHP` (Fairlight headphones), `FAMS` (solo), `FAIP` (analog input level: mic/line).
- `VuMC` and `SaMw` (per-window multiviewer VU and safe-area enable).
- `KKFP` (fly keyframe geometry can only be snapshotted from the live DVE with `SFKF`).

**Commands that exist with no op wrapper:**
- `MAct` actions 3 insert-user-wait, 4 continue and 5 delete. `ACTION_DELETE` is defined on the
  class only, with no module constant.
- `CTDv.logo_rate`, `CKDV.border_bevel_enabled`, `CInL.port_type`.
- `TiRq`.

**Whole features with no code:**
- Still **upload** above the protocol layer.
- Clip upload/download, audio upload, label/thumbnail transfer.
- Macro rename and loop (no `MPrp`/`MRPr` setters).
- Classic (non-Fairlight) audio in profiles.
- SuperSource, streaming/recording, multiview, camera control and talkback in **profile apply**.
  The last three have `ApplyOptions` flags that do nothing.
- The `tcp://` proxy and USB transports. Both raise `NotImplementedError`, so the `*XFC`/`*XFR`
  handling and the TCP `connected` path are dead code.
- `CXMS` bytes 10–15 (auto-roll and frame delay) are not mapped and are sent as 0.

**The macro codec** knows 146 op codes. These families are absent and round-trip only as
`Unknown_0x…` with `rawParams`:
- classic audio, SuperSource, multiviewer, camera control, talkback, streaming/recording;
- HyperDeck transport, media-player clips and playback;
- pattern and non-advanced chroma key parameters;
- DVE rotation, bevel and light source;
- wipe symmetry, pattern softness;
- DSK invert and mask edges;
- Fairlight pan, delay, strip EQ enable and master dynamics;
- macro-runs-macro and user-wait ops.

### 5.2 Implemented, but not exposed (or exposed in a way that breaks)

- **21 `Send` classes are not re-exported from `atemwire.messages`.** They are reachable only by
  module path: `AudioInputCommand`, `AudioMasterPropertiesCommand`,
  `AudioMonitorPropertiesCommand`, `SendAudioLevelsCommand`, `CameraControlCommand`,
  `HyperdeckSettingsCommand`, `MultiviewInputCommand`, `MultiviewPropertiesCommand`,
  `RecordingSettingsSetCommand`, `RecorderStatusCommand`, `StreamingServiceSetCommand`,
  `StreamingAudioBitrateCommand`, `StreamingStatusSetCommand`, `SupersourceBoxPropertiesCommand`,
  `SupersourcePropertiesCommand`, `TransitionPreviewCommand`, `TransitionPositionCommand`,
  `AutoInputVideoModeCommand`, `SaveStartupStateCommand`, `ClearStartupStateCommand`,
  `SetTimeOfDayCommand`. `DeviceIdentityField` (`WhoI`) is the one unexported `Recv`.
- **No operations are re-exported** from `atemwire.messages`; all 180 live at
  `atemwire.messages.<module>.<op>`.
- **The `ATEM` facade forwards 419 names, but only 179 are working operations.** The other 86 are
  `mx`-first readers and 154 are `Send`/`Recv` classes. `__getattr__` calls each with the
  `ATEMConnection` as its first argument, so `atem.macro_entry(0)`, `atem.usk_dve(...)` and similar
  raise. Pass `atem.raw.mixerstate` to readers instead. Seven names are shadowed by real facade
  members: `program_source`, `preview_source`, `ftb_active`, `clear_still`, `transition_style`,
  `product_name`, `video_mode`. **The `hyperdeck` module is not forwarded at all.**
- **Transfers without facade entry points.** The facade has no still or macro transfer; use
  `atem.raw.download_still`, `atem.raw.download_macro` and
  `upload_macro_bytecode(atem.raw.protocol, …)`. The `ATEMConnection` has no upload.
- **`atemwire.macrotransfer` stability is unstated.** It is public in practice, but the
  `atemwire/__init__.py` docstring does not list it among the layers covered by the version
  number.
- **`build_full_state` omits whole areas.** It covers no multiviewer, SuperSource, recording,
  streaming, tally, camera control, legacy audio, media-pool slots, media-player selection, locks,
  time or meters, and lists **only the first 12 macros**.
- **Unused constants.** `FTSD`'s `MODE_WRITE_RLE`, `MODE_WRITE` and `MODE_ERASE` are unused (the
  engine uses literals). `profile/_enums._source_symbol()` is dead code.
- **`SaveOptions.media_pool_images` and `ApplyOptions.restore_media_pool_images` are never read.**

### 5.3 Docs and comments that say something the code doesn't do

- `protocol.py:44-49, 85-89` say unparsed codes are stored under their pretty name; they are
  stored under the raw 4-char code.
- **Stale references:** comments in `state.py`, `messages/_dsl.py`, `messages/macros.py`,
  `messages/upstream_keyer.py`, `macrotransfer/fairlight.py`, `profile/__init__.py`,
  `profile/_common.py` and `docs/MACRO_FORMAT.md` cite `atemwire.operations` / `operations.py`.
  `MACRO_FORMAT.md` also cites `atemwire.rawtransfer` and `atemwire/macrotransfer.py`. None of
  these exist.
- `messages/transition.py:16` lists `TsPr`; the class code is `TrPr`.
- `set_fairlight_eq_band` says `frequency_range` takes "0..3"; the wire values are the bitfield
  1/2/4/8.
- `system_info` calls `TiRq` a keepalive; it is sent once.
- `probe.py` / `__init__.py` say a cold connect costs ~6 s; `CONNECT_TIMEOUT` is 15 s.
- `connection.py:726-734` says an abandoned lock lasts ~5 min; `MEDIA_LOCK.md` measured ~5 s.
- `_io.py:123` lists an inbound `FTUA` in the upload flow; none is waited for or parsed.
- The `upstream_keyer` DVE comment calls `rotation` "u16"; the field is i32.
- **`docs/MACRO_FORMAT.md`:**
  - it says "143 op-ids" while the table lists 44 codes; the code has 146;
  - it says "Fairlight not yet decoded"; 35 Fairlight ops exist;
  - its marker-byte values and "byte-identical round-trip" claims are wrong;
  - it describes the empty-macro MSRc fallback; empty bytecode is uploaded instead;
  - it cites resolver and symmetry functions that don't exist.
- **`docs/PROFILE_FORMAT.md` / `PROFILE_SAVE_RESTORE.md`:**
  - **They describe as gaps things that are implemented:** compressor/limiter/expander, HyperDecks,
    fly keyframes, DSK tie and CACC sampled colour. They also say multiview and camera-control
    Send classes don't exist; they do, but profile doesn't use them.
  - **They claim values are read live that are actually hardcoded:** nextStyle, nextSelection,
    previewTransition, borderStyle, MacroControl loop, CameraControl, Counters, Talkback, MixMinus,
    ButtonMapping.
  - **They describe options that don't exist:** `restore_program`, `restore_preview`, `restore_usk`.
  - **Import and encoder errors:** `from atemwire import SaveOptions` (an `ImportError`), and the
    claim that the encoder resolves post-rename input names (it is static).

### 5.4 Behaviour a CLI will trip over (verified in code)

Items 1, 2 and 4 were fixed after 1.2.0, in the commits named in each; they still apply to
1.2.0 and earlier, including the 1.1.0 that WebATEM pins.

1. **Profile video mode does not round-trip at fractional rates** (1.2.0). Save writes
   `get_label()` (`1080p59.94`); apply only knows `1080p5994`-style names, so it skips the mode as
   unrecognised. Both SD aspects save as the same string (`625i50`), and `525i59.94` has no entry
   (`profile/save.py:516-522`). *Fixed in `e9e4a77`:* save and apply share
   `input_video.VIDEO_MODE_XML_NAMES` (all 30 modes, SD 16:9 as `NTSC_widescreen` /
   `PAL_widescreen`), and apply compares mode numbers. Old decimal spellings are still read.
   The two old SD labels never recorded the aspect (`1a7e366`): `525i59.94` is skipped with a
   warning, and `625i50`, which is also ASC's name for PAL 4:3, applies as 4:3 with a warning.
2. **Transport reconnect leaves `is_connected == True` while `mixerstate == {}`** (1.2.0). A warm
   `ATEM(ip)` can join mid-outage; check `mixerstate` or wait for `connected`. *Fixed in
   `edb5fa0`:* `is_connected` keeps its meaning (worker alive; `send()` and pool eviction use it).
   The new `ATEMConnection.is_ready` is False from `disconnected` until the next `connected`,
   `ATEM.connected` reads it, and `probe()` reports `connection_status=False` during the gap.
3. **An unknown `VidM` mode code raises inside the parser.** The exception escapes `loop()`, the
   rest of that packet is lost, `video-mode` never lands, and `wait_ready` times out.
4. **Slot 0 is never latched as the last-run macro** (1.2.0). `last_run_macro_index` uses
   `int(index) or 0xFFFF` (`connection.py:481`). *Fixed in `a9f3646`:* only a missing index or
   0xFFFF means idle.
5. **`FEna` disable cannot address M/E 2+.** "Disable ME2" equals "enable ME1" on the wire, so
   `set_ftb_disabled` always targets ME1. `FEna` state is a byte heuristic.
6. **Several state keys are stored bare, so the last packet wins:** `FMLv` (per-strip meters),
   `RTMD` (per-disk). Listen to `change:` events instead of reading `mixerstate`.
   **`MPfe`** is keyed by slot index with the type byte skipped (`KEY_FORMAT '>xxH'`); if a
   switcher reports clip slots through `MPfe`, they would overwrite stills with the same index
   (not verified on hardware).
7. **`FASD` (strip deleted) is stored and never acted on**, so deleted strips stay in
   `fairlight-strip-properties`.
8. **Profile apply problems:**
   - It sends both halves of a split-mono strip with channel −1, so the second overwrites the
     first. Apply can't simply be pointed at the right channel. Save writes the same
     `AudioSource id` for both halves (a per-input sentinel: −256 if `FAIP` says mono, else
     −65280) and records no subchannel, so only element order tells them apart. The ASC reference
     export has no split input, so how ASC marks the second half is unknown.
   - It clears every HyperDeck slot saved as 0.0.0.0 by default.
   - `externalPortType` is saved from `InPr.source_ports`, not `external_port_type`.
   - A non-`ValueError` from a macro op encoder (a bad IP gives `OSError`, an out-of-range value
     `struct.error`) aborts the whole apply.
   - It has no model or version guard.
9. **Macro codec losses:**
   - Marker bytes are rewritten.
   - Unknown transition style, key type and mix type strings silently encode as Mix, Luma and On.
   - TransitionSource mask 0 becomes Background.
   - u16/u32 values are masked silently (70000 → 4464).
   - Fly-key `MiddleCentre` raises, despite a comment saying it is accepted.
   - A missing `MacroSleep.frames` encodes as 0.
10. **Unit traps:**
    - **Unclamped values:** stinger clip/gain (×10). CACK values are ×1000 and unclamped.
    - **`CICP`/`CILP`/`CIXP`/`CMCP`/`CMLP` take dB/ms floats**, while `CFMP`/`CFSP`/`CEBP` take
      ×100 ints.
    - The DVE border hue/sat/luma write is ×10, with an asymmetric read (the "SCALE CONTRACT"
      comment).
    - `set_stinger_source` takes an MP slot (1–4), not a source ID.
    - `set_aux_output` is 0-based, but state keys are `aux1…`.
    - `execute_macro` is 1-based, `macro_run` 0-based.
    - `set_keyer_fly_keyframe` accepts `'full'` and `'runToInfinite'`, which `SFKF` documents as
      A/B only.
    - The infinite-run direction enum differs between the op docstring (1–9, with 5 the centre)
      and the macro table (0 CentreOfKey, no 5). They agree on 1–4 and 6–9. ASC's own export (the
      Lots XML) uses both `CentreOfKey` and `MiddleCentre` in one macro, so they are different
      locations. No capture in the repo shows the centre byte; both sites carry a TODO
      (`f960428`).
11. **Bare clear is unreliable.** The `CSTL` sent by `media.clear_still` is ignored by some models;
    use the locked `clear_still`.
12. **Upload progress can exceed 100%**, and download progress under-reports.
    `download_macro_bytecode(progress_callback=…)` never calls back.
13. **`upload_macro_bytecode` failure modes:**
    - It does not abort the lane on timeout.
    - Its error handler doesn't filter by store.
    - On a bare protocol it needs a separate pump.
14. **Unverified writes.** Every "restored upstream" class (20 of the 82) and `CKMs` ("not
    Wireshark-validated") have byte-pinned layouts but no hardware verification in this fork.

## Counts

Measured by importing the package, not tallied by hand.

| what | count |
|---|---|
| **Commands: `Send` classes (= distinct outgoing opcodes)** | **82** (74 control, 7 lock/file-transfer, 1 `TiRq`) |
| … by area | upstream keyer 12, fairlight 10, downstream keyer 8, transition 8, switching (M/E + aux) 5, system 5, file-transfer 5, legacy audio 4, FTB 3, macro 3, media 3, streaming 3, input/video 2, lock 2, multiviewer 2, recording 2, supersource 2, camera control 1, color gen 1, hyperdeck 1 |
| … not re-exported from `atemwire.messages` | 21 (20 of them "restored upstream", unverified on hardware) |
| Operation functions `op(conn, …)` | 180 |
| **State: `Recv` classes (= distinct incoming opcodes parsed)** | **92** (4 codes, `AiVM`, `FEna`, `FTDa` and `STAB`, are both a command and a state packet) |
| … parsed state attributes across them | 526 (public, index attributes included) |
| … known-but-unparsed codes stored raw | 27 |
| Reader functions `reader(mx, …)` | 88 |
| **Transfer and special operations** | **10**: 5 multi-packet wire flows (still download, still upload, locked still clear, macro download, macro upload); 3 composite flows built on them (profile save, profile apply, media-pool PNG export); 2 offline codecs (macro bytecode, image/RLE conversion) |
| … macro bytecode op codes (encode + decode) | 146 |
| … profile: sections saved / applied; control opcodes apply emits | 14 / 10; 43 |
