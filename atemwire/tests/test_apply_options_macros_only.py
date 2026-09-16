# SPDX-License-Identifier: LGPL-3.0-only
"""Unit tests for ``ApplyOptions.macros_only()``.

The helper exists so a macro-swap upload job can express
"exactly the macros, never anything else" at a single call site without
having to enumerate every field in ApplyOptions inline. The constructor
defaults are tuned for the switcher-to-switcher clone case — most flags
default True — so the macros-only intent must explicitly disable every
other section. This test pins that behaviour so a future field added to
ApplyOptions with default True doesn't silently turn on for the
macro-swap caller.
"""

from dataclasses import fields

from atemwire.profile.options import ApplyOptions, MixEffectOptions


def test_macros_only_enables_only_restore_macros():
    """All boolean fields except restore_macros must be False, and
    every per-M/E block must be fully deselected."""
    opts = ApplyOptions.macros_only()

    bool_field_names = [
        f.name for f in fields(opts)
        if f.name not in ('mes',)
    ]
    for name in bool_field_names:
        value = getattr(opts, name)
        if name == 'restore_macros':
            assert value is True, "restore_macros should be the only True field"
        else:
            assert value is False, f"{name} should be False but is {value!r}"

    for me_opts in opts.mes:
        assert not me_opts.any_selected()
        assert me_opts.usk == [False, False, False, False]


def test_macros_only_returns_fresh_instance():
    """Each call returns a new ApplyOptions instance so callers can
    safely mutate the returned object without affecting subsequent
    callers (defends against shared aliasing on mes / usk lists)."""
    a = ApplyOptions.macros_only()
    b = ApplyOptions.macros_only()
    assert a is not b
    assert a.mes is not b.mes
    assert a.mes[0] is not b.mes[0]
    assert a.mes[0].usk is not b.mes[0].usk

    a.mes[0].usk[0] = True
    assert b.mes[0].usk == [False, False, False, False]


def test_macros_only_lists_every_apply_options_field():
    """If a future commit adds a new field to ApplyOptions, this test
    fails so the author has to explicitly decide what macros_only()
    should set it to. Prevents silent inheritance of a True default for
    the macro-swap caller."""
    expected_fields = {
        'mes',
        'restore_multiview', 'restore_camera_control', 'restore_hyperdecks',
        'restore_talkback',
        'restore_fly_keyframes',
        'restore_downstream_keys', 'restore_color_generators',
        'restore_audio', 'restore_video_mode', 'restore_inputs',
        'restore_macros', 'restore_aux', 'restore_media_players',
        'restore_settings_flags', 'restore_media_pool_images',
    }
    actual_fields = {f.name for f in fields(ApplyOptions)}
    assert actual_fields == expected_fields, (
        "ApplyOptions fields changed. Update ApplyOptions.macros_only() "
        "to explicitly set the new field, then update this test's "
        f"expected_fields set. Diff: {actual_fields ^ expected_fields}"
    )


def test_mix_effect_options_lists_every_field():
    """Same future-field guard for the per-M/E block: macros_only()
    relies on MixEffectOptions.none() deselecting everything, so a new
    per-M/E flag must be accounted for there."""
    expected_fields = {'program', 'preview', 'next_transition',
                       'transition_style', 'fade_to_black', 'usk'}
    actual_fields = {f.name for f in fields(MixEffectOptions)}
    assert actual_fields == expected_fields, (
        "MixEffectOptions fields changed. Update MixEffectOptions.none() / "
        "any_selected() and this test. "
        f"Diff: {actual_fields ^ expected_fields}"
    )


def test_default_apply_options_does_not_disable_macros():
    """Sanity check: a default ApplyOptions has restore_macros=True.
    Our macros_only() change shouldn't have accidentally flipped that."""
    opts = ApplyOptions()
    assert opts.restore_macros is True
