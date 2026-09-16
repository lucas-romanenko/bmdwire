# Handoff: bmdwire Brand Assets

## Overview

Repository identity for **bmdwire** — one Python package carrying four libraries for Blackmagic
Design broadcast devices (`atemwire`, `hyperdeckwire`, `ultimattewire`, `videohubwire`).

This is a **repo-presentation** package, not an app UI. There is no interface to build. The
deliverables are a logo, a color and type system, a GitHub social preview image, and a
README banner.

## About the design files

`BMDwire Brand.dc.html` in this folder is a **design reference created in HTML** — a brand
board showing the intended look. It is not production code and nothing needs to import it.
Use it as the source of truth for geometry and values.

`bmdwire-social-preview.png` is a finished asset. Upload it as-is (see §4).

Section 01 of the board contains a **mark study** with two rejected alternatives (B trace,
C brackets). Those document the decision. Only **A** ships.

## Fidelity

**High fidelity.** All colors, ratios and type are final.

---

## 1. Logo

### The mark

A bus with four taps: one vertical spine on the left, four horizontal branches of varying
length to the right. One wire, four device families.

Geometry at tile size `S`:

- Tile: `S × S`, `border-radius: round(S * 0.235)`, `background: #0A0B0C`,
  `border: 1px solid #34383C`, `box-sizing: border-box`, `padding: round(S * 0.19)`
- Tile is `display: flex; gap: round(S * 0.09)`
- **Spine:** `width: max(2, round(S * 0.11))`, full height,
  `border-radius: max(1, round(S * 0.045))`, `background: #D98A3D`
- **Taps:** `flex: 1` column, `justify-content: space-between`, four bars each
  `height: max(2, round(S * 0.09))`, same radius, `background: #8A9094`
  - Widths, top to bottom: **100%, 72%, 88%, 58%**

The varying widths are deliberate — equal-length taps read as a list or a hamburger menu.
Keep the order; it is not random.

**Light-ground variant:** tile `#D3D2CD`, border `#CFCEC9`, spine `#A0611F`, taps `#6E706C`.

### Small-size rule — required

**At 32px and below, drop to three taps** at widths **100%, 68%, 86%**, and drop the bar
radius to `1px`. Four 2px bars in a 32px tile merge into a smear. The spine never drops at
any size.

### Wordmark

`bmdwire`, set solid, in Archivo:

- `bmd` — weight **400**, `#9A9E9F` (dark ground) / `#6E706C` (light)
- `wire` — weight **800**, `#EDEDEA` (dark) / `#111312` (light)
- `letter-spacing: -0.03em`, `line-height: 1`

Lowercase always. It is a pip install name.

### Lockup

Mark left, wordmark right, on a horizontal baseline. `gap: 20px` at a 56px mark with a 34px
wordmark; scale proportionally.

### Secondary wordmarks

- `bmd/wire` — IBM Plex Mono 600, slash in `#D98A3D`. For CLI output and docs.
- `BMDWIRE` — Archivo 600, `letter-spacing: 0.16em`. For small labels.

---

## 2. Color

| Token | Hex | Use |
|---|---|---|
| `--ink` | `#0E0F10` | Page / banner background |
| `--tile` | `#0A0B0C` | Logo tile field, deepest ground |
| `--surface` | `#141618` | Cards, chips, badges |
| `--border` | `#26292C` | Dividers |
| `--border-strong` | `#34383C` | Tile edge, chip outlines |
| `--text` | `#EDEDEA` | Primary text |
| `--text-muted` | `#9A9E9F` | Secondary text, the `bmd` in the wordmark |
| `--text-faint` | `#7E8386` | Captions, mono metadata |
| `--copper` | `#D98A3D` | The accent — spine, slash, rules, links |
| `--tap` | `#8A9094` | Tap bars in the mark |
| `--paper` | `#F2F1EE` | Light ground |

### Rules

1. **Copper is the only accent.** Do not introduce a second.
2. **Never use red or green.** In this ecosystem they mean program and preview and belong
   to the operator-facing app. A library that never renders a bus does not wear them.
3. **On copper fills, use ink `#0E0F10` for text, never white.**
4. Copper on ink is 7.4:1, so it is also safe as link and heading text. Link hover:
   `#EFBB86`.

### Relationship to webATEM

Ink, greys, radii, both typefaces and the weight-contrast wordmark are shared with the
webATEM identity, so the two read as a set. Only the accent and the mark differ. If you
touch shared values, change them in both.

---

## 3. Typography

```
Archivo — 400, 600, 800
IBM Plex Mono — 400, 600
```

**Archivo** for prose and display. **IBM Plex Mono for anything machine-facing** — package
names, versions, ports, install commands, labels. In a banner, every package name is mono.

| Role | Family | Size | Weight | Tracking |
|---|---|---|---|---|
| Social preview wordmark | Archivo | 82px | 800 | -0.035em |
| Banner wordmark | Archivo | 28–44px | 800 | -0.03em |
| Social preview subhead | Archivo | 30px | 400 | 0, `line-height: 1.35` |
| Body | Archivo | 15–19px | 400 | 0, `line-height: 1.55` |
| Overline | IBM Plex Mono | 11px | 400 | 0.18em, uppercase, `--text-faint` |
| Chip / package name | IBM Plex Mono | 12–19px | 400 | 0 |

---

## 4. GitHub social preview

`bmdwire-social-preview.png` — **1280×640**, ready to upload.

> Repo → Settings → General → Social preview → Edit → Upload an image

Constraints baked in, in case it is ever regenerated:

- The composition is centred, so GitHub's crops in link unfurls stay balanced.
- Nothing sits in the outer 40px. GitHub crops to roughly 2:1 in some placements.
- Smallest type is 19px, so it survives being scaled down in a link unfurl.
- A 10px copper bar runs the full height of the left edge.
- The four package names appear as mono chips: `atemwire`, `hyperdeckwire`,
  `ultimattewire`, `videohubwire`.

---

## 5. README banner

Optional. If you want one at the top of the root `README.md`, the board's section 05 is the
design: tile background `#0A0B0C`, a 6px copper rule on the left edge, 46px mark plus
wordmark, the repo's one-line description, then three mono chips (`pure stdlib`,
`prebuilt wheels`, `CPython 3.10–3.14`).

Render it at **1280px wide** and commit it as `.github/banner.png`, referenced with a plain
`<img>` at the top of the README. GitHub strips CSS from rendered markdown, so a banner has
to be an image.

Keep the existing badge row and package table exactly as they are — the banner sits above
them and replaces nothing.

---

## 6. Legal

Carry the line already in the README wherever the identity appears:

> Not affiliated with or endorsed by Blackmagic Design Pty Ltd. ATEM, HyperDeck, Ultimatte
> and Videohub are trademarks of Blackmagic Design.

Do not use Blackmagic logos, product photography, or brand colors. Copper is this project's
own.

---

## Files

- `bmdwire-social-preview.png` — 1280×640, upload as-is
- `BMDwire Brand.dc.html` — the brand board; open in a browser

## Icon sizes to generate

Render the mark at 512, 256, 128, 64, 32, 16 if a favicon or docs icon is ever needed.
Four taps at 56 and above, three at 32 and below.

---

## Where these live in this repository

| Asset | Path | Status |
|---|---|---|
| README banner | `.github/banner.png` | rendered at 1280 CSS px (2560 physical) from `banner.html`, in the README |
| Social preview | `docs/brand/social-preview.png` | **needs a manual upload**, see below |
| Brand board | `docs/brand/brand-board.html` | reference, open in a browser |
| Banner source | `docs/brand/banner.html` | section 05's markup, standalone at 1280px |

To re-render the banner after editing `banner.html`, load it in a browser at a
1280px viewport and screenshot the `#banner` element at device pixel ratio 2.

**The social preview is the one thing that cannot be committed.** GitHub stores
it as a repository setting, not a file, and its API does not expose it. Upload
`docs/brand/social-preview.png` by hand:

> Repo → Settings → General → Social preview → Edit → Upload an image

Until that is done, a link to this repository unfurls with GitHub's default
grey card instead of the brand image.
