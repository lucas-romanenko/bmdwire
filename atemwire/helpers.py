# SPDX-License-Identifier: LGPL-3.0-only
"""
ATEM protocol helpers — rate parsing/formatting, transition-selection bitmask,
keyframe and fly-key location enum lookup.

Rate strings are the ``seconds:frames`` format the ATEM uses on the wire for
transition rates, fade-to-black durations, and downstream keyer rates. Parsing
and formatting both need the switcher's display FPS (ASC convention: 59.94→30,
50→25, etc.) — resolve it via ``atemwire.state.display_fps(mixerstate)`` and
pass it in, or the ``FALLBACK_FPS`` of 25 will be used during the handshake
window before the first ``video-mode`` packet arrives.
"""



# Fallback FPS used only when mixerstate doesn't have a video-mode yet (e.g.
# during the handshake window before the first VidM packet arrives).
# Real FPS is read per-call from the connection's mixerstate via
# ``atemwire.state.display_fps()``; callers should always pass the resolved FPS
# when they have a connection handy.
FALLBACK_FPS = 25


# ---------------------------------------------------------------------------
# Rate parsing & formatting
# ---------------------------------------------------------------------------

def parse_rate(rate_str, fps: int = FALLBACK_FPS) -> int:
    """Parse a rate string into total frames at the given FPS.

    Accepts two forms:
      - ``'seconds:frames'`` (e.g. ``'1:12'``) — what the live UI sends;
      - a bare frame count (e.g. ``'50'``) — what ``profile.apply`` passes
        (it flattens the saved wire frame value to a plain int string).

    FPS is the ATEM's display FPS (ASC convention: HALF the field rate for doubled
    modes — 59.94→30, 50→25, 23.98→24; see the module header + ``state.display_fps``).
    Callers should resolve it from mixerstate via ``display_fps()`` and pass
    it in explicitly; the fallback only exists for the handshake window.

    Genuinely malformed input (None, ``'1:2:3'``, ``'abc'``) falls back to
    ``fps``."""
    if fps <= 0:
        fps = FALLBACK_FPS
    try:
        if ':' in rate_str:
            seconds, frames = map(int, rate_str.split(":"))
            return seconds * fps + frames
        return int(rate_str)          # bare frame count (what apply passes)
    except (ValueError, TypeError, AttributeError):
        return fps                    # malformed / None -> safe default


def format_rate(rate_frames, fps: int = FALLBACK_FPS) -> str:
    """Format total frames as 'seconds:frames' at the given FPS.

    FPS is the ATEM's display FPS. Callers should resolve it from mixerstate
    via ``display_fps()`` and pass it in explicitly."""
    if fps <= 0:
        fps = FALLBACK_FPS
    try:
        rate_frames = int(rate_frames)
        return f"{rate_frames // fps}:{rate_frames % fps:02d}"
    except Exception:
        return f"1:{0:02d}"


# ---------------------------------------------------------------------------
# Transition selection bitmask (used by TransitionSettingsCommand)
# ---------------------------------------------------------------------------

# Bits for the ``next_transition`` mask in the TrSS command.
TRANSITION_BIT_BACKGROUND = 1 << 0
TRANSITION_BIT_KEY1 = 1 << 1
TRANSITION_BIT_KEY2 = 1 << 2
TRANSITION_BIT_KEY3 = 1 << 3
TRANSITION_BIT_KEY4 = 1 << 4


def transition_mask_from_selection(selection: dict) -> int:
    """Pack a {background, key1, key2, key3, key4} dict into the TrSS mask."""
    mask = 0
    if selection.get('background'):
        mask |= TRANSITION_BIT_BACKGROUND
    if selection.get('key1'):
        mask |= TRANSITION_BIT_KEY1
    if selection.get('key2'):
        mask |= TRANSITION_BIT_KEY2
    if selection.get('key3'):
        mask |= TRANSITION_BIT_KEY3
    if selection.get('key4'):
        mask |= TRANSITION_BIT_KEY4
    return mask


# ---------------------------------------------------------------------------
# Keyframe enum
# ---------------------------------------------------------------------------

# atemwire's keyframe enum values — A=1, B=2, Full=3, RunToInfinite=4.
# PyATEMMax's legacy strings, preserved so the frontend payload doesn't change.
KEYFRAME_BY_NAME = {
    'a': 1,
    'b': 2,
    'full': 3,
    'runToInfinite': 4,
}


def keyframe_constant(name: str):
    """Map a frontend keyframe string to atemwire's enum int. Returns None for
    unknown names."""
    return KEYFRAME_BY_NAME.get(name)


# ---------------------------------------------------------------------------
# Fly-key location enum
# ---------------------------------------------------------------------------

# Where a flying key runs to on Run to Infinity, by ATEM Software Control's
# name. One enum on both paths: byte 5 of the live RFlK command (which KeFS
# byte 5 echoes) and the location byte of macro op 0x0056
# FlyKeyRunToInfinity. 1-9 are the 3x3 grid row by row from the top left, so
# 5 is MiddleCentre; 0 is CentreOfKey, the tenth location and a different one.
# Captured 2026-10-13 on a 1 M/E Constellation HD (bmdwire#10): ASC's RFlK for
# each of the ten carried these bytes, and the same ten runs recorded as a
# macro downloaded as these location bytes, which ASC's Save Switcher State
# named as below.
FLYKEY_LOCATION_NAMES = {
    0: 'CentreOfKey',
    1: 'TopLeft',
    2: 'TopCentre',
    3: 'TopRight',
    4: 'MiddleLeft',
    5: 'MiddleCentre',
    6: 'MiddleRight',
    7: 'BottomLeft',
    8: 'BottomCentre',
    9: 'BottomRight',
}

FLYKEY_LOCATION_BY_NAME = {v: k for k, v in FLYKEY_LOCATION_NAMES.items()}


def flykey_location(location):
    """Map a fly-key location to its wire byte: one of ASC's names in
    ``FLYKEY_LOCATION_NAMES``, or a number (int or numeric string), returned
    as an int unchecked. Returns None for anything else."""
    if isinstance(location, str) and location in FLYKEY_LOCATION_BY_NAME:
        return FLYKEY_LOCATION_BY_NAME[location]
    try:
        return int(location)
    except (TypeError, ValueError):
        return None
