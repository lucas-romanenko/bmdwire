# SPDX-License-Identifier: MIT
"""The control table against the protocol document, and value checks."""

import pytest

from ultimattewire import controls as C


def test_the_table_has_every_control_in_the_document():
    # Pages 10-18 of the document: 289 rows.
    assert len(C.CONTROLS) == 289
    assert len({c.name.lower() for c in C.CONTROLS}) == 289
    kinds = {}
    for c in C.CONTROLS:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
    assert kinds == {C.RANGE: 176, C.BOOL: 79, C.READONLY: 1,
                     C.CHOICE: 13, C.FUNCTION: 20}


@pytest.mark.parametrize("name,lo,hi", [
    ("Matte Density", 0, 10000), ("Matte Correct Horizontal Size", 0, 6),
    ("Matte Correct Vertical Size", 0, 3), ("Veil Correct Vertical Size", 0, 6),
    ("Transition Rate", 1, 120), ("BM Process Horizontal", 0, 3),
    ("FG Input Frame Delay", 0, 14), ("Output Offset", -1500, 1500),
    ("GP 5 Input Delay", 1, 120),
])
def test_documented_ranges(name, lo, hi):
    c = C.lookup(name)
    assert (c.kind, c.minimum, c.maximum) == (C.RANGE, lo, hi)


def test_footnote_limits():
    assert C.lookup("GP Out Delay").limit == C.NOT_MINI
    assert C.lookup("Talent Mirror").limit == C.NOT_HD
    assert C.lookup("Talent Highlight Level").limit == C.NOT_HD
    assert C.lookup("GM Freeze").limit == C.NOT_MINI
    assert C.lookup("Output Range").min_protocol == (2, 0)
    assert C.lookup("Tally Active").kind == C.READONLY
    assert not C.lookup("GP Out").available_on(C.MODEL_HD_MINI)
    assert C.lookup("GP Out").available_on(C.MODEL_HD)
    assert not C.lookup("LY Freeze").available_on(C.MODEL_HD)
    assert C.lookup("LY Freeze").available_on(C.MODEL_4K)
    assert C.lookup("LY Freeze").available_on(C.MODEL_UNKNOWN)


def test_lookup_ignores_case_and_spacing():
    assert C.lookup("matte  density").name == "Matte Density"
    assert C.lookup("3g sdi LEVEL").name == "3G SDI level"
    assert C.lookup("nope") is None


@pytest.mark.parametrize("model,family", [
    ("Ultimatte 12 8K", C.MODEL_8K), ("Ultimatte 12 4K", C.MODEL_4K),
    ("Ultimatte 12 HD", C.MODEL_HD), ("Ultimatte 12 HD Mini", C.MODEL_HD_MINI),
    ("ultimatte 12  hd mini", C.MODEL_HD_MINI), ("Ultimatte 12", C.MODEL_12),
    ("Ultimatte 11", ""), ("", ""), ("Ultimatte 12 16K", ""),
])
def test_model_family(model, family):
    assert C.model_family(model) == family


def test_model_inputs_and_blocks():
    assert C.model_inputs(C.MODEL_HD_MINI) == ("FG In", "BG In")
    assert len(C.model_inputs(C.MODEL_HD)) == 6
    assert len(C.model_inputs(C.MODEL_8K)) == 9
    assert not C.has_gpi(C.MODEL_HD_MINI) and C.has_gpi(C.MODEL_HD)
    assert C.has_camera_control(C.MODEL_HD_MINI)
    assert not C.has_camera_control(C.MODEL_8K)


@pytest.mark.parametrize("name,text,value", [
    ("Matte Density", "273", 273), ("Output Offset", "-1500", -1500),
    ("Output Offset", "+1500", 1500), ("Matte Enable", "On", True),
    ("Matte Enable", "off", False), ("Tally Active", "on", True),
    ("Factory Defaults", "Yes", True), ("Backing Color", "blue", "blue"),
    ("Matte Density", "lots", "lots"), ("Matte Enable", "maybe", "maybe"),
    ("Active Unit", "on", "on"),        # a 12 4K control the document lacks
])
def test_parse_value(name, text, value):
    assert C.parse_value(name, text) == value


@pytest.mark.parametrize("fmt,size", [
    ("1080p25", (1920, 1080)), ("2160p59.94", (3840, 2160)),
    ("525i59.94 NTSC 4:3", (720, 486)), ("625i50 PAL 4:3", (720, 576)),
    ("720p50", (1280, 720)), ("4320p24", (7680, 4320)),
    ("auto detect", None), ("", None),
])
def test_frame_size(fmt, size):
    assert C.frame_size(fmt) == size


def test_format_value_kinds():
    f = C.format_value
    assert f(C.lookup("Matte Density"), 10000) == "10000"
    assert f(C.lookup("Matte Density"), "42") == "42"
    assert f(C.lookup("Matte Enable"), True) == "on"
    assert f(C.lookup("Matte Enable"), "OFF") == "off"
    assert f(C.lookup("Backing Color"), "Blue") == "blue"
    assert f(C.lookup("Monitor Out"), "combined matte") == "combined matte"
    assert f(C.lookup("Layer Order"), "fg/layer/bg layer/bg") == "fg/layer/bg layer/bg"
    assert f(C.lookup("Factory Defaults"), True) == "yes"
    assert f(C.lookup("Filter Median"), 3) == "3"


@pytest.mark.parametrize("name,value", [
    ("Matte Density", 10001), ("Matte Density", -1), ("Matte Density", "x"),
    ("Matte Density", True), ("Transition Rate", 0), ("Output Offset", 1501),
    ("Matte Enable", "maybe"), ("Backing Color", "purple"),
    ("Filter Median", 5), ("Tally Active", True), ("Factory Defaults", "no"),
])
def test_format_value_refuses(name, value):
    with pytest.raises(C.ControlValueError):
        C.format_value(C.lookup(name), value)


def test_model_and_protocol_refusals():
    mini = C.Context(family=C.MODEL_HD_MINI)
    with pytest.raises(C.UnsupportedError):
        C.format_value(C.lookup("GP Out Delay"), 10, mini)
    with pytest.raises(C.UnsupportedError):
        C.format_value(C.lookup("Talent Mirror"), True, C.Context(family=C.MODEL_HD))
    with pytest.raises(C.UnsupportedError):
        C.format_value(C.lookup("Output Range"), "full", C.Context(protocol=(1, 2)))
    assert C.format_value(C.lookup("Output Range"), "Full", C.Context(protocol=(2, 0))) == "full"


def test_window_position_follows_the_running_format():
    ctx = C.Context(video_format="1080p25")
    top, left = C.lookup("Window Position Top"), C.lookup("Window Position Left")
    assert C.format_value(top, 1080, ctx) == "1080"
    with pytest.raises(C.ControlValueError):
        C.format_value(top, 1081, ctx)
    assert C.format_value(left, 1920, ctx) == "1920"
    # format unknown: only the lower bound
    assert C.format_value(top, 5000, C.Context()) == "5000"
    with pytest.raises(C.ControlValueError):
        C.format_value(top, -1, C.Context())


def test_video_format_by_model_and_by_the_units_own_list():
    vf = C.lookup("Video Format")
    assert C.format_value(vf, "4320p25", C.Context(family=C.MODEL_8K)) == "4320p25"
    with pytest.raises(C.ControlValueError):
        C.format_value(vf, "4320p25", C.Context(family=C.MODEL_4K))
    with pytest.raises(C.ControlValueError):
        C.format_value(vf, "1080PsF25", C.Context(family=C.MODEL_HD_MINI))
    assert C.format_value(vf, "1080psf25", C.Context(family=C.MODEL_HD)) == "1080psf25"
    own = C.Context(video_formats=("auto detect", "1080p25"))
    assert C.format_value(vf, "1080P25", own) == "1080p25"
    with pytest.raises(C.ControlValueError):
        C.format_value(vf, "1080p50", own)


def test_offsets():
    assert C.format_offset(C.lookup("Matte Density"), -10) == "-10"
    assert C.format_offset(C.lookup("Matte Density"), "+5") == "5"
    with pytest.raises(C.ControlValueError):
        C.format_offset(C.lookup("Matte Enable"), 1)
    with pytest.raises(C.ControlValueError):
        C.format_offset(C.lookup("Matte Density"), "x")
    with pytest.raises(C.UnsupportedError):
        C.format_offset(C.lookup("GP Out Delay"), 1, C.Context(family=C.MODEL_HD_MINI))
