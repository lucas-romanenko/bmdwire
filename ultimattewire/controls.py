# SPDX-License-Identifier: MIT
"""
Every control in Blackmagic's "Ultimatte 12 Ethernet Protocol" v2.0
(September 2022), as a table: its wire name, its kind, its documented
range or choices, and the models that don't have it.

The ``Controls`` table in that document is the only source. Each entry
keeps the document's own wording; where the document is unclear the
comment says so, and nothing is filled in from elsewhere.

Kinds:

- ``range``: an integer, ``minimum``..``maximum`` inclusive. The only kind
  that takes the relative ``Offset <name>: <delta>`` form.
- ``bool``: ``On/Off``.
- ``choice``: one of ``choices`` (the document's spelling; compared
  without regard to case, because the unit reports them in lower case:
  an Ultimatte 12 4K on software 2.3 sent ``Monitor Out: fg`` and
  ``Video Format: auto detect``).
- ``function``: footnote 5, "These controls represent functions. Send
  'Yes' to execute the function. Ultimatte will respond with a 'Yes' to
  indicate the function is complete." An Ultimatte 12 4K on 2.3 never
  did for Sample Wall / Sample Floor (captured 2026-11-12); see
  ``Ultimatte.run_function``.
- ``readonly``: ``Tally Active``, "On/Off {Read Only}".

Model limits, from the footnotes:

- ``*``  "not available on the Ultimatte 12 HD Mini"
- ``**`` "not available of the Ultimatte 12 HD or HD Mini"
- footnote 1: Veil and Flare Correct Vertical Size are "Ultimatte device
  dependent. For Ultimate 12 8K running protocol 2.0, the range is 0-6.
  For Ultimatte 12 running version 1.2, the range is 0-3." The table
  keeps the wider 0-6; the unit itself refuses what it can't take.
- footnote 2: Output Range is "only available in protocol version 2.0 and
  up".
- footnote 4: Video Format choices are device dependent: 4320p only on the
  12 8K, no PsF on the HD Mini.
- Window Position Top/Bottom/Left/Right are "0-{Based on Video Format}".
  Read here as the frame's height (Top, Bottom) or width (Left, Right) in
  the running format, inclusive. Not verified on hardware.

None of this has been run against a unit yet except what the existing
archive/network code already used (the names in CONTROL). Ranges and
choices are the document's, not measurements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

MODEL_8K = "Ultimatte 12 8K"
MODEL_4K = "Ultimatte 12 4K"
MODEL_HD = "Ultimatte 12 HD"
MODEL_HD_MINI = "Ultimatte 12 HD Mini"
MODEL_12 = "Ultimatte 12"          # the original (non-8K, non-HD) model
MODEL_UNKNOWN = ""


def model_family(model: str) -> str:
    """Map an IDENTITY ``Model`` to one of the MODEL_* constants.

    ``""`` when the model isn't one the document names; callers then skip
    the model checks rather than guess."""
    m = " ".join((model or "").split()).lower()
    if not m.startswith("ultimatte 12"):
        return MODEL_UNKNOWN
    rest = m[len("ultimatte 12"):].strip()
    if rest == "hd mini":
        return MODEL_HD_MINI
    if rest == "hd":
        return MODEL_HD
    if rest == "8k":
        return MODEL_8K
    if rest == "4k":
        return MODEL_4K
    if rest == "":
        return MODEL_12
    return MODEL_UNKNOWN


# The DEVICE block's input lock lines each model has, from the note under
# "Device Information". The 12 4K and the original 12 aren't listed by the
# document; they are taken to have the full set, as the 12 8K's example
# does. "REFIn" is the document's spelling in its example; "REF In" in its
# note.
INPUTS_ALL = ("FG In", "BG In", "MONITOR In", "G MATTE In", "H MATTE In",
              "REF In", "BG MATTE In", "LAYER In", "LAYER MATTE In")
MODEL_INPUTS = {
    MODEL_HD_MINI: ("FG In", "BG In"),
    MODEL_HD: ("FG In", "BG In", "G MATTE In", "H MATTE In", "MONITOR In",
               "REF In"),
}


def model_inputs(family: str) -> Tuple[str, ...]:
    return MODEL_INPUTS.get(family, INPUTS_ALL)


def has_gpi(family: str) -> bool:
    """GPI LIST: "The Ultimatte 12 HD Mini does not have GPI inputs"."""
    return family != MODEL_HD_MINI


def has_camera_control(family: str) -> bool:
    """CAMERACONTROL: "only available on the Ultimatte 12 HD Mini"."""
    return family == MODEL_HD_MINI


# --------------------------------------------------------------------------
# Controls
# --------------------------------------------------------------------------

RANGE = "range"
BOOL = "bool"
CHOICE = "choice"
FUNCTION = "function"
READONLY = "readonly"

NOT_MINI = "*"          # not on the HD Mini
NOT_HD = "**"           # not on the HD or the HD Mini


@dataclass(frozen=True)
class Control:
    """One row of the document's Controls table."""
    name: str
    kind: str
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    choices: Tuple[str, ...] = ()
    limit: str = ""                   # "", NOT_MINI or NOT_HD
    min_protocol: Optional[Tuple[int, int]] = None
    note: str = ""
    window: str = ""                  # "height" / "width": range from the format

    def available_on(self, family: str) -> bool:
        if not family:
            return True
        if self.limit == NOT_MINI:
            return family != MODEL_HD_MINI
        if self.limit == NOT_HD:
            return family not in (MODEL_HD, MODEL_HD_MINI)
        return True


def _r(name, lo=0, hi=10000, **kw):
    return Control(name, RANGE, lo, hi, **kw)


def _b(name, **kw):
    return Control(name, BOOL, **kw)


def _c(name, *choices, **kw):
    return Control(name, CHOICE, choices=tuple(choices), **kw)


def _f(name, **kw):
    return Control(name, FUNCTION, **kw)


_FOOTNOTE_1 = ("device dependent: 0-6 on the 12 8K at protocol 2.0, "
               "0-3 on the Ultimatte 12 at 1.2")

MONITOR_OUT = (
    "Program", "FG", "BG", "Combined Matte", "Internal Matte", "Fill",
    "Layer In", "Background Matte In", "Garbage Matte In",
    "Holdout Matte In", "Layer Matte In", "Processed LM", "Processed HM",
    "Processed GM", "Processed BM", "Screen Correction",
)

LAYER_ORDER = (
    "FG/Layer/BG Layer/BG", "Layer/FG/BG Layer/BG", "Layer/BG Layer/FG/BG",
    "BG Layer/Layer/FG/BG", "BG Layer/FG/Layer/BG", "FG/BG Layer/Layer/BG",
    "FG/BG Layer/BG", "BG Layer/FG/BG", "FG/Layer/BG", "Layer/FG/BG",
)

VIDEO_FORMATS = (
    "Auto Detect", "525i59.94 NTSC 4:3", "625i50 PAL 4:3",
    "720p60", "720p59.94", "720p50",
    "1080i60", "1080i59.94", "1080i50",
    "1080p60", "1080p59.94", "1080p50", "1080p30", "1080p29.97",
    "1080p25", "1080p24", "1080p23.98",
    "1080PsF30", "1080PsF29.97", "1080PsF25", "1080PsF24", "1080PsF23.98",
    "2160p60", "2160p59.94", "2160p50", "2160p30", "2160p29.97",
    "2160p25", "2160p24", "2160p23.98",
    "4320p60", "4320p59.94", "4320p50", "4320p30", "4320p29.97",
    "4320p25", "4320p24", "4320p23.98",
)

# In the document's order, page by page.
CONTROLS: Tuple[Control, ...] = (
    _r("Matte Density"), _r("Black Gloss"), _r("Blue Density"),
    _r("Green Density"), _r("Red Density"), _r("Shadow Level"),
    _r("Shadow Threshold"),
    _r("Matte Correct Horizontal Size", 0, 6),
    _r("Matte Correct Vertical Size", 0, 3),
    _r("Cursor X"), _r("Cursor Y"), _r("Cursor 2 X"), _r("Cursor 2 Y"),
    _r("Veil Master"), _r("Veil Red"), _r("Veil Green"), _r("Veil Blue"),
    _r("Veil Correct Horizontal Size", 0, 6),
    _r("Veil Correct Vertical Size", 0, 6, note=_FOOTNOTE_1),
    _r("Wall Color Red"), _r("Wall Color Green"), _r("Wall Color Blue"),
    _r("Floor Color Red"), _r("Floor Color Green"), _r("Floor Color Blue"),
    _r("Cleanup Level"), _r("Cleanup Dark Recover"),
    _r("Cleanup Light Recover"), _r("Cleanup Strength"),
    _r("GM Cleanup Level"), _r("GM Cleanup Dark Recover"),
    _r("GM Cleanup Light Recover"), _r("GM Cleanup Strength"),
    _r("Correction Level"), _r("Noise Level"), _r("Black Balance"),
    _r("Gray Balance"),
    # page 11
    _r("White Balance"), _r("Flare Level"), _r("Cool"), _r("Skin Tone"),
    _r("Light Warm"), _r("Dark Warm"),
    _r("Flare Correct Horizontal Size", 0, 6),
    _r("Flare Correct Vertical Size", 0, 6, note=_FOOTNOTE_1),
    _r("Ambiance Master"), _r("Ambiance Red"), _r("Ambiance Green"),
    _r("Ambiance Blue"), _r("Ambiance Strength"),
    _r("Direct Light Red"), _r("Direct Light Green"),
    _r("Direct Light Blue"), _r("Direct Light Mix"),
    _r("Vertical Blur"),
    _r("FG Saturation Red"), _r("FG Saturation Green"),
    _r("FG Saturation Blue"), _r("FG Saturation Master"),
    _r("FG Contrast Red"), _r("FG Contrast Green"), _r("FG Contrast Blue"),
    _r("FG Contrast Master"),
    _r("FG Black Red"), _r("FG Black Green"), _r("FG Black Blue"),
    _r("FG Black Master"),
    _r("FG White Red"), _r("FG White Green"), _r("FG White Blue"),
    _r("FG White Master"),
    _r("FG Contrast Crossover"), _r("Fade Mix"),
    _r("BG Saturation Red"),
    # page 12
    _r("BG Saturation Green"), _r("BG Saturation Blue"),
    _r("BG Saturation Master"),
    _r("BG Contrast Red"), _r("BG Contrast Green"), _r("BG Contrast Blue"),
    _r("BG Contrast Master"),
    _r("BG Black Red"), _r("BG Black Green"), _r("BG Black Blue"),
    _r("BG Black Master"),
    _r("BG White Red"), _r("BG White Green"), _r("BG White Blue"),
    _r("BG White Master"),
    _r("BG Contrast Crossover"), _r("BG Filter"),
    _r("Test Signal Master"), _r("Test Signal Red"),
    _r("Test Signal Green"), _r("Test Signal Blue"),
    _r("LY Saturation Red"), _r("LY Saturation Green"),
    _r("LY Saturation Blue"), _r("LY Saturation Master"),
    _r("LY Contrast Red"), _r("LY Contrast Green"), _r("LY Contrast Blue"),
    _r("LY Contrast Master"),
    _r("LY Black Red"), _r("LY Black Green"), _r("LY Black Blue"),
    _r("LY Black Master"),
    _r("LY White Red"), _r("LY White Green"), _r("LY White Blue"),
    _r("LY White Master"),
    # page 13
    _r("LY Contrast Crossover"), _r("LY Filter"),
    _r("LY Test Signal Master"), _r("LY Test Signal Red"),
    _r("LY Test Signal Green"), _r("LY Test Signal Blue"),
    _r("LY Fade Mix"),
    _r("Lighting Level Red"), _r("Lighting Level Green"),
    _r("Lighting Level Blue"), _r("Lighting Level Master"),
    _r("Lighting Minimum Level"),
    _r("Window Position Top", 0, None, window="height"),
    _r("Window Position Bottom", 0, None, window="height"),
    _r("Window Position Left", 0, None, window="width"),
    _r("Window Position Right", 0, None, window="width"),
    _r("Window Softness Top"), _r("Window Softness Bottom"),
    _r("Window Softness Left"), _r("Window Softness Right"),
    _r("Window Skew Top"), _r("Window Skew Bottom"),
    _r("Window Skew Left"), _r("Window Skew Right"),
    _r("Window Skew Offset Top"), _r("Window Skew Offset Bottom"),
    _r("Window Skew Offset Left"), _r("Window Skew Offset Right"),
    _r("Transition Rate", 1, 120),
    _r("BM Process Horizontal", 0, 3), _r("BM Process Vertical", 0, 3),
    _r("BM Filter"), _r("BM Input Level"), _r("BM Input Offset"),
    _r("GM Process Horizontal", 0, 3), _r("GM Process Vertical", 0, 3),
    _r("GM Filter"),
    # page 14
    _r("GM Input Level"), _r("GM Input Offset"),
    _r("HM Process Horizontal", 0, 3), _r("HM Process Vertical", 0, 3),
    _r("HM Filter"), _r("HM Input Level"), _r("HM Input Offset"),
    _r("LM Process Horizontal", 0, 3), _r("LM Process Vertical", 0, 3),
    _r("LM Filter"), _r("LM Input Level"), _r("LM Input Offset"),
    _r("Noise Cursor X"), _r("Noise Cursor Y"),
    _r("FG Input Frame Delay", 0, 14),
    _r("FG Input U Position"), _r("FG Input V Position"),
    _r("FG Input UV Position"),
    _r("Talent Highlight Level", limit=NOT_HD),
    _r("Monitor Highlight Level"), _r("Matte Out Level"),
    _r("Output Offset", -1500, 1500),
    _r("GP Out Delay", 1, 120, limit=NOT_MINI),
    _r("GP 1 Input Delay", 1, 120, limit=NOT_MINI),
    _r("GP 2 Input Delay", 1, 120, limit=NOT_MINI),
    _r("GP 3 Input Delay", 1, 120, limit=NOT_MINI),
    _r("GP 4 Input Delay", 1, 120, limit=NOT_MINI),
    _r("GP 5 Input Delay", 1, 120, limit=NOT_MINI),
    _b("Matte Enable"), _b("Screen Correct"), _b("GM Cleanup Enable"),
    _b("Noise Enable"), _b("Noise Cursor Enable"), _b("FG Freeze"),
    _b("FG Advanced Contrast Enable"), _b("Advanced Flare Enable"),
    _b("HM Flare Enable"),
    # page 15
    _b("Ambiance Enable"), _b("BG Gradient Enable"), _b("BG Freeze"),
    _b("BG Advanced Contrast Enable"), _b("BG Test Signal Enable"),
    _b("LY Input Enable"), _b("LY Advanced Contrast Enable"),
    _b("LY Freeze", limit=NOT_HD), _b("LY Test Signal Enable"),
    _b("Lighting Enable"), _b("Window Enable"), _b("Window BM Enable"),
    _b("Window GM Enable"), _b("Window HM Enable"), _b("Window LM Enable"),
    _b("Window Invert"), _b("Wall Cursor Position Enable"),
    _b("Floor Cursor Position Enable"), _b("Dual Cursor"),
    _b("Manual Color Enable"),
    # The table's "Custom Powerup(deprecated)": the wire name is taken to
    # be without the annotation.
    _b("Custom Powerup", note="deprecated in the document"),
    _b("BM Enable"), _b("BM Invert"), _b("BM Process Invert"),
    _b("BM Freeze", limit=NOT_HD),
    _b("GM Enable"), _b("GM Invert"), _b("GM Process Invert"),
    _b("GM Freeze", limit=NOT_MINI),
    _b("HM Enable"), _b("HM Invert"), _b("HM Process Invert"),
    _b("HM Freeze", limit=NOT_MINI),
    _b("LM Invert"), _b("LM Process Invert"),
    _b("Monitor To Program"), _b("Monitor To Talent", limit=NOT_HD),
    # page 16
    _b("Fill Linear Mix Correction", limit=NOT_MINI),
    _b("Talent Mirror", limit=NOT_HD), _b("Monitor Cascade"),
    _b("Matte Out Invert", limit=NOT_MINI),
    _b("On Air Enable"), _b("On Air Lockout"),
    _b("Matte View Range"), _b("Matte View Invert"),
    _b("Monitor Out RGB"), _b("Monitor Out Red Only"),
    _b("Monitor Out Green Only"), _b("Monitor Out Blue Only"),
    _b("GP Out Save", limit=NOT_MINI),
    _b("Quickload 1"), _b("Quickload 2"), _b("Quickload 3"),
    _b("Quickload 4"), _b("Quickload 5"),
    _b("Quicksave 1"), _b("Quicksave 2"), _b("Quicksave 3"),
    _b("Quicksave 4"), _b("Quicksave 5"),
    _b("GP 1 Input Enable", limit=NOT_MINI),
    _b("GP 2 Input Enable", limit=NOT_MINI),
    _b("GP 3 Input Enable", limit=NOT_MINI),
    _b("GP 4 Input Enable", limit=NOT_MINI),
    _b("GP 5 Input Enable", limit=NOT_MINI),
    _b("GP 1 High Enable", limit=NOT_MINI),
    _b("GP 2 High Enable", limit=NOT_MINI),
    _b("GP 3 High Enable", limit=NOT_MINI),
    _b("GP 4 High Enable", limit=NOT_MINI),
    _b("GP 5 High Enable", limit=NOT_MINI),
    Control("Tally Active", READONLY, note="On/Off {Read Only}"),
    _c("3G SDI level", "A", "B"),
    _c("Color Space", "Rec.709", "Rec.2020", limit=NOT_MINI),
    _c("Filter Mode", "Median", "Average"),
    # page 17
    _c("Filter Median", "0", "1", "2", "3", "4"),
    _c("Filter Average", "0", "1", "2", "3", "4"),
    _c("LY In Mix Mode", "Realistic", "Linear", "Additive"),
    _c("Backing Color", "Red", "Green", "Blue"),
    _c("Cursor Position", "Default", "Last"),
    _c("GP Out Level", "High", "Low", limit=NOT_MINI),
    _c("Output Range", "Normal", "Full", min_protocol=(2, 0)),
    _c("Monitor Out", *MONITOR_OUT),
    _c("Layer Order", *LAYER_ORDER),
    _c("Video Format", *VIDEO_FORMATS,
       note="device dependent: 4320p on the 12 8K only, no PsF on the HD Mini"),
    _f("Factory Defaults"), _f("User Defaults"), _f("Auto Screen Sample"),
    _f("Screen Capture"), _f("Noise Select"), _f("Sample Wall"),
    _f("Sample Floor"), _f("Matte Reset"), _f("Cleanup Reset"),
    _f("GM Cleanup Reset"), _f("FG Color Reset"), _f("BG Color Reset"),
    _f("Flare Reset"),
    # page 18
    _f("Ambiance Reset"), _f("BG Test Signal Color Reset"),
    _f("LY Color Reset"), _f("LY Test Signal Color Reset"),
    _f("Window Reset"), _f("Window Skew Reset"),
    _f("GP Out", limit=NOT_MINI),
)

_BY_KEY: Dict[str, Control] = {c.name.lower(): c for c in CONTROLS}


def lookup(name: str) -> Optional[Control]:
    """The documented control called ``name`` (any case), or None."""
    return _BY_KEY.get(" ".join((name or "").split()).lower())


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------

_TRUE = {"on", "true", "yes", "1"}
_FALSE = {"off", "false", "no", "0"}


def parse_bool(text: str) -> Optional[bool]:
    t = (text or "").strip().lower()
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    return None


def parse_int(text: str) -> Optional[int]:
    t = (text or "").strip()
    if t.startswith("+"):
        t = t[1:]
    try:
        return int(t)
    except ValueError:
        return None


def parse_value(name: str, text: str):
    """A CONTROL line's value, typed by the table.

    ``range`` gives an int, ``bool`` and ``readonly`` a bool, ``function``
    the bool of its ``Yes`` (True once the unit says it is done), ``choice``
    the string as the unit sent it. A value that doesn't parse as its kind,
    and every control not in the table, stays the raw string: reading never
    fails on an odd unit."""
    control = lookup(name)
    raw = (text or "").strip()
    if control is None:
        return raw
    if control.kind == RANGE:
        v = parse_int(raw)
    elif control.kind in (BOOL, READONLY, FUNCTION):
        v = parse_bool(raw)
    else:
        return raw
    return raw if v is None else v


# frame size per format family: "1080" -> (1920, 1080)
_FRAME = {"525": (720, 486), "625": (720, 576), "720": (1280, 720),
          "1080": (1920, 1080), "2160": (3840, 2160), "4320": (7680, 4320)}


def frame_size(video_format: str) -> Optional[Tuple[int, int]]:
    """(width, height) of a format name like ``1080p25`` or ``2160p59.94``,
    or None (``auto detect``, or a name not recognised)."""
    m = re.match(r"\s*(\d{3,4})", video_format or "")
    return _FRAME.get(m.group(1)) if m else None


class ControlValueError(ValueError):
    """A value the document says the control can't take, refused before it
    reaches the wire."""


class UnsupportedError(ValueError):
    """A control, block or command the document says this model (or this
    protocol version) doesn't have, refused before it reaches the wire."""


@dataclass
class Context:
    """What validation needs to know about the unit."""
    family: str = MODEL_UNKNOWN
    protocol: Optional[Tuple[int, int]] = None
    video_format: str = ""
    video_formats: Tuple[str, ...] = field(default_factory=tuple)


def format_value(control: Control, value, ctx: Optional[Context] = None) -> str:
    """Check ``value`` against ``control`` and return it as the wire text.

    Raises ControlValueError / UnsupportedError. Booleans go out as
    ``on``/``off`` and choices in lower case, as the unit reports them;
    the document's own Telnet examples send ``blue`` and ``yes`` the same
    way."""
    ctx = ctx or Context()
    if not control.available_on(ctx.family):
        raise UnsupportedError(
            f"{control.name!r} is not available on the {ctx.family} "
            f"(protocol document, footnote {control.limit})")
    if (control.min_protocol and ctx.protocol
            and ctx.protocol < control.min_protocol):
        raise UnsupportedError(
            f"{control.name!r} needs protocol "
            f"{'.'.join(map(str, control.min_protocol))} or later; the unit "
            f"speaks {'.'.join(map(str, ctx.protocol))}")
    if control.kind == READONLY:
        raise ControlValueError(f"{control.name!r} is read only")
    if control.kind == FUNCTION:
        if value not in (True, None) and parse_bool(str(value)) is not True:
            raise ControlValueError(
                f"{control.name!r} is a function: the only value is yes")
        return "yes"
    if control.kind == BOOL:
        v = value if isinstance(value, bool) else parse_bool(str(value))
        if v is None:
            raise ControlValueError(f"{control.name!r} is on/off, not {value!r}")
        return "on" if v else "off"
    if control.kind == RANGE:
        if isinstance(value, bool) or not isinstance(value, int):
            v = parse_int(str(value))
            if v is None:
                raise ControlValueError(
                    f"{control.name!r} takes an integer, not {value!r}")
        else:
            v = value
        lo, hi = control.minimum, maximum_for(control, ctx)
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            raise ControlValueError(
                f"{control.name!r} is {lo}-{hi if hi is not None else '?'}, "
                f"not {v}")
        return str(v)
    # CHOICE
    text = " ".join(str(value).split())
    allowed = list(control.choices)
    if control.name == "Video Format":
        allowed = video_formats_for(ctx)
    for choice in allowed:
        if choice.lower() == text.lower():
            return choice.lower()
    raise ControlValueError(
        f"{control.name!r} is one of {', '.join(allowed)}; not {value!r}")


def maximum_for(control: Control, ctx: Context) -> Optional[int]:
    if not control.window:
        return control.maximum
    size = frame_size(ctx.video_format)
    if size is None:
        return None
    return size[1] if control.window == "height" else size[0]


def video_formats_for(ctx: Context):
    """The formats this unit can take: its own VIDEO FORMATS list when it
    sent one, else the document's list cut by footnote 4."""
    if ctx.video_formats:
        return list(ctx.video_formats)
    out = []
    for f in VIDEO_FORMATS:
        if f.startswith("4320") and ctx.family and ctx.family != MODEL_8K:
            continue
        if "PsF" in f and ctx.family == MODEL_HD_MINI:
            continue
        out.append(f)
    return out


def format_offset(control: Control, delta, ctx: Optional[Context] = None) -> str:
    """Check a relative change: "Only controls with numerical ranges
    support this relative mode". The unit clamps (or refuses) the result;
    nothing here can know the value it applies to at that instant."""
    ctx = ctx or Context()
    if control.kind != RANGE:
        raise ControlValueError(
            f"{control.name!r} has no numerical range; Offset needs one")
    if not control.available_on(ctx.family):
        raise UnsupportedError(
            f"{control.name!r} is not available on the {ctx.family}")
    if isinstance(delta, bool) or not isinstance(delta, int):
        v = parse_int(str(delta))
        if v is None:
            raise ControlValueError(f"an offset is an integer, not {delta!r}")
        delta = v
    return str(delta)
