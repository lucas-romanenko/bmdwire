# ATEM Switcher Profile XML Format — v2.1

This is a reverse-engineered reference for the `<Profile>` XML emitted by
**Blackmagic ATEM Software Control**'s "Save Switcher State" command.
Blackmagic does not publish a schema. The mapping here was reconstructed by
diffing one complete export from a production switcher against the
on-the-wire packets that atemwire already understands.

**Scope of this document.** Documented strictly against one reference file:

| Field      | Value                            |
|------------|----------------------------------|
| Path       | `everything_2026-04-27_18-16-51.xml` |
| Switcher   | ATEM 1 M/E Constellation HD      |
| Format     | `<Profile majorVersion="2" minorVersion="1">` |

Other ATEM models almost certainly emit a strict superset (and may
differ on per-element attribute counts — multi-ME boxes, ATEMs with
SuperSource, ATEMs with more aux/USK/DSK, ATEMs with additional
Fairlight strips). This doc is honest about that and aims to be a
useful jumping-off point rather than a closed schema.

The format is created **client-side** by ATEM Software Control. The
switcher itself has no concept of XML — Software Control receives
state-dump packets over the wire (the same ones atemwire parses) and
serializes them. To restore a saved profile, software walks the XML and
issues the corresponding wire commands. That's the model `atemwire.profile`
follows.

## Conventions

- Boolean attributes use the strings `"True"` / `"False"` (Pythonic
  capitalization). Numeric `1` / `0` is **not** used.
- Floats are emitted in decimal notation with up to ~6 fractional
  digits, **without** trailing-zero stripping in some places (so
  `gain="0"` and `gain="-0.08"` and `clip="0.300003"` all coexist —
  the precision matches whatever Software Control had internally).
- Special float values seen: `faderGain="-inf"` (literal `-inf`).
- Element ordering inside the file is significant for matching
  Software Control's output but the parser doesn't care.
- Indentation is four spaces; self-closing tags use `<X/>` (no space
  before the slash).
- Source references mix two styles depending on context:
  - **Bus-style** (declarative state): numeric ATEM source IDs as
    decimal strings, e.g. `input="3010"` (= MediaPlayer1 fill).
  - **Symbolic** (only inside `<Macro><Op>` elements): the ATEM
    Software Control display name, e.g. `input="MediaPlayer1"`,
    `input="Camera3"`, `input="Black"`, `input="ExternalTRS"`.
  See the [Source ID reference](#source-id-reference) below.

---

## Top-level structure

```
<Profile majorVersion="2" minorVersion="1" product="...">
  <MixEffectBlocks>
  <DownstreamKeys>
  <ColorGenerators>
  <Auxiliaries>
  <Settings ...>
  <VideoMode>
  <HyperDecks>
  <FairlightAudioMixer>
  <MediaPlayers>
  <MediaPool>
  <CameraControl>
  <MacroPool>
  <MacroControl>
  <Counters>
</Profile>
```

The `product` attribute is the human-readable model name and matches
what `atemwire.state.product_name(mx)` returns.

| Attribute      | Type   | Source                                  |
|----------------|--------|------------------------------------------|
| `majorVersion` | int    | hardcoded `"2"`                          |
| `minorVersion` | int    | hardcoded `"1"`                          |
| `product`      | str    | `mixerstate['product-name'].name`        |

---

## MixEffectBlocks

```
<MixEffectBlocks>
  <MixEffectBlock index="0">
    <Program input="3010"/>
    <Preview input="1"/>
    <NextTransition selection="Background" nextSelection="Background"/>
    <TransitionStyle style="Mix" nextStyle="Mix" previewTransition="False" transitionPosition="0">
      <MixParameters rate="25"/>
      <DipParameters rate="25" input="2001"/>
      <WipeParameters .../>
      <StingerParameters .../>
      <DVEParameters .../>
    </TransitionStyle>
    <Keys>
      <Key index="0..3" type="Luma|Chroma|Pattern|DVE" .../>
    </Keys>
    <FadeToBlack rate="25" isFullyBlack="False"/>
  </MixEffectBlock>
</MixEffectBlocks>
```

The Constellation HD is a 1 M/E switcher. Multi-ME models emit one
`<MixEffectBlock>` per ME unit.

### Program / Preview

| Attribute | Source                                 | Wire op                       |
|-----------|----------------------------------------|--------------------------------|
| `input`   | `state.program_source(mx, me)`          | `operations.set_program`       |
| `input`   | `state.preview_source(mx, me)`          | `operations.set_preview`       |

### NextTransition

The next-transition selection is a 5-bit mask: background + 4 keyers.
The XML splits it into two values: `selection` (currently active) and
`nextSelection` (will apply after the in-flight transition ends).

| Attribute       | Source                                       |
|-----------------|----------------------------------------------|
| `selection`     | `state.transition_selection(mx)` — packed name |
| `nextSelection` | (same packing rule, applied to next-style state) |

Packing rule for `selection` strings:
- `"Background"` — background-only
- `"Key1"` / `"Key2"` / ... / `"Key4"` — single key
- `"Background,Key1"` etc. — comma-joined when multiple bits set

Setters: `operations.toggle_transition_background`,
`operations.toggle_transition_key(key=N)` (these are toggles, so
restoring the exact selection requires reading current state first).

### TransitionStyle

| Attribute            | Type                                               | Source / Op                  |
|----------------------|----------------------------------------------------|------------------------------|
| `style`              | `Mix\|Dip\|Wipe\|Stinger\|DVE`                     | `state.transition_style` / `set_transition_style` |
| `nextStyle`          | same enum (next-style after transition)            | reads `transition-settings.style_next` |
| `previewTransition`  | bool — preview-transition button held down         | `mixerstate['transition-preview']` |
| `transitionPosition` | int — 0..10000 t-bar position                     | `state.transition_position * 10000` |

#### MixParameters / DipParameters / WipeParameters / StingerParameters / DVEParameters

These are **always all five emitted**, even for unused styles — the
saved values are whatever was last configured for that style.

| Element             | Wire field           | Setter ops              |
|---------------------|----------------------|--------------------------|
| `MixParameters`     | `transition-mix`     | `set_mix_rate`           |
| `DipParameters`     | `transition-dip`     | `set_dip_rate`, `set_dip_source` |
| `WipeParameters`    | `transition-wipe`    | `set_wipe_*`             |
| `StingerParameters` | `transition-stinger` | `set_stinger_*`          |
| `DVEParameters`     | `transition-dve`     | `set_dve_*`              |

Notes on units:
- `rate` is in **frames** (matches wire). The `_resolve_rate` helper in
  operations converts seconds:frames to frames; restore can pass
  `f"{frames}"` directly to the rate setters or convert via display fps.
- `clip` / `gain` / `symmetry` / `softness` are **0..100 percent**
  (matches the operations layer's `_percent_to_thousandths` input).
- `xPosition` / `yPosition` for wipe are **0..1 unit scalars**.
- `pattern`, `effect` are stringified enum values (see [enums](#enum-tables)).
- Stinger `mixRate` is the rate in frames for the stinger mix transition.
- Stinger `clipDuration` and `triggerPoint` are in frames.

### Keys

```
<Key index="0..3" type="Luma|Chroma|Pattern|DVE" inputCut="..." inputFill="..."
     onAir="False" masked="False" maskTop="..." maskBottom="..." maskLeft="..." maskRight="...">
  <LumaParameters preMultiplied="..." clip="..." gain="..." inverse="..."/>
  <AdvancedChromaParameters .../>
  <PatternParameters .../>
  <DVEParameters .../>
  <FlyParameters enabled="..." xPosition="..." yPosition="..." xSize="..." ySize="..." rotation="..." rate="...">
    <KeyFrameA .../>     <!-- optional, only when at least one keyframe is stored -->
  </FlyParameters>
</Key>
```

| Attribute / element | Source / Op |
|---------------------|-------------|
| `index`             | 0..3 (USK index on this ME) |
| `type`              | `state.usk_type` enum int → `Luma`/`Chroma`/`Pattern`/`DVE` strings; `set_usk_type` |
| `inputCut`          | `state.usk_key_source` / `set_usk_key_source` |
| `inputFill`         | `state.usk_fill_source` / `set_usk_fill_source` |
| `onAir`             | `state.usk_on_air` / `set_usk_on_air` |
| `masked`, `maskTop/Bottom/Left/Right` | `state.usk_mask` / `set_usk_mask_*` |
| `<LumaParameters>`  | `state.usk_luma` / `set_usk_luma_*` (`inverse` ↔ `invert`) |
| `<AdvancedChromaParameters>` | `state.usk_chroma` / `set_usk_chroma_*` (see chroma table) |
| `<PatternParameters>` | `state.usk_pattern` / `set_usk_pattern_*` |
| `<DVEParameters>`   | `state.usk_dve` / `set_usk_dve_*` |
| `<FlyParameters>`   | `state.usk_fly` / `set_usk_fly_enabled` + per-keyframe ops |

Mask attribute units: `maskTop/Bottom` are signed floats `-9..+9`,
`maskLeft/Right` are `-16..+16`. Maps to ops via `set_usk_mask_*` (×1000
to wire). Same convention applies to DVE-mask (different ranges).

#### LumaParameters

| Attr             | Source              | Op                              |
|------------------|---------------------|----------------------------------|
| `preMultiplied`  | `luma_pre_multiplied` | `set_usk_luma_pre_multiplied`  |
| `clip`           | `luma_clip` (0..100)  | `set_usk_luma_clip`            |
| `gain`           | `luma_gain` (0..100)  | `set_usk_luma_gain`            |
| `inverse`        | `luma_invert`         | `set_usk_luma_invert`          |

#### AdvancedChromaParameters

> **Gap.** atemwire stores `key-properties-advanced-chroma` and
> `key-properties-advanced-chroma-colorpicker` **bare** — not indexed by
> ME/keyer (see `state.usk_chroma`'s LIMITATION block). The XML
> serializes the same value for every keyer because the wire only ever
> kept the last received packet for each. This isn't a bug in the
> profile module; it's a known upstream limitation. For
> Constellation HD this rarely matters in practice (only the keyer
> currently displaying chroma "wins"), but cross-keyer round-trip is
> NOT byte-perfect from a clean reread.

| XML attr             | atemwire field (`KeyPropertiesAdvancedChromaField`) | Unit | Op |
|----------------------|-----------------------------------------------------|------|-----|
| `foregroundLevel`    | `foreground` (×100 → percent)                       | 0..100 | `set_usk_chroma_foreground` |
| `backgroundLevel`    | `background`                                        | 0..100 | `set_usk_chroma_background` |
| `keyEdge`            | `key_edge`                                          | 0..100 | `set_usk_chroma_key_edge`   |
| `spillSuppress`      | `spill_suppress`                                    | 0..100 | `set_usk_chroma_spill`      |
| `flareSuppress`      | `flare_suppress`                                    | 0..100 | `set_usk_chroma_flare_suppression` |
| `brightness`         | `brightness/10` (signed, ±100)                      | float  | `set_usk_chroma_brightness` |
| `contrast`           | `contrast/10`                                       | float  | `set_usk_chroma_contrast`   |
| `saturation`         | `saturation/1000` (× 100% display)                  | 0..2 (1.0 = neutral, but XML reports as percent ×1) | `set_usk_chroma_saturation` |
| `red` / `green` / `blue` | per-channel ×10                                  | float  | `set_usk_chroma_red/green/blue` |
| `cursorXPosition` / `cursorYPosition` | colorpicker `x`,`y` (signed, ±16/9) | float | `set_usk_chroma_sample_position` (uses unit-normalized 0..1) |
| `cursorSize`         | colorpicker `size`                                   | 0..100 | `set_usk_chroma_sample_size` |
| `sampledY` / `sampledCb` / `sampledCr` | colorpicker sampled YCbCr (read-only) | 0..1   | (no op — display-only) |

#### PatternParameters

| Attr        | Source                | Op                            |
|-------------|-----------------------|--------------------------------|
| `style`     | `pattern_style` enum  | `set_usk_pattern_style`        |
| `inverse`   | `pattern_invert`       | `set_usk_pattern_invert`       |
| `size`      | `pattern_size` (0..100) | `set_usk_pattern_size`        |
| `symmetry`  | `pattern_symmetry`    | `set_usk_pattern_symmetry`     |
| `softness`  | `pattern_softness`    | `set_usk_pattern_softness`     |
| `xPosition` / `yPosition` | `pattern_position_x/y` (0..1) | `set_usk_pattern_position_x/y` |

#### DVEParameters (USK)

| Attr                   | Source / Op |
|------------------------|-------------|
| `maskEnabled`, `maskTop/Bottom/Left/Right` | `dve_masked`, `dve_top/bottom/left/right` (signed, ±9 / ±16); `set_usk_dve_masked`, `set_usk_dve_top/bottom/left/right` |
| `shadowEnabled`        | `dve_shadow` / `set_usk_dve_shadow` |
| `lightSourceDirection` | `dve_light_direction` (degrees) / `set_usk_dve_light_direction` |
| `lightSourceAltitude`  | `dve_light_altitude` (0..100) / `set_usk_dve_light_altitude` |
| `borderEnabled`        | `dve_border_enabled` / `set_usk_dve_border_enabled` |
| `borderStyle`          | bevel-type enum; `dve_border_bevel_type` (no current setter — gap) |
| `borderBevelHue`       | `dve_border_hue` / `set_usk_dve_border_hue` |
| `borderBevelSaturation`| `dve_border_saturation` / `set_usk_dve_border_saturation` |
| `borderBevelLuma`      | `dve_border_luma` / `set_usk_dve_border_luma` |
| `borderWidthOut/In`    | `dve_border_outer_width/inner_width` / `set_usk_dve_border_outer/inner_width` |
| `borderSoftnessOut/In` | `dve_border_outer_softness/inner_softness` |
| `borderBevelOpacity`   | `dve_border_opacity` |
| `borderBevelPosition`  | `dve_border_bevel_position` |
| `borderBevelSoftness`  | `dve_border_bevel_softness` |

#### FlyParameters / KeyFrameA

The KeyFrameA element appears only when the keyer has a stored
keyframe. The full set (`KeyFrameA`, optionally `KeyFrameB`) records
the saved DVE state for flight animation.

> **Gap.** atemwire can read `key-properties-fly` (a/b stored bits) but
> doesn't currently expose the per-keyframe parameters, nor does it
> have a "set keyframe data without flying to it" op. Restore of fly
> keyframes is **out of scope** in this session.

### FadeToBlack

| Attribute     | Source / Op                                     |
|---------------|--------------------------------------------------|
| `rate`        | `state.ftb_rate` (frames) / `set_ftb_rate`       |
| `isFullyBlack`| `state.ftb_active` (read-only — no direct setter) |

---

## DownstreamKeys

```
<DownstreamKey index="0" fillSource="..." keySource="..." rate="..."
               maskEnabled="..." maskTop="..." maskBottom="..." maskLeft="..." maskRight="..."
               preMultipliedKey="..." clip="..." gain="..." invert="..."
               onAir="False" tie="False"/>
```

The Constellation HD has 1 DSK. All fields map cleanly:

| Attribute          | Source           | Op                          |
|--------------------|------------------|------------------------------|
| `fillSource`       | `dsk_state.fill_source` | `set_dsk_fill_source`  |
| `keySource`        | `dsk_state.key_source`  | `set_dsk_key_source`   |
| `rate`             | `dsk_state.rate`         | `set_dsk_rate`         |
| `maskEnabled` etc. | `dsk_state.mask_*`       | `set_dsk_mask_*`       |
| `preMultipliedKey` | `dsk_state.pre_multiplied` | `set_dsk_pre_multiplied` |
| `clip` / `gain`    | `dsk_state.clip/gain` (0..100) | `set_dsk_clip` / `set_dsk_gain` |
| `invert`           | `dsk_state.invert_key`   | `set_dsk_invert_key`   |
| `onAir`            | `dsk_state.on_air`       | `set_dsk_on_air`       |
| `tie`              | `dsk_state.tie`           | (no current op — gap)  |

---

## ColorGenerators

```
<ColorGenerator index="0..1" hue="..." saturation="..." luma="..."/>
```

| Attribute    | Source                  | Op                                    |
|--------------|-------------------------|----------------------------------------|
| `hue`        | `color_generator.hue`   | `set_color_generator_hue` (degrees, 0..359.9) |
| `saturation` | `color_generator.saturation` | `set_color_generator_saturation` (0..100) |
| `luma`       | `color_generator.luma`   | `set_color_generator_luma` (0..100) |

---

## Auxiliaries

```
<Auxiliary id="8001..8006" input="..."/>
```

The `id` is the ATEM source ID **of the aux output itself** (8001 =
Aux1, 8002 = Aux2, ...). The aux index passed to
`operations.set_aux_output(aux=N, source=...)` is `id - 8001`.

| Attribute | Source                           | Op                |
|-----------|-----------------------------------|--------------------|
| `id`      | `8001 + aux_index`                | (n/a — XML key)    |
| `input`   | `state.aux_source(mx, aux_idx)`   | `set_aux_output`   |

---

## Settings

```
<Settings abDirect="False" cameraAux="-1" SDI3GOutputLevel="LevelB" ftbEnabled="True">
  <MultiViewVideoModes>
  <Talkback>
  <EngineeringTalkback>
  <Inputs>
  <MediaPool>
  <MixMinusOutputs>
  <MultiViews>
  <ButtonMapping>
  <UpstreamKeys>
</Settings>
```

| Attribute           | Source                  | Op             |
|---------------------|-------------------------|----------------|
| `abDirect`          | (no atemwire field — gap) | gap            |
| `cameraAux`         | (no atemwire field — gap) | gap            |
| `SDI3GOutputLevel`  | `mixerstate['sdi-3g-level']` (LevelA/LevelB) | gap |
| `ftbEnabled`        | `not state.ftb_disabled(mx)` | `set_ftb_disabled` (inverted) |

### MultiViewVideoModes

Static lookup table mapping the switcher's core video modes to their
multiview output modes. **Hardware capability advertisement** — emitted
verbatim by Software Control. Not currently exposed through atemwire and
not user-modifiable. Round-trip preserves it; restore ignores it.

### Talkback / EngineeringTalkback

> **Gap.** atemwire doesn't expose talkback state. Round-trip preserves;
> restore ignores.

### Inputs

```
<Input id="..." shortName="..." longName="..." externalPortType="SDI"/>
```

Lists the **external** inputs (port_type=0 in `InputPropertiesField`).
Internal sources (color generators, media players, ME outputs, etc.)
are not enumerated here — they appear by their numeric IDs in
`<Auxiliary>`, `<MultiView>` etc.

| Attribute          | Source                                         | Op |
|--------------------|------------------------------------------------|-----|
| `id`               | `InputPropertiesField.index`                   | (key) |
| `shortName`        | `InputPropertiesField.short_name`              | `operations.set_input_label(short_name=...)` |
| `longName`         | `InputPropertiesField.name`                    | `operations.set_input_label(long_name=...)` |
| `externalPortType` | enum from `external_port_type` bitfield: `SDI` (1), `HDMI` (2), `Component` (4), `Composite` (8), `SVideo` (16) | gap (port-type write supported by the same wire command but not currently exposed via the operations layer; in practice external port type is hardware-fixed) |

**Implemented as of 2026-04-28.** `Profile.apply` walks every `<Input>`
element and issues one `CInL` packet per source. Setting both labels
in one packet is the wire-native form (single mask byte, both fields
in one structure).

**Quirk: mixerstate doesn't refresh InPr after a self-issued CInL.**
The ATEM accepts the rename and persists it, but does not re-broadcast
`InPr` to the client that issued the change. Reading
`mixerstate['input-properties'][src].name` on the same connection
will show the OLD value until the connection is dropped + re-opened
(a fresh handshake re-downloads the renamed labels). Verification
flows that check post-rename state must reconnect first.

### MediaPool

```
<MediaPool>
  <Clips>
    <Clip index="0..1" maxFrameCount="100"/>
  </Clips>
</MediaPool>
```

> **Gap.** atemwire doesn't currently parse the clip-pool capacity
> packet. The reference always shows `maxFrameCount="100"` for both
> clips, suggesting it's the (post-allocation) frame budget per clip.
> Round-trip preserves; restore ignores.

### MixMinusOutputs

```
<MixMinusOutput index="0..5" audioMode="ProgramOut|MixMinus"/>
```

> **Gap.** atemwire doesn't expose mix-minus mode per output. Preserve / ignore.

### MultiViews / Windows

```
<MultiView index="0" LayoutID="12" vuMeterOpacity="1">
  <Windows>
    <Window index="0..15" input="..." vuMeterEnabled="False" safeAreaEnabled="False"/>
  </Windows>
</MultiView>
```

| Attribute       | Source                                         | Op (gap)  |
|-----------------|------------------------------------------------|-----------|
| `LayoutID`      | `mixerstate['multiviewer-properties'].layout`  | gap (no Send class yet) |
| `vuMeterOpacity`| (no atemwire field — gap)                        | gap |
| `Window.input`  | `mixerstate['multiviewer-input'][mv][win].source` | gap (no Send class yet) |
| `Window.vuMeterEnabled` | `mixerstate['multiviewer-vu'][mv][win].enabled` | gap |
| `Window.safeAreaEnabled`| `mixerstate['multiviewer-safe-area'][mv][win].enabled` | gap |

Constellation HD has 1 multiviewer with 16 windows. Save reads from
mixerstate; restore is a documented gap.

### ButtonMapping

```
<Button index="0..9" externalInputIndex="0..9" mappedToCamera="True" mappedCameraModelName="Blackmagic Generic"/>
```

> **Gap.** Per-button camera-control mapping for the front-panel
> buttons. Software Control reads from local config, not from the
> switcher. Round-trip preserves; restore ignores.

### UpstreamKeys

```
<UpstreamKeys sizeLink="True"/>
```

> **Gap.** Single property — DVE-keyer "lock X/Y size together" UI
> hint in Software Control. Not stored on the switcher.

---

## VideoMode

```
<VideoMode videoMode="1080p60"/>
```

| Attribute   | Source                              | Op |
|-------------|-------------------------------------|-----|
| `videoMode` | `state.video_mode(mx)['format']`     | `operations.set_video_mode(mode='1080p60')` |

**Implemented as of 2026-04-28.** Mode names are mapped via
`operations.VIDEO_MODE_NAMES` (covers all 30 entries from
`VideoModeField`'s receive table). The XML elides the decimal point
in fractional rates (29.97 → `2997`, 59.94 → `5994`).

**Quirk: changing video mode is destructive.** The ATEM resyncs all
outputs; video drops briefly and clients may be force-disconnected.
`Profile.apply` handles this:
- Reads the current mode first; if equal to the target, skips with
  `'already at <mode>'`.
- Otherwise logs a warning (`Profile.apply changing video mode X → Y.
  Outputs will drop briefly while the ATEM resyncs.`) before sending.
- Sleeps 3 s after the command so subsequent sections in the same
  apply don't race the resync.

Direct callers (`atem.set_video_mode`) get the warning's promise but
not the wait — implement your own settle if you need it.

---

## HyperDecks

```
<HyperDeck id="0..9" networkAddress="0.0.0.0" input="0" autoRoll="False" autoRollFrameDelay="0"/>
```

> **Saved and restored** (2026-06-09). `networkAddress` + `input` round-trip
> via `RXMS` (read) and `CXMS` (write, RE'd from a Software Control capture).
> `autoRoll` / `autoRollFrameDelay` are not yet mapped on the wire — they are
> emitted at the reference default and not restored yet.

---

## FairlightAudioMixer

```
<FairlightAudioMixer masterOutFaderGain="-0.08" followFadeToBlack="False"
                     audioFollowVideoCrossfadeTransition="False">
  <MasterOutEqualizer enabled="True" gain="0">
    <EqualizerBand index="0..5" enabled="..." shape="..." frequencyRange="..."
                   frequency="..." gain="..." qFactor="..."/>
  </MasterOutEqualizer>
  <MasterOutDynamicsProcessor makeupGain="0">
    <Compressor enabled="..." threshold="..." ratio="..." attack="..." hold="..." release="..."/>
    <Limiter enabled="..." threshold="..." attack="..." hold="..." release="..."/>
  </MasterOutDynamicsProcessor>
  <AudioInputs>
    <AudioInput configuration="Stereo|Mono" id="...">
      <Analog level="ProLine|Mic"/>     <!-- only on inputs that have analog config -->
      <AudioSource id="-65280" inputGain="..." pan="..." faderGain="..." mixOption="..." delayFrames="...">
        <Equalizer enabled="..." gain="...">
          <EqualizerBand .../>
        </Equalizer>
        <DynamicsProcessor makeupGain="...">
          <Expander enabled="..." gateMode="..." threshold="..." range="..." ratio="..." attack="..." hold="..." release="..."/>
          <Compressor .../>
          <Limiter .../>
        </DynamicsProcessor>
      </AudioSource>
    </AudioInput>
  </AudioInputs>
  <AudioHeadphoneOutputs>
    <AudioHeadphoneOutput index="0" gain="..." masterOutGain="..." masterOutMute="True"
                          talkbackGain="..." talkbackMute="True" sidetoneGain="..."/>
  </AudioHeadphoneOutputs>
</FairlightAudioMixer>
```

### Master out

| Attribute              | Source                                            | Op |
|------------------------|---------------------------------------------------|-----|
| `masterOutFaderGain`   | `mixerstate['fairlight-master-properties'].volume / 100` (×0.01 dB) | `operations.set_fairlight_master(volume_db=...)` |
| `followFadeToBlack`    | `mixerstate['fairlight-master-properties'].afv`   | `set_fairlight_master(afv=...)` |
| `audioFollowVideoCrossfadeTransition` | (no atemwire field) | gap |
| `MasterOutEqualizer.enabled` | `fairlight-master-properties.eq_enable`     | `set_fairlight_master(eq_enable=...)` |
| `MasterOutEqualizer.gain`    | `fairlight-master-properties.eq_gain / 100` | `set_fairlight_master(eq_gain_db=...)` |
| `MasterOutDynamicsProcessor.makeupGain` | `fairlight-master-properties.dynamics_gain / 100` | `set_fairlight_master(dynamics_makeup_db=...)` |

**Implemented as of 2026-04-28** for the master fader / EQ enable /
EQ gain / dynamics make-up gain / AFV. One `CFMP` packet per
`Profile.apply` carries all of them.

> **Gap remains: Compressor / Limiter / Expander parameters.** These
> live in `mixerstate['fairlight-compressor-properties']` /
> `'fairlight-limiter-properties'` / `'fairlight-expander-properties'`
> /`'fairlight-master-compressor-properties'` /
> `'fairlight-master-limiter-properties'` (received via AICP / AILP /
> AIXP / MOCP / AMLP packets). The corresponding **send** wire
> commands are not yet reverse-engineered in atemwire — only the receive
> packets are parsed. Save serializes default values that match
> Software Control's blank output; restore skips with the logged note
> `compressor/limiter/expander writes not implemented in atemwire yet`.
> Round-trip on a parsed XML still preserves these elements verbatim.

### EqualizerBand (master + per-input, both share this shape)

| Attribute        | Source                                                          | Op |
|------------------|------------------------------------------------------------------|-----|
| `index`          | 0..5                                                             | (band index) |
| `enabled`        | `mixerstate['atem-eq-band-properties'][strip_id][band].band_enabled` | `operations.set_fairlight_eq_band(enabled=...)` |
| `shape`          | enum: `HighPass`,`LowShelf`,`BandPass`,`HighShelf`,`LowPass`,`Notch` (`band_filter`) | `set_fairlight_eq_band(shape=...)` |
| `frequencyRange` | enum: `Low`,`MidLow`,`MidHigh`,`High` (`band_freq_range`)         | `set_fairlight_eq_band(frequency_range=...)` |
| `frequency`      | `band_frequency` (Hz)                                            | `set_fairlight_eq_band(frequency_hz=...)` |
| `gain`           | `band_gain / 100` (dB)                                           | `set_fairlight_eq_band(gain_db=...)` |
| `qFactor`        | `band_q / 100`                                                   | `set_fairlight_eq_band(q=...)` |

**Implemented as of 2026-04-28.** One `CEBP` packet per band per
strip, plus six on the master EQ.

The mixerstate index for per-input EQ is the strip-id string built by
`AtemEqBandPropertiesField.strip_id`: `"<source>.<subchannel>"`
(stereo: `"1.0"`, split-mono: `"1.0"` and `"1.1"`). `_build_eq_bands`
reads through that key; the apply path calls into
`set_fairlight_eq_band(channel=-1)` for stereo or `channel=0/1` for
split, which the wire encodes as `split=0x01` or `0xff` accordingly.

### AudioInputs / AudioInput / AudioSource

The `id` attribute of `AudioInput` is the source ID (1..10 for
external SDI, 1301 for the ATEM XLR Mic, 1401 for the headphone TRS,
2001/2002 for color generators that route audio via media players,
etc.). These are the same ATEM source IDs as used elsewhere in the
profile.

`<Analog level="...">` appears **only** on inputs that have analog
configuration (i.e. the TRS jack, source 1401). Levels: `ProLine`
(line) or `Mic`. Maps to `mixerstate['fairlight-audio-input'].level`
(1=mic, 2=line).

`<AudioSource id="...">` represents one channel within the input. The
ID is from the `mixerstate['fairlight-strip-properties']` structure:
- `-65280` = stereo combined ("0xFF00" — the high byte is 0xFF
  meaning "not split", subchannel byte is 0).
- `-256` = mono ("0xFFFFFFFF" — both bytes 0xFF).
- `-768` etc. = split channels.

For Constellation HD, every AudioSource in the reference uses
`id="-65280"` (full-width stereo source) or `id="-256"` for mono.

| AudioSource attr | Source | Op |
|------------------|--------|-----|
| `inputGain`      | `fairlight-strip-properties.gain / 100` (dB) | `operations.set_fairlight_strip(input_gain_db=...)` |
| `pan`            | `pan / 100` (-100..100)                       | `set_fairlight_strip(pan=...)` |
| `faderGain`      | `volume / 100` (dB; `-inf` when `volume == -10000`) | `set_fairlight_strip(fader_gain_db=...)` (accepts `float('-inf')`) |
| `mixOption`      | enum: `Off` / `On` / `AudioFollowVideo` (`state` bitfield) | `set_fairlight_strip(mix_option=...)` |
| `delayFrames`    | `delay`                                       | `set_fairlight_strip(delay_frames=...)` |

**Implemented as of 2026-04-28.** One `CFSP` per `<AudioSource>` per
input, with all observed fields packed into a single mask. The
apply path also issues a second `CFSP` per strip that carries
`eq_enable` + `eq_gain_db` from the `<Equalizer>` child element, then
a third for `dynamics_makeup_db` from the `<DynamicsProcessor>` child.

`Equalizer` and `DynamicsProcessor` shapes mirror the master section.

### AudioHeadphoneOutputs

```
<AudioHeadphoneOutput index="0" gain="..." masterOutGain="..." masterOutMute="..."
                      talkbackGain="..." talkbackMute="..." sidetoneGain="..."/>
```

| Attribute      | Source                                            |
|----------------|---------------------------------------------------|
| `gain`         | `mixerstate['fairlight-headphones'].volume / 100` |
| `masterOutGain`/`Mute` | (no atemwire field)                          |
| `talkbackGain`/`Mute`  | (no atemwire field)                          |
| `sidetoneGain` | (no atemwire field)                                  |

> **Gap.** Only `volume` is exposed. The other levels would need
> additional FMHP-adjacent packets that atemwire doesn't currently parse.

---

## MediaPlayers

```
<MediaPlayer index="0..1" sourceType="Still|Clip" sourceIndex="..."/>
```

| Attribute      | Source                                              | Op |
|----------------|-----------------------------------------------------|----|
| `sourceType`   | `mediaplayer_selected.source_type` (1=Still, 2=Clip) | `set_media_player_still` / `set_media_player_clip` |
| `sourceIndex`  | `mediaplayer_selected.slot`                          | (same op) |

---

## MediaPool

```
<MediaPool>
  <Stills>
    <Still index="..." name="..." path="ATEM Media Pool/<name>.png"/>
  </Stills>
</MediaPool>
```

The `Stills` section enumerates the **populated** slots (ones where
`mediaplayer-file-info.is_used == True`).

| Attribute | Source                                         |
|-----------|------------------------------------------------|
| `index`   | slot index                                     |
| `name`    | `mediaplayer_slot_info.name`                   |
| `path`    | `"ATEM Media Pool/<name>.png"` — Software Control's local path convention; not on the switcher |

**Implemented as of 2026-04-28** (was previously "out of scope").
``Profile.from_atem(atem)`` with ``SaveOptions.media_pool_images=True``
captures every used slot's PNG via ``download_media_pool_images``
(wraps ``atemwire.rawtransfer.raw_download_slot``). On the apply side,
``Profile.apply(atem, options)`` with
``options.restore_media_pool_images=True`` signals that image bytes
the caller supplies are to be pushed into the media pool by the
caller's own upload path after the XML config is applied.

Filenames match Software Control's convention
(``ATEM Media Pool/<name>.png``) so a profile saved by this module is
cross-tool compatible with Software Control's "Save Switcher State".
When media-pool images are included in a save, a caller can bundle
the XML and the captured PNGs into a single ZIP
(``<basename>.xml`` at root + ``ATEM Media Pool/<name>.png`` entries)
that the operator downloads in one click — extracting the ZIP yields
the same on-disk layout Software Control's restore expects. When no
images are included, the response is the bare XML. Load uses standard
``<input type="file">`` pickers — single-select for the XML,
multi-select for the matching images. See the "Section selection"
section below.

---

## CameraControl

```
<Parameter device="1..10" category="Lens|Video|ColorCorrection" parameter="..." value="..."/>
<Parameter device="..." category="Video" parameter="ManualWhiteBalance" temperature="5600" tint="0"/>
<Parameter device="..." category="ColorCorrection" parameter="LiftAdjust" red="0" green="0" blue="0" luma="0"/>
<Parameter device="..." category="ColorCorrection" parameter="ContrastAdjust" pivot="0.5" adjust="1"/>
<Parameter device="..." category="ColorCorrection" parameter="ColorAdjust" hue="0" saturation="1"/>
<Property device="1..10" property="ApertureCoarse" value="1"/>
<Property device="1..10" property="Locked" value="False"/>
```

> **Gap.** atemwire parses CCdP (camera control data packet) but doesn't
> expose typed accessors for individual parameters. The format here is
> consistent and machine-readable (one Parameter per category/parameter
> combo), but all values are device-specific (Blackmagic camera
> protocol). Round-trip preserves verbatim; restore is **out of scope**
> (no Send class for ``CCdP`` yet).

---

## MacroPool

```
<MacroPool>
  <Macro index="0..N" name="..." description="...">
    <Op id="..." [op-specific attrs]/>
    ...
  </Macro>
</MacroPool>
```

The `Op` elements describe the macro's serialized command sequence. Each
op corresponds to one `atemwire.macrocommand.MacroCommand` opcode (which
in turn maps to a wire `MacroCommand` block when stored on the switcher).

### Observed Op IDs

The reference XML uses these op IDs:

| Op `id`                                  | Wire effect                              | atemwire equivalent |
|------------------------------------------|------------------------------------------|--------------------|
| `ProgramInput`                           | Set program bus on ME                    | `set_program`      |
| `PreviewInput`                           | Set preview bus on ME                    | `set_preview`      |
| `KeyOnAir`                               | USK on/off (no transition)               | `set_usk_on_air`   |
| `KeyType`                                | USK type (Luma/Chroma/Pattern/DVE)       | `set_usk_type`     |
| `KeyFillInput` / `KeyCutInput`           | USK fill/key source                      | `set_usk_fill_source` / `set_usk_key_source` |
| `KeyMaskEnable`                          | USK mask on/off                          | `set_usk_mask_enabled` |
| `KeyFlyEnable`                           | Toggle flying-key mode                   | `set_usk_fly_enabled` |
| `LumaKeyClip` / `LumaKeyGain`            | USK luma clip/gain                       | `set_usk_luma_clip/gain` |
| `LumaKeyInvert` / `LumaKeyPreMultiply`   | USK luma invert/premultiply              | `set_usk_luma_invert/pre_multiplied` |
| `DVEAndFlyKeyXSize` / `YSize`            | USK DVE size                              | `set_usk_dve_size_x/y` |
| `DVEAndFlyKeyXPosition` / `YPosition`    | USK DVE position                          | `set_usk_dve_position_x/y` |
| `DVEKeyMaskEnable` / `MaskTop` / `Bottom` / `Left` / `Right` | USK DVE mask | `set_usk_dve_masked` / `set_usk_dve_top/bottom/left/right` |
| `DownstreamKeyOnAir`                     | DSK on-air                                | `set_dsk_on_air` |
| `DownstreamKeyAuto`                      | DSK auto transition                       | `dsk_auto`       |
| `DownstreamKeyFillInput` / `CutInput`    | DSK fill/key source                       | `set_dsk_fill_source/key_source` |
| `DownstreamKeyRate`                      | DSK rate                                  | `set_dsk_rate`   |
| `DownstreamKeyMaskEnable`                | DSK mask on/off                           | `set_dsk_mask_enabled` |
| `DownstreamKeyPreMultiply`               | DSK pre-multiply                          | `set_dsk_pre_multiplied` |
| `DownstreamKeyClip` / `Gain`             | DSK clip/gain                             | `set_dsk_clip/gain` |
| `TransitionStyle`                        | Transition style                          | `set_transition_style` |
| `TransitionSource`                       | Next-transition layers                    | `set_next_transition_layers` (absolute mask, one packet) |
| `AuxiliaryInput`                         | Aux output routing                         | `set_aux_output` |
| `MediaPlayerSourceStill`                 | Set source-type to Still on a media player | `set_media_player_still` |
| `MediaPlayerSourceStillIndex`            | Set still-slot index on a media player    | (combined; same op) |
| `FairlightAudioMixerInputSourceMixType`  | Per-input mix mode (On/Off/AFV)           | gap (Fairlight strip op) |
| `FairlightAudioMixerInputSourceFaderGain`| Per-input fader gain                       | gap |
| `FairlightAudioMixerMasterOutFaderGain`  | Master out fader gain                      | gap |
| `MacroSleep`                             | Wait N frames                              | (executed inside the switcher; no atemwire op) |

### Op attribute grammar

Each Op uses a small grammar of attributes:

- `mixEffectBlockIndex`: ME index for ME-scoped ops.
- `keyIndex`: USK keyer index (0..3) when on a ME, or DSK index (0)
  when on the DSK.
- `mediaPlayer`: 0..1.
- `input` / `source` / `fillInput` / `cutInput`: source name as a
  **symbolic string** (see [Source ID reference](#source-id-reference)).
- `clip` / `gain` / `xSize` / `ySize` / etc.: floats matching the
  units used elsewhere in the file (e.g. `clip="0.300003"` is a 0..1
  scalar; `gain="-0.0800018"` is dB; `xPosition="0"` is unit-scaled).
- Audio-op `gain` is in dB but **specially encoded**: `-327.68`
  represents the wire's `-32768/100`, which is the silenced sentinel
  value (≡ `-inf` in the declarative `<AudioSource>` representation).
- Audio-op `sourceId` is a 64-bit unsigned integer that encodes the
  same concept as the `<AudioSource id>` attribute, but in a different
  representation: `18446744073709486336` = `0xFFFFFFFFFFFF0000` is the
  unsigned 64-bit reading of the same -65280 value (full-width stereo).

**Macro round-trip uses bytecode upload (partial-upload policy) as of
2026-04-29.**

- **Apply.** Each `<Macro>` is encoded **per-op** via
  `atemwire.macrotransfer.encode_single_op`, and the resulting chunks
  concatenated into the macro's bytecode and uploaded via the
  file-transfer protocol (FTSD/FTCD/FTDa/FTFD/FTDC, store=0xFFFF,
  mode=0x0300). Ops the encoder doesn't recognise (Fairlight,
  VideoMode, etc.) are **dropped** from that macro's bytecode and
  enumerated in the apply-result message; the surviving ops still
  upload, so the macro lands in its slot with most of its
  functionality intact. The bytecode lands directly in the slot — no
  live ATEM state changes, unlike the previous recording-based
  approach that drove program/USK/DSK through every recorded op.

  Operator-facing behaviour:
  - All ops encodable: ``macros (N/M restored, 0 failed)``
  - Some ops dropped: ``macros (N/M restored, K with dropped ops:
    slot X ('name') dropped 1 op (FairlightAudioMixerInputSourceMixType))``
  - All ops dropped: same as "with dropped ops" but with an
    ``— metadata only`` suffix on the affected slot. Falls back to
    `MSRc → MAct(stop)` so name + description still land.
  - Upload itself fails: ``macros (N/M restored, K failed: slot X
    ('name') — upload error: <details>)``. The slot is left in
    whatever state it was — the macro DIDN'T land. Distinct from
    drops because drops still produce a usable slot.

  Operators should treat dropped ops as "the macro will behave
  differently than originally recorded" — re-record manually if a
  dropped op is functionally important.

- **Save.** ATEM exposes recorded macros via the standard
  file-transfer protocol (FTSU/FTDa/FTUA/FTDC) using store id
  `0xFFFF`. The bytecode is little-endian (the wire protocol's only
  little-endian payload) and decodes to the same op-id strings
  Software Control's XML uses.
- **Empty macros.** Macros with no `<Op>` children fall back to the
  recording path (MSRc → MAct stop) which sets only metadata. This is
  the only case where the recording path runs during apply — and it
  doesn't change live state because no ops are issued.

See [`MACRO_FORMAT.md`](MACRO_FORMAT.md) for the wire layouts of
FTSU/FTSD/FTDa/FTFD/FTDC, the bytecode op-code table, and verified
live tests.

`Profile.apply` walks `<MacroPool><Macro>` after inputs and labels
have been applied (so the encoder can resolve `Camera5` etc. against
post-rename input properties). **143 op ids** round-trip via the
encoder in `atemwire.macrotransfer._KNOWN_OPS`, covering the entire
macro-recordable surface of the 1 M/E Constellation HD: transitions
across all five styles, color generators, fade-to-black, USK mask +
fly-key + DVE + border + shadow, full Advanced Chroma Key, fly-key
keyframe ops, Fairlight EQ + compressor + limiter + expander +
headphones. A comprehensive Software Control recording (Lots XML,
2026-04-30) restores with `macros (12/12 restored, 0 failed)` —
zero drops, with **168/168 ops byte-identical** through encode →
decode → encode round-trip.

`Profile.from_atem` walks every used macro slot, downloads its
bytecode via `download_macro_bytecode`, parses the LE op stream via
`decode_macro_bytecode`, and emits `<Op>` children matching what
Software Control would write. Per-slot download or decode failures are
logged and the macro is emitted with metadata only — they don't
poison sibling slots.

`_KNOWN_OPS` is the single source of truth: each entry is a 3-tuple
`(xml_id, decoder, encoder)`. A unit test
(`test_known_ops_table_entries_are_3_tuples`) enforces the shape so a
new op can't land with only one direction defined.

Live verification: an end-to-end download → decode → encode → upload
→ download cycle on slot 0 ("WIDE CAM", 712 bytes, 52 ops) is
byte-identical when every op is encodable. Reference XML
(`everything_2026-04-27_18-16-51.xml`, 8 macros including 2 with
Fairlight ops) reports ``macros (8/8 restored, 2 with dropped ops:
slot 0 ('WIDE CAM') dropped 25 ops; slot 2 ('CAMERA TOP') dropped 28
ops)`` — both partial macros land with all their non-Fairlight ops
intact.

`ApplyOptions.restore_macros` defaults to `True`.

---

## MacroControl

```
<MacroControl loop="False"/>
```

| Attribute | Source                                       | Op (gap) |
|-----------|----------------------------------------------|----------|
| `loop`    | `mixerstate['macro-play-status'].is_looping` | gap (`MacroActionCommand` does not currently expose loop) |

---

## Counters

```
<Counter index="0" enabled="False" mode="Countdown" autoHide="False"
         positionX="0" positionY="-6.3" size="25" opacity="100"
         startFromHours="0" startFromMinutes="0" startFromSeconds="0" startFromFrames="0"/>
```

> **Gap.** Display-clock / counter overlay properties. atemwire parses
> DCPV/DSTV but doesn't surface typed accessors. Round-trip preserves;
> restore out of scope.

---

## Source ID reference

Numeric ATEM source IDs used in this file:

| Range / value | Meaning                              |
|--------------|---------------------------------------|
| `0`          | Black                                  |
| `1..10`      | External inputs (Camera 1..10 on Constellation HD) |
| `1000`       | Color Bars                            |
| `2001`,`2002`| Color generators 1, 2                 |
| `3010`,`3011`| MediaPlayer1 fill, MediaPlayer1 key   |
| `3020`,`3021`| MediaPlayer2 fill, MediaPlayer2 key   |
| `4000`,`4010`,`4030` | Program out, Preview out, Clean1 out |
| `5010`,`5011`,...    | ME1 program, ME1 preview (ME-scoped outputs) |
| `8001..8006` | Aux 1..6                              |
| `9000..900N` | MultiViewer N                         |
| `10010`,`10011` | ProgramOut, PreviewOut             |
| `1301`       | XLR mic input (audio-only)            |
| `1401`       | TRS jack (audio-only, has Analog level) |

Symbolic names used inside `<Op>` elements correspond to these IDs.
The mapping is implicit in Software Control; atemwire reproduces it via
`InputPropertiesField.short_name` and the small lookup table in
`atemwire.profile`.

---

## Enum tables

### Wipe pattern (`<WipeParameters pattern="...">`, `<PatternParameters style="...">`)

These are the 18 pattern enum values exposed by ATEM. Names are spelled
exactly as ATEM Software Control's "Save Switcher State" XML emits them.
Wire 0/1/2/3 and 6 were captured live against ASC 2026-08-31 (the
earlier 0-4 rows here were wrong: `HorizontalBars`/`VerticalBars`
don't exist in ASC's enum, `LeftToRightBar`/`TopToBottomBar` are 0/1 not
2/3, and `CornersInFourBox` at index 4 was missing, shifting everything
below it). The remaining rows mirror `atemwire/profile/_enums.py`.

| Wire int | Name (XML)              |
|----------|-------------------------|
| 0        | `LeftToRightBar`         |
| 1        | `TopToBottomBar`         |
| 2        | `HorizontalBarnDoor`     |
| 3        | `VerticalBarnDoor`       |
| 4        | `CornersInFourBox`       |
| 5        | `RectangleIris`          |
| 6        | `DiamondIris`            |
| 7        | `CircleIris`             |
| 8..11    | `TopLeftBox`, `TopRightBox`, `BottomRightBox`, `BottomLeftBox` |
| 12..15   | `TopCentreBox`, `RightCentreBox`, `BottomCentreBox`, `LeftCentreBox` |
| 16..17   | `TopLeftDiagonal`, `TopRightDiagonal` |

(atemwire operations accept the integer; the profile module does the str↔int
mapping via `WIPE_PATTERN_NAMES`.)

### DVE transition effect (`<DVEParameters effect="...">`)

Corrected against `atemwire/profile/_enums.py` `DVE_EFFECT_NAMES` (16=`SqueezeTopLeft`
hardware-confirmed via `tools/macro_dve_probe.py`, reconciled 2026-06-03). The profile
enum has **no** Swoosh entries and no wire-13; `GraphicLogoWipe=34` exists only in the
macro codec table (`macrotransfer/_helpers.py`), not here.

| Wire int | Name (XML)  |
|----------|-------------|
| 16..19   | `SqueezeTopLeft`, `SqueezeTop`, `SqueezeTopRight`, `SqueezeLeft` |
| 20..23   | `SqueezeRight`, `SqueezeBottomLeft`, `SqueezeBottom`, `SqueezeBottomRight` |
| 24..27   | `PushTopLeft`, `PushTop`, `PushTopRight`, `PushLeft` |
| 28..31   | `PushRight`, `PushBottomLeft`, `PushBottom`, `PushBottomRight` |

### Frequency band shape (`<EqualizerBand shape="...">`)

| Wire bitfield | XML name    |
|---------------|-------------|
| 0x01          | `LowShelf`  |
| 0x02          | `LowPass`   |
| 0x04          | `BandPass`  |
| 0x08          | `Notch`     |
| 0x10          | `HighPass`  |
| 0x20          | `HighShelf` |

### MixOption (`<AudioSource mixOption="...">`)

| Wire bitfield | XML name             |
|---------------|----------------------|
| 0x01          | `Off`                |
| 0x02          | `On`                 |
| 0x04          | `AudioFollowVideo`   |

### USK type (`<Key type="...">`)

| Wire int | XML name |
|----------|----------|
| 0        | `Luma`   |
| 1        | `Chroma` |
| 2        | `Pattern`|
| 3        | `DVE`    |

---

## Section selection

An application can ship a Software Control–style section-selection dialog
on top of `Profile.from_atem` (save) and `Profile.apply` (load). The
underlying API:

- `atemwire.profile.SaveOptions` — per-section flags gating which
  `<...>` top-level elements `from_atem` emits. All True by default
  (the no-args `Profile.from_atem(atem)` call still produces a
  full-fidelity profile, byte-identical with prior versions on the
  reference XML).
- `atemwire.profile.ApplyOptions` — per-element flags controlling
  restore. The M/E 1 grid has nine independent flags
  (``restore_program``, ``restore_preview``,
  ``restore_next_transition``, ``restore_transition_style``,
  ``restore_fade_to_black``, ``restore_usk[i]`` for ``i`` in 0..3).
  Each flag controls exactly the XML element it names — no
  coalescing. Coarse flags cover sections outside the M/E box
  (``restore_downstream_keys``, ``restore_color_generators``,
  ``restore_audio``, ``restore_aux``, ``restore_video_mode``,
  ``restore_inputs``, ``restore_macros``, ``restore_media_players``,
  ``restore_settings_flags``, ``restore_media_pool_images``).

  Defaults: every supported section on, except ``restore_program`` /
  ``restore_preview`` (cuts-to-air gates).

  M/E granularity is single-tab today — ``restore_usk`` is a flat
  4-element list, not per-M/E. Multi-M/E support would re-introduce
  the per-M/E nesting; deferred until cross-model support is added.

- `atemwire.profile.SaveOptions` — per-element flags controlling save.
  Mirrors ``ApplyOptions`` for the M/E grid: ``program``, ``preview``,
  ``next_transition``, ``transition_style``, ``fade_to_black``,
  ``usk[i]``. Each flag gates emission of exactly the corresponding
  XML element — ``<Program>``, ``<Preview>``, ``<NextTransition>``,
  ``<TransitionStyle>`` (with its five per-style children), the
  ``<Key index="i">`` inside ``<Keys>``, and ``<FadeToBlack>``.

  ``<Keys>`` itself is only emitted when at least one ``usk[i]`` is
  True. ``<MixEffectBlocks>`` is only emitted when at least one M/E
  flag is True. All flags default True — the no-args
  ``Profile.from_atem(atem)`` call still produces a byte-identical
  full-fidelity profile on the reference XML.
- `describe_save_sections(atem)` /
  `describe_load_sections(profile)` — return the JSON section
  descriptor an application's save/load dialog renders. This lives in the
  application rather than atemwire itself — the descriptor shape is UI
  scaffolding (``group``, ``me_tab``, ``checked``, ``supported``,
  ``reason``), not part of the save/restore feature. Sections are a flat list; each
  carries a ``group`` field — one of ``switcher_me``,
  ``switcher_global``, ``media``, ``io``, ``others`` — that the
  frontend uses to lay things out in Software Control's grid style.
  Sections in ``switcher_me`` carry an additional ``me_tab`` integer
  (always ``1`` today) so the renderer can place them inside the
  appropriate M/E tab box. The descriptor also returns a top-level
  ``me_tabs`` list of tab numbers. Multi-M/E rendering will become
  conditional on ATEM topology when cross-model support is added.

  Sections that don't apply to the 1 M/E Constellation HD aren't
  emitted at all — SuperSource, M/E 2-4, Media Player 3-4,
  Stream/Record, Audio Mapping, Remote Source. Sections backed by
  apply gaps (Camera Control, HyperDecks, Counter) emit with
  ``supported: False`` so the cell renders disabled with a
  hover-tooltip explanation.

  ``describe_load_sections`` is driven by the parsed XML — only
  sections whose XML element is actually present in the loaded
  profile are emitted. Operators can't restore what wasn't saved,
  so omitting absent sections keeps the dialog honest. ``me_tabs``
  is empty if no ``switcher_me`` cells are present in the XML.

  Presence checks use ``element is not None`` — ``bool(Element)`` is
  False for self-closing elements like ``<VideoMode videoMode="..."/>``
  in ElementTree, which would falsely report them as missing. (See
  the ``test_self_closing_video_mode_is_present`` regression test.)

  The descriptor's section ``id`` values are stable: they map
  directly to the keys the application reads out of the dialog's
  selection map when it builds ``SaveOptions`` / ``ApplyOptions``.
- `atemwire.profile.download_media_pool_images(atem)` — captures every
  used media-pool slot as a PNG via the still-store file-transfer
  protocol (FTSU/FTDa) over the connection's native still download.
  Saves cross-tool-compatible filenames matching
  `<Still name="..."/>.png`.

## Implementation status

The `atemwire.profile` module ships:

- **Save** (`Profile.from_atem(atem)`, `Profile.to_xml()`): produces an
  XML profile from a connected ATEM's mixerstate. Sections covered as
  far as atemwire currently exposes the underlying data — gaps documented
  above are emitted with sensible defaults that match Software Control's
  observed output.
- **Round-trip** (`Profile.from_xml(s).to_xml()`): preserves the input
  modulo XML whitespace differences. The reference file at the top of
  this document is the round-trip test fixture.
- **Apply** (`Profile.apply(atem, options)`): walks the XML tree and
  issues atemwire operations for every section that has a clean setter
  path. Defaults to applying every section that has a working
  implementation; the cuts-to-air gate (Program/Preview) is the only
  section off by default. Sections backed by gaps (multiview,
  talkback, hyperdeck binding, camera control, the Fairlight
  compressor/limiter/expander triplet) are gated off and report
  skipped with the documented reason. The `ApplyResult` reports
  per-section what was applied vs. skipped vs. errored.

### Sections that restore today

| Section                 | Status |
|-------------------------|--------|
| Program / Preview       | Opt-in (`options.restore_program_preview=True`) |
| **Video mode**          | ✅ (destructive — see VideoMode section) |
| **Input renames** (long + short labels) | ✅ |
| **Fairlight master** (volume / EQ enable / EQ gain / dynamics make-up / AFV) | ✅ |
| **Fairlight per-strip** (input gain / EQ enable+gain / dynamics gain / pan / fader / mix option / delay) | ✅ |
| **Fairlight EQ bands** (master + per-strip, six bands each: enabled / shape / freq range / frequency / gain / Q) | ✅ |
| **Macros** (record-by-replay apply; FTSU+LE-bytecode-decode save; 39 op ids covered with apply↔decoder symmetry guarded at import time) | ✅ — see MACRO_FORMAT.md |
| Transition style + per-style rate/params | ✅ |
| USK on-air, type, sources, mask, luma, chroma color-correct, pattern, DVE | ✅ |
| USK fly enabled / keyframe data | Partial (enable only; keyframe data gap) |
| DSK fill/key, rate, mask, clip/gain/invert/pre-mult, on-air | ✅ |
| Color generators        | ✅ |
| Aux outputs             | ✅ |
| Media player slot assignment | ✅ |
| Fade-to-black rate + enabled | ✅ |
| Next-transition selection | ✅ (absolute mask via `set_next_transition_layers`, single packet) |

### Sections that don't restore (documented gaps)

- **Fairlight compressor / limiter / expander** parameters — receive
  packets parsed by atemwire but no send wire commands available yet
- **MultiView layout + window routing** (`<MultiViews>`)
- **Talkback / Engineering talkback** (`<Talkback>`, `<EngineeringTalkback>`)
- **Mix-minus output mode** (`<MixMinusOutputs>`)
- **HyperDeck binding** (`<HyperDecks>`)
- **Camera control parameters** (`<CameraControl>`)
- **Counter / display-clock** (`<Counters>`)
- **Settings flags** (`abDirect`, `cameraAux`, `SDI3GOutputLevel`,
  `MediaPool.Clips.maxFrameCount`, `ButtonMapping.*`,
  `UpstreamKeys.sizeLink`, `Inputs.externalPortType`)

These all round-trip cleanly — the values are read from the live
state-dump and serialized — but `apply()` skips them with a logged
note. They become reachable as atemwire adds the corresponding Send
classes under ``atemwire.messages.<feature>``.

### Other models / format versions

This document only describes one switcher (Constellation HD) and one
format version (2.1). Other ATEMs almost certainly add or shift
attributes — particularly multi-ME boxes, ATEMs with SuperSource
(`<SuperSource>` not present here), ATEMs with more keyers/aux/DSK,
and the Mini family which has different multiview layouts. The
`atemwire.profile` parser is permissive (unknown attributes preserved
verbatim) so reading a profile from another model should not fail; the
applier may simply have nothing to do for unknown sections.

To extend coverage to a new model, drop a representative export into
the test fixtures and run `pytest tests/test_profile_roundtrip.py`.
Adapt the parser as needed; this document is the canonical place to
record what changes.
