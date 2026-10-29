# SPDX-License-Identifier: MIT
"""9998 text-channel prelude: reading, section parsing, label, annotation.
The prelude used here is the synthetic one in conftest."""

import socket

import pytest

from ultimattewire import _preamble
from ultimattewire.profile._ranges import RANGES, annotate_preamble, to_display


# ---- read_preamble ---------------------------------------------------------

def test_read_preamble_reads_until_end_prelude_then_drains(monkeypatch, host,
                                                           fake_socket_cls, sample_prelude):
    chunks = [sample_prelude[:20].encode(), sample_prelude[20:].encode(),
              b"trailing\n", socket.timeout()]
    sock = fake_socket_cls(chunks)
    monkeypatch.setattr(_preamble, "_connect_with_retry",
                        lambda host, port, timeout=None: sock)
    text = _preamble.read_preamble(host)
    assert text == sample_prelude + "trailing\n"
    assert sock.closed
    # first the quiet timeout, then the short post-END drain timeout
    assert sock.timeouts[0] == _preamble.PREAMBLE_QUIET_TIMEOUT
    assert sock.timeouts[-1] == 0.3


def test_read_preamble_returns_partial_text_on_early_close(monkeypatch, host, fake_socket_cls):
    """The unit may drop the connection mid-prelude; return what arrived."""
    sock = fake_socket_cls([b"Label: Keyer A\nCONT"])
    monkeypatch.setattr(_preamble, "_connect_with_retry",
                        lambda host, port, timeout=None: sock)
    assert _preamble.read_preamble(host) == "Label: Keyer A\nCONT"
    assert sock.closed


# ---- parse_section / parse_file_list / extract_label -----------------------

def test_parse_section_returns_body_up_to_next_header(sample_prelude):
    body = _preamble.parse_section(sample_prelude, "CONTROL")
    assert body.splitlines() == [
        "Matte Density: 5000", "Transition Rate: 30",
        "Offset Black Balance: 2500", "Not A Param: 12",
    ]


def test_parse_section_multi_line_file_list_is_not_truncated(sample_prelude):
    assert _preamble.parse_section(sample_prelude, "FILE LIST") == "preset-a\npreset-b"


def test_parse_section_empty_section_does_not_swallow_the_next(sample_prelude):
    assert _preamble.parse_section(sample_prelude, "GPI SETTINGS") == ""


def test_parse_section_missing_header_returns_empty(sample_prelude):
    assert _preamble.parse_section(sample_prelude, "NO SUCH SECTION") == ""


# ---- extract_model / parse_current_file -------------------------------------

# Sections as an Ultimatte 12 4K with software 2.3 sent them (2026-10-27),
# the others left out and its Unique ID zeroed.
PRELUDE_12_4K = (
    "PROTOCOL PREAMBLE:\nVersion: 2.3\n\n"
    "IDENTITY:\nModel: Ultimatte 12 4K\nLabel: Studio Keyer\n"
    "Unique ID: 00000000000000000000000000000000\n\n"
    "VERSION:\nProduct ID: BE8F\nHardware Version: 0100\n"
    "Software Version: 2AE2D1CA\nSoftware Release: 2.3\n\n"
    "CURRENT FILE:\nName: 3\nStatus: Modified\n\n"
    "FILE LIST:\n1\n2\n\n"
    "END PRELUDE:\n"
)


def test_extract_model_reads_identity():
    assert _preamble.extract_model(PRELUDE_12_4K) == "Ultimatte 12 4K"


def test_extract_model_without_identity_is_empty(sample_prelude):
    assert _preamble.extract_model(sample_prelude) == ""


def test_parse_current_file_reads_name_and_status():
    assert _preamble.parse_current_file(PRELUDE_12_4K) == ("3", "Modified")
    # What the same unit said after a restore of its own archive.
    after = PRELUDE_12_4K.replace("Name: 3\nStatus: Modified", "Name: Ultimatte Defaults\nStatus: Clean")
    assert _preamble.parse_current_file(after) == ("Ultimatte Defaults", "Clean")


def test_parse_current_file_without_the_section_is_empty(sample_prelude):
    assert _preamble.parse_current_file(sample_prelude) == ("", "")


def test_the_12_4k_prelude_keeps_its_label_and_file_list():
    assert _preamble.extract_label(PRELUDE_12_4K) == "Studio_Keyer"
    assert _preamble.parse_file_list(PRELUDE_12_4K) == ["1", "2"]


def test_parse_file_list(sample_prelude):
    assert _preamble.parse_file_list(sample_prelude) == ["preset-a", "preset-b"]
    assert _preamble.parse_file_list("Label: x\nEND PRELUDE:\n") == []


def test_extract_label_sanitizes_for_filenames(sample_prelude):
    assert _preamble.extract_label(sample_prelude) == "Keyer_A"
    assert _preamble.extract_label("Label: a/b:c d\n") == "a_b_c_d"
    assert _preamble.extract_label("no label here\n") == "unknown"


# ---- ranges / annotation ---------------------------------------------------

@pytest.mark.parametrize("name,raw,expected", [
    ("Matte Density", "5000", (100.0, "%")),      # -100..300 span, midpoint
    ("Black Balance", "2500", (-50.0, "%")),      # -100..100 span, quarter
    ("Transition Rate", "30", (30.0, "frames")),  # shown as-is
    ("Matte Density", "abc", (None, None)),
    ("Not A Param", "1", (None, None)),
])
def test_to_display(name, raw, expected):
    assert to_display(name, raw) == expected


def test_ranges_table_is_well_formed():
    for name, entry in RANGES.items():
        assert len(entry) == 6, name
        rmin, rmax, dmin, ddef, dmax, unit = entry
        assert rmin <= rmax, name
        assert isinstance(unit, str) and unit, name
        if dmin is not None:
            assert dmin <= ddef <= dmax, name


def test_annotate_preamble_annotates_control_lines_only(sample_prelude):
    out = annotate_preamble(sample_prelude)
    lines = out.splitlines()
    assert "Label: Keyer A" in lines                      # outside CONTROL: untouched
    assert any(l.startswith("Matte Density: 5000") and l.endswith("(100%)") for l in lines)
    assert any(l.startswith("Transition Rate: 30") and l.endswith("(30 frames)") for l in lines)
    assert any(l.startswith("Offset Black Balance: 2500") and l.endswith("(-50%)") for l in lines)
    assert "Not A Param: 12" in lines                     # no range entry: untouched
    assert "preset-a" in lines                            # FILE LIST left alone
    assert out.endswith("\n")
