# SPDX-License-Identifier: LGPL-3.0-only
"""Wipe-pattern name↔wire mapping, pinned to a live ASC capture.

The wipe-pattern enum has two copies in the tree: the profile table
(``atemwire/profile/_enums.py`` ``WIPE_PATTERN_NAMES``, driving ``<WipeParameters
pattern>`` / ``<PatternParameters style>`` on profile save+apply) and the macro
codec table (``atemwire/macrotransfer/_helpers.py`` ``_WIPE_PATTERN_NAMES``, op
0x007d). They MUST agree with each other and with what ATEM Software Control
actually writes in its "Save Switcher State" XML.

Wire 0/1/2/3 were captured live 2026-08-31 (one Save Switcher State per pattern
on the test switcher; the ``<WipeParameters pattern="...">`` strings below are
verbatim from those XMLs), and wire 6 was cross-checked from the same XMLs' USK
``<PatternParameters style="DiamondIris">``. Before the fix BOTH tables were
wrong at 0-1 and the profile table was wrong at 0-3.
"""
from atemwire.profile._enums import WIPE_PATTERN_NAMES, WIPE_PATTERN_TO_INT
from atemwire.macrotransfer._helpers import (
    _WIPE_PATTERN_NAMES as MACRO_WIPE_NAMES,
    _WIPE_PATTERN_TO_INT as MACRO_WIPE_TO_INT,
)

# Verbatim from the ASC Save Switcher State XMLs (captured 2026-08-31).
ASC_CAPTURED = {
    0: 'LeftToRightBar',
    1: 'TopToBottomBar',
    2: 'HorizontalBarnDoor',   # also the earliest live pin
    3: 'VerticalBarnDoor',
    6: 'DiamondIris',          # from the USK PatternParameters in the same XMLs
}


def test_profile_table_matches_asc_capture():
    for wire, name in ASC_CAPTURED.items():
        assert WIPE_PATTERN_NAMES[wire] == name, (
            f'profile wipe name at wire {wire} is '
            f'{WIPE_PATTERN_NAMES[wire]!r}, ASC XML says {name!r}')


def test_macro_table_matches_asc_capture():
    for wire, name in ASC_CAPTURED.items():
        assert MACRO_WIPE_NAMES[wire] == name, (
            f'macro wipe name at wire {wire} is '
            f'{MACRO_WIPE_NAMES[wire]!r}, ASC XML says {name!r}')


def test_the_two_tables_agree_across_all_18():
    # Both are 18-long (0..17) and identical — this is what stops one from
    # drifting away from the other (the exact divergence found and fixed).
    assert len(WIPE_PATTERN_NAMES) == 18
    assert set(MACRO_WIPE_NAMES) == set(range(18))
    for i in range(18):
        assert WIPE_PATTERN_NAMES[i] == MACRO_WIPE_NAMES[i], (
            f'tables diverge at wire {i}: profile={WIPE_PATTERN_NAMES[i]!r} '
            f'macro={MACRO_WIPE_NAMES[i]!r}')


def test_name_int_roundtrip_for_captured():
    for wire, name in ASC_CAPTURED.items():
        assert WIPE_PATTERN_TO_INT[name] == wire
        assert MACRO_WIPE_TO_INT[name] == wire


def test_no_phantom_names():
    # Names that used to be in one table or the doc but ASC never emits.
    for bogus in ('HorizontalBars', 'VerticalBars', 'HorizontalBar',
                  'VerticalBar', 'LeftSlash'):
        assert bogus not in WIPE_PATTERN_TO_INT
        assert bogus not in MACRO_WIPE_TO_INT
