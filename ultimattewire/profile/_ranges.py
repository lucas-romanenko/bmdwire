# SPDX-License-Identifier: MIT
"""
Per-parameter display ranges and the annotated-preamble helper. Used by
profile/archive.py to emit the bonus human-readable .txt dump alongside
the binary zip. Source: Ultimatte 12 Operations Manual (Feb 2026) +
Smart Remote 4 panel screenshots.
"""


# Maps protocol parameter name -> (raw_min, raw_max, display_min,
#                                  display_default, display_max, unit_label).
# If display_min is None, the value is shown as-is (e.g. pixel counts,
# frame counts).
RANGES = {
    # Matte
    "Matte Density":                (0, 10000, -100, 0, 300, "%"),
    "Black Gloss":                  (0, 10000, 0, 0, 100, "%"),
    "Blue Density":                 (0, 10000, 0, 0, 100, "%"),
    "Green Density":                (0, 10000, 0, 0, 100, "%"),
    "Red Density":                  (0, 10000, 0, 0, 100, "%"),
    "Shadow Level":                 (0, 10000, 0, 0, 100, "%"),
    "Shadow Threshold":             (0, 10000, 0, 0, 100, "%"),
    "Matte Correct Horizontal Size": (0, 6, None, None, None, "px"),
    "Matte Correct Vertical Size":  (0, 3, None, None, None, "lines"),

    # Cursor positions
    "Cursor X":                     (0, 10000, 0, 0, 100, "%"),
    "Cursor Y":                     (0, 10000, 0, 0, 100, "%"),
    "Cursor 2 X":                   (0, 10000, 0, 0, 100, "%"),
    "Cursor 2 Y":                   (0, 10000, 0, 0, 100, "%"),

    # Veil
    "Veil Master":                  (0, 10000, 0, 0, 100, "%"),
    "Veil Red":                     (0, 10000, 0, 0, 100, "%"),
    "Veil Green":                   (0, 10000, 0, 0, 100, "%"),
    "Veil Blue":                    (0, 10000, 0, 0, 100, "%"),
    "Veil Correct Horizontal Size": (0, 6, None, None, None, "px"),
    "Veil Correct Vertical Size":   (0, 6, None, None, None, "lines"),

    # Wall/floor sample colors
    "Wall Color Red":               (0, 10000, 0, 0, 100, "%"),
    "Wall Color Green":             (0, 10000, 0, 0, 100, "%"),
    "Wall Color Blue":              (0, 10000, 0, 0, 100, "%"),
    "Floor Color Red":              (0, 10000, 0, 0, 100, "%"),
    "Floor Color Green":            (0, 10000, 0, 0, 100, "%"),
    "Floor Color Blue":             (0, 10000, 0, 0, 100, "%"),

    # Clean Up
    "Cleanup Level":                (0, 10000, 0, 0, 100, "%"),
    "Cleanup Dark Recover":         (0, 10000, 0, 0, 100, "%"),
    "Cleanup Light Recover":        (0, 10000, 0, 0, 100, "%"),
    "Cleanup Strength":             (0, 10000, 0, 0, 100, "%"),
    "GM Cleanup Level":             (0, 10000, 0, 0, 100, "%"),
    "GM Cleanup Dark Recover":      (0, 10000, 0, 0, 100, "%"),
    "GM Cleanup Light Recover":     (0, 10000, 0, 0, 100, "%"),
    "GM Cleanup Strength":          (0, 10000, 0, 0, 100, "%"),

    # Filter
    "Correction Level":             (0, 10000, 0, 0, 100, "%"),
    "Noise Level":                  (0, 10000, 0, 0, 100, "%"),

    # Flare 1 / Flare 2
    "Black Balance":                (0, 10000, -100, 0, 100, "%"),
    "Gray Balance":                 (0, 10000, -100, 0, 100, "%"),
    "White Balance":                (0, 10000, -100, 0, 100, "%"),
    "Flare Level":                  (0, 10000, 0, 0, 100, "%"),
    "Cool":                         (0, 10000, 0, 0, 100, "%"),
    "Skin Tone":                    (0, 10000, 0, 0, 100, "%"),
    "Light Warm":                   (0, 10000, 0, 0, 100, "%"),
    "Dark Warm":                    (0, 10000, 0, 0, 100, "%"),
    "Flare Correct Horizontal Size": (0, 6, None, None, None, "px"),
    "Flare Correct Vertical Size":  (0, 6, None, None, None, "lines"),

    # Ambiance
    "Ambiance Master":              (0, 10000, 0, 0, 100, "%"),
    "Ambiance Red":                 (0, 10000, 0, 0, 100, "%"),
    "Ambiance Green":               (0, 10000, 0, 0, 100, "%"),
    "Ambiance Blue":                (0, 10000, 0, 0, 100, "%"),
    "Ambiance Strength":            (0, 10000, 0, 0, 100, "%"),
    "Direct Light Red":             (0, 10000, 0, 0, 100, "%"),
    "Direct Light Green":           (0, 10000, 0, 0, 100, "%"),
    "Direct Light Blue":            (0, 10000, 0, 0, 100, "%"),
    "Direct Light Mix":             (0, 10000, 0, 0, 100, "%"),
    "Vertical Blur":                (0, 10000, 0, 0, 100, "%"),

    # Foreground color
    "FG Saturation Red":            (0, 10000, 0, 100, 200, "%"),
    "FG Saturation Green":          (0, 10000, 0, 100, 200, "%"),
    "FG Saturation Blue":           (0, 10000, 0, 100, 200, "%"),
    "FG Saturation Master":         (0, 10000, 0, 100, 200, "%"),
    "FG Contrast Red":              (0, 10000, 0, 100, 200, "%"),
    "FG Contrast Green":            (0, 10000, 0, 100, 200, "%"),
    "FG Contrast Blue":             (0, 10000, 0, 100, 200, "%"),
    "FG Contrast Master":           (0, 10000, 0, 100, 200, "%"),
    "FG Black Red":                 (0, 10000, -100, 0, 100, "%"),
    "FG Black Green":               (0, 10000, -100, 0, 100, "%"),
    "FG Black Blue":                (0, 10000, -100, 0, 100, "%"),
    "FG Black Master":              (0, 10000, -100, 0, 100, "%"),
    "FG White Red":                 (0, 10000, 0, 100, 200, "%"),
    "FG White Green":               (0, 10000, 0, 100, 200, "%"),
    "FG White Blue":                (0, 10000, 0, 100, 200, "%"),
    "FG White Master":              (0, 10000, 0, 100, 200, "%"),
    "FG Contrast Crossover":        (0, 10000, 0, 50, 100, "%"),
    "Fade Mix":                     (0, 10000, 0, 100, 100, "%"),

    # Background color
    "BG Saturation Red":            (0, 10000, 0, 100, 200, "%"),
    "BG Saturation Green":          (0, 10000, 0, 100, 200, "%"),
    "BG Saturation Blue":           (0, 10000, 0, 100, 200, "%"),
    "BG Saturation Master":         (0, 10000, 0, 100, 200, "%"),
    "BG Contrast Red":              (0, 10000, 0, 100, 200, "%"),
    "BG Contrast Green":            (0, 10000, 0, 100, 200, "%"),
    "BG Contrast Blue":             (0, 10000, 0, 100, 200, "%"),
    "BG Contrast Master":           (0, 10000, 0, 100, 200, "%"),
    "BG Black Red":                 (0, 10000, -100, 0, 100, "%"),
    "BG Black Green":               (0, 10000, -100, 0, 100, "%"),
    "BG Black Blue":                (0, 10000, -100, 0, 100, "%"),
    "BG Black Master":              (0, 10000, -100, 0, 100, "%"),
    "BG White Red":                 (0, 10000, 0, 100, 200, "%"),
    "BG White Green":               (0, 10000, 0, 100, 200, "%"),
    "BG White Blue":                (0, 10000, 0, 100, 200, "%"),
    "BG White Master":              (0, 10000, 0, 100, 200, "%"),
    "BG Contrast Crossover":        (0, 10000, 0, 50, 100, "%"),
    "BG Filter":                    (0, 10000, 0, 0, 100, "%"),
    "Test Signal Master":           (0, 10000, 0, 0, 100, "%"),
    "Test Signal Red":              (0, 10000, 0, 0, 100, "%"),
    "Test Signal Green":            (0, 10000, 0, 0, 100, "%"),
    "Test Signal Blue":             (0, 10000, 0, 0, 100, "%"),

    # Layer color
    "LY Saturation Red":            (0, 10000, 0, 100, 200, "%"),
    "LY Saturation Green":          (0, 10000, 0, 100, 200, "%"),
    "LY Saturation Blue":           (0, 10000, 0, 100, 200, "%"),
    "LY Saturation Master":         (0, 10000, 0, 100, 200, "%"),
    "LY Contrast Red":              (0, 10000, 0, 100, 200, "%"),
    "LY Contrast Green":            (0, 10000, 0, 100, 200, "%"),
    "LY Contrast Blue":             (0, 10000, 0, 100, 200, "%"),
    "LY Contrast Master":           (0, 10000, 0, 100, 200, "%"),
    "LY Black Red":                 (0, 10000, -100, 0, 100, "%"),
    "LY Black Green":               (0, 10000, -100, 0, 100, "%"),
    "LY Black Blue":                (0, 10000, -100, 0, 100, "%"),
    "LY Black Master":              (0, 10000, -100, 0, 100, "%"),
    "LY White Red":                 (0, 10000, 0, 100, 200, "%"),
    "LY White Green":               (0, 10000, 0, 100, 200, "%"),
    "LY White Blue":                (0, 10000, 0, 100, 200, "%"),
    "LY White Master":              (0, 10000, 0, 100, 200, "%"),
    "LY Contrast Crossover":        (0, 10000, 0, 50, 100, "%"),
    "LY Filter":                    (0, 10000, 0, 0, 100, "%"),
    "LY Test Signal Master":        (0, 10000, 0, 0, 100, "%"),
    "LY Test Signal Red":           (0, 10000, 0, 0, 100, "%"),
    "LY Test Signal Green":         (0, 10000, 0, 0, 100, "%"),
    "LY Test Signal Blue":          (0, 10000, 0, 0, 100, "%"),
    "LY Fade Mix":                  (0, 10000, 0, 100, 100, "%"),

    # Lighting
    "Lighting Level Red":           (0, 10000, 0, 100, 200, "%"),
    "Lighting Level Green":         (0, 10000, 0, 100, 200, "%"),
    "Lighting Level Blue":          (0, 10000, 0, 100, 200, "%"),
    "Lighting Level Master":        (0, 10000, 0, 100, 200, "%"),
    "Lighting Minimum Level":       (0, 10000, 0, 25, 100, "%"),

    # Window
    "Window Position Top":          (0, 10000, 0, 0, 100, "%"),
    "Window Position Bottom":       (0, 10000, 0, 0, 100, "%"),
    "Window Position Left":         (0, 10000, 0, 0, 100, "%"),
    "Window Position Right":        (0, 10000, 0, 0, 100, "%"),
    "Window Softness Top":          (0, 10000, 0, 0, 100, "%"),
    "Window Softness Bottom":       (0, 10000, 0, 0, 100, "%"),
    "Window Softness Left":         (0, 10000, 0, 0, 100, "%"),
    "Window Softness Right":        (0, 10000, 0, 0, 100, "%"),
    "Window Skew Top":              (0, 10000, 0, 0, 100, "%"),
    "Window Skew Bottom":           (0, 10000, 0, 0, 100, "%"),
    "Window Skew Left":             (0, 10000, 0, 0, 100, "%"),
    "Window Skew Right":            (0, 10000, 0, 0, 100, "%"),
    "Window Skew Offset Top":       (0, 10000, 0, 0, 100, "%"),
    "Window Skew Offset Bottom":    (0, 10000, 0, 0, 100, "%"),
    "Window Skew Offset Left":     (0, 10000, 0, 0, 100, "%"),
    "Window Skew Offset Right":    (0, 10000, 0, 0, 100, "%"),

    # Transition rate
    "Transition Rate":              (1, 120, None, None, None, "frames"),

    # Matte Input processing
    "BM Process Horizontal":        (0, 3, None, None, None, "px"),
    "BM Process Vertical":          (0, 3, None, None, None, "lines"),
    "BM Filter":                    (0, 10000, 0, 0, 100, "%"),
    "BM Input Level":               (0, 10000, 0, 100, 200, "%"),
    "BM Input Offset":              (0, 10000, -100, 0, 100, "%"),
    "GM Process Horizontal":        (0, 3, None, None, None, "px"),
    "GM Process Vertical":          (0, 3, None, None, None, "lines"),
    "GM Filter":                    (0, 10000, 0, 0, 100, "%"),
    "GM Input Level":               (0, 10000, 0, 100, 200, "%"),
    "GM Input Offset":              (0, 10000, -100, 0, 100, "%"),
    "HM Process Horizontal":        (0, 3, None, None, None, "px"),
    "HM Process Vertical":          (0, 3, None, None, None, "lines"),
    "HM Filter":                    (0, 10000, 0, 0, 100, "%"),
    "HM Input Level":               (0, 10000, 0, 100, 200, "%"),
    "HM Input Offset":              (0, 10000, -100, 0, 100, "%"),
    "LM Process Horizontal":        (0, 3, None, None, None, "px"),
    "LM Process Vertical":          (0, 3, None, None, None, "lines"),
    "LM Filter":                    (0, 10000, 0, 0, 100, "%"),
    "LM Input Level":               (0, 10000, 0, 100, 200, "%"),
    "LM Input Offset":              (0, 10000, -100, 0, 100, "%"),

    # Noise cursor
    "Noise Cursor X":               (0, 10000, 0, 0, 100, "%"),
    "Noise Cursor Y":               (0, 10000, 0, 0, 100, "%"),

    # FG input timing
    "FG Input Frame Delay":         (0, 14, None, None, None, "frames"),
    "FG Input U Position":          (0, 10000, -2, 0, 2, "px"),
    "FG Input V Position":          (0, 10000, -2, 0, 2, "px"),
    "FG Input UV Position":         (0, 10000, -2, 0, 2, "px"),

    # Highlight levels
    "Talent Highlight Level":       (0, 10000, 0, 0, 100, "%"),
    "Monitor Highlight Level":      (0, 10000, 0, 0, 100, "%"),

    # Matte output level
    "Matte Out Level":              (0, 10000, 0, 100, 100, "%"),

    # Output offset
    "Output Offset":                (-1500, 1500, None, None, None, "subpx"),

    # GPI delays
    "GP Out Delay":                 (1, 120, None, None, None, "frames"),
    "GP 1 Input Delay":             (1, 120, None, None, None, "frames"),
    "GP 2 Input Delay":             (1, 120, None, None, None, "frames"),
    "GP 3 Input Delay":             (1, 120, None, None, None, "frames"),
    "GP 4 Input Delay":             (1, 120, None, None, None, "frames"),
    "GP 5 Input Delay":             (1, 120, None, None, None, "frames"),
}


def to_display(name, raw_value):
    """Convert raw protocol value to (display_value, unit). Returns
    (None, None) if the param has no range entry or the value isn't
    numeric."""
    if name not in RANGES:
        return None, None
    try:
        raw = float(raw_value)
    except (TypeError, ValueError):
        return None, None
    rmin, rmax, dmin, _ddef, dmax, unit = RANGES[name]
    if dmin is None:
        return raw, unit
    if rmax == rmin:
        return dmin, unit
    pct = (raw - rmin) / (rmax - rmin)
    return dmin + pct * (dmax - dmin), unit


def annotate_preamble(preamble):
    """Walk the preamble line-by-line and append (display) annotations
    to matched CONTROL lines. Returns the annotated string."""
    out = []
    in_control = False
    for line in preamble.splitlines():
        stripped = line.rstrip("\r")
        if stripped.endswith(":") and not stripped.startswith(" "):
            header = stripped[:-1]
            in_control = header in ("CONTROL", "CONTROL DEFAULT")
            out.append(stripped)
            continue
        if not in_control or ":" not in stripped:
            out.append(stripped)
            continue
        name, _, value = stripped.partition(":")
        name = name.strip()
        value = value.strip()
        lookup_name = name[7:] if name.startswith("Offset ") else name
        display, unit = to_display(lookup_name, value)
        if display is None:
            out.append(stripped)
            continue
        num = str(int(display))
        sep = "" if unit == "%" else " "
        annot = f"{num}{sep}{unit}"
        left = f"{name}: {value}"
        out.append(f"{left:<40} ({annot})")
    return "\n".join(out) + "\n"
