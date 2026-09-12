# SPDX-License-Identifier: LGPL-3.0-only
"""Round-trip and parse tests for atemwire.profile against the reference XML.

The reference file lives at the repo root (the user dropped it there for
manual exercise + CI). It's a complete export from a real Constellation HD,
format v2.1. These tests exercise parser/serializer correctness without
needing a live ATEM connection.
"""

import os
import re

import pytest

from atemwire.profile import (
    ApplyOptions, ApplyResult, MixEffectOptions, Profile,
)


_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_REFERENCE_XML = os.path.join(
    _REPO_ROOT, 'everything_2026-04-27_18-16-51.xml'
)
_MACROS_ONLY_XML = os.path.join(
    _REPO_ROOT, 'macros_only_2025-05-12_11-24-47.xml'
)


@pytest.fixture
def reference_xml() -> str:
    if not os.path.exists(_REFERENCE_XML):
        pytest.skip("reference XML not present in repo root")
    with open(_REFERENCE_XML, encoding='utf-8') as f:
        return f.read()


@pytest.fixture
def macros_only_xml() -> str:
    if not os.path.exists(_MACROS_ONLY_XML):
        pytest.skip("macros-only XML not present in repo root")
    with open(_MACROS_ONLY_XML, encoding='utf-8') as f:
        return f.read()


def _normalize_whitespace(s: str) -> str:
    """Collapse whitespace to make XML byte-comparable across formatters."""
    s = re.sub(r'>\s+<', '><', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def test_profile_parses_reference(reference_xml):
    p = Profile.from_xml(reference_xml)
    assert p.major_version == 2
    assert p.minor_version == 1
    assert p.product == 'ATEM 1 M/E Constellation HD'
    assert p.video_mode == '1080p60'
    macros = p.macros()
    # Reference has 8 used macro slots.
    assert len(macros) >= 5


def test_profile_parses_macros_only(macros_only_xml):
    p = Profile.from_xml(macros_only_xml)
    macros = p.macros()
    assert len(macros) == 1
    macro0 = macros[0]
    assert macro0['name'] == 'Deal Cam'
    op_ids = [op['id'] for op in macro0['ops']]
    assert 'ProgramInput' in op_ids
    assert 'KeyOnAir' in op_ids


def test_profile_round_trip_normalized(reference_xml):
    """Whitespace-normalized round-trip should be byte-identical."""
    p = Profile.from_xml(reference_xml)
    out = p.to_xml()

    a = _normalize_whitespace(reference_xml)
    b = _normalize_whitespace(out)

    if a != b:
        # Provide actionable diff context if it fails.
        for i, (ca, cb) in enumerate(zip(a, b)):
            if ca != cb:
                start = max(0, i - 50)
                end = min(len(a), i + 50)
                end_b = min(len(b), i + 50)
                pytest.fail(
                    f"diff at offset {i}\n"
                    f"  expected: ...{a[start:end]}...\n"
                    f"  got     : ...{b[start:end_b]}..."
                )
        pytest.fail(f"length differ: {len(a)} vs {len(b)}")
    assert a == b


def test_profile_to_xml_starts_with_declaration(reference_xml):
    p = Profile.from_xml(reference_xml)
    out = p.to_xml()
    assert out.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')


def test_profile_to_xml_uses_4space_indent_and_compact_self_closing(reference_xml):
    p = Profile.from_xml(reference_xml)
    out = p.to_xml()
    # Self-closing tags should not have a leading space.
    assert ' />' not in out
    # First nested element should be 4-space indented.
    assert '\n    <MixEffectBlocks>' in out


def test_apply_options_default_skips_program_preview():
    opts = ApplyOptions()
    # Per-M/E blocks: platform-max list, each with the cuts-to-air
    # gates off and everything else on.
    assert len(opts.mes) == 4
    for me_opts in opts.mes:
        assert me_opts.program is False
        assert me_opts.preview is False
        assert me_opts.next_transition is True
        assert me_opts.transition_style is True
        assert me_opts.fade_to_black is True
        assert me_opts.usk == [True, True, True, True]
    # Switcher-global / coarse sections also default on.
    assert opts.restore_downstream_keys is True
    assert opts.restore_inputs is True
    assert opts.restore_video_mode is True
    assert opts.restore_audio is True
    assert opts.restore_macros is True
    # Sections still gated by missing ops are off.
    assert opts.restore_multiview is False


def test_apply_result_summary():
    r = ApplyResult()
    assert r.summary() == 'no changes'
    r.note_applied('color_generators')
    r.note_skipped('audio', 'gap')
    assert 'applied: 1' in r.summary()
    assert 'skipped: 1' in r.summary()


# -----------------------------------------------------------------------------
# Next-transition selection apply — regression test for the RMW race that
# previously turned "Background,Key1,Key2,Key3,Key4" into a single-bit-only
# mask because each toggle re-read mixerstate and packed the full mask
# absolutely. Fix: use ``ops.set_next_transition_layers`` which sends one
# TransitionSettingsCommand with the absolute mask.
# -----------------------------------------------------------------------------


class _RecordingConn:
    """Minimal stand-in for ATEMConnection used by the apply path.
    Captures every Command passed to ``send`` so the test can assert on the
    wire bytes without spinning up a real worker thread."""

    def __init__(self):
        self.sent = []
        self.mixerstate = {}

    def send(self, command):
        self.sent.append(command)


def _profile_with_selection(selection_str: str) -> Profile:
    """Build a minimal valid profile XML with the given next-transition
    selection. ME 0 only — enough for the apply path to find a
    NextTransition element to operate on."""
    xml = (
        '<Profile majorVersion="2" minorVersion="1">'
        '<MixEffectBlocks>'
        '<MixEffectBlock index="0">'
        f'<NextTransition selection="{selection_str}" '
        f'nextSelection="{selection_str}"/>'
        '</MixEffectBlock>'
        '</MixEffectBlocks>'
        '</Profile>'
    )
    return Profile.from_xml(xml)


def _apply_with_only_transition(profile: Profile, conn) -> None:
    """Run Profile.apply with the next-transition gate on (and the
    in-transition-style gate also on so set_transition_style can
    happen), everything else off, so the only wire commands the test
    sees are the transition ones."""
    opts = ApplyOptions(
        mes=[MixEffectOptions(
            program=False,
            preview=False,
            next_transition=True,
            transition_style=False,
            fade_to_black=False,
            usk=[False, False, False, False],
        )],
        restore_video_mode=False,
        restore_inputs=False,
        restore_audio=False,
        restore_macros=False,
        restore_downstream_keys=False,
        restore_color_generators=False,
        restore_aux=False,
        restore_media_players=False,
        restore_settings_flags=False,
    )
    profile.apply(conn, opts)


def _trss_next_transition_from_sent(sent):
    """Return the ``next_transition`` mask of the single
    TransitionSettingsCommand recorded for the apply, or None if absent."""
    from atemwire.messages import TransitionSettingsCommand
    for cmd in sent:
        if isinstance(cmd, TransitionSettingsCommand):
            if cmd.next_transition is not None:
                return cmd.next_transition
    return None


def test_apply_next_transition_all_layers_no_whitespace():
    # The save side of this library emits the selection list with no spaces.
    p = _profile_with_selection('Background,Key1,Key2,Key3,Key4')
    conn = _RecordingConn()
    _apply_with_only_transition(p, conn)
    mask = _trss_next_transition_from_sent(conn.sent)
    # 0b11111 = 31 = BG + Key1 + Key2 + Key3 + Key4 all set.
    assert mask == 0x1F, f"expected mask 0x1F, got {mask!r} (sent={conn.sent})"


def test_apply_next_transition_all_layers_with_whitespace():
    # The Software Control save format uses ", " spaces — must round-trip
    # the same way through _str_to_next_transition's whitespace strip.
    p = _profile_with_selection('Background, Key1, Key2, Key3, Key4')
    conn = _RecordingConn()
    _apply_with_only_transition(p, conn)
    assert _trss_next_transition_from_sent(conn.sent) == 0x1F


def test_apply_next_transition_partial_selection():
    # Only Background + Key2 — make sure the mask reflects ONLY those.
    p = _profile_with_selection('Background,Key2')
    conn = _RecordingConn()
    _apply_with_only_transition(p, conn)
    # 0b00101 = 5 = Background (bit 0) + Key2 (bit 2).
    assert _trss_next_transition_from_sent(conn.sent) == 0x05


def test_apply_next_transition_keyer_only():
    # No background bit, single keyer — verify no implicit Background fallback.
    p = _profile_with_selection('Key3')
    conn = _RecordingConn()
    _apply_with_only_transition(p, conn)
    # 0b01000 = 8 = Key3 (bit 3) only.
    assert _trss_next_transition_from_sent(conn.sent) == 0x08


def test_apply_next_transition_emits_one_packet_not_five():
    # The bug shipped 5 sequential toggles racing each other. The fix
    # sends ONE TransitionSettingsCommand with the absolute mask. Count
    # the TransitionSettingsCommand-with-next_transition packets to
    # verify we don't regress.
    from atemwire.messages import TransitionSettingsCommand
    p = _profile_with_selection('Background,Key1,Key2,Key3,Key4')
    conn = _RecordingConn()
    _apply_with_only_transition(p, conn)
    nt_packets = [c for c in conn.sent
                  if isinstance(c, TransitionSettingsCommand)
                  and c.next_transition is not None]
    assert len(nt_packets) == 1, (
        f"expected 1 TrSS next_transition packet, got {len(nt_packets)}: "
        f"{nt_packets}")


def test_profile_from_atem_against_synthetic_state():
    """Build a Profile from a synthetic mixerstate and verify the XML
    structure is well-formed and contains the expected sections."""

    class _Source:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class _ProductName:
        name = b'ATEM 1 M/E Constellation HD'

    class _VideoMode:
        rate = 60
        def get_label(self):
            return '1080p60'
        def get_resolution(self):
            return (1920, 1080)

    mx = {
        'product-name': _ProductName(),
        'video-mode': _VideoMode(),
        'program-bus-input': {0: _Source(source=1)},
        'preview-bus-input': {0: _Source(source=2)},
        'transition-settings': {0: _Source(
            style=0, style_next=0,
            next_transition_bkgd=True,
            next_transition_key1=False, next_transition_key2=False,
            next_transition_key3=False, next_transition_key4=False,
        )},
        'transition-position': {0: _Source(in_transition=False, position=0,
                                          frames_remaining=0)},
        'transition-mix': {0: _Source(rate=25)},
        'transition-dip': {0: _Source(rate=25, source=2001)},
        'transition-wipe': {0: _Source(rate=25, pattern=0, symmetry=5000,
                                       positionx=5000, positiony=5000,
                                       reverse=False, flipflop=False,
                                       softness=0, width=0, source=2001)},
        'transition-dve': {0: _Source(rate=25, fill_source=1, key_source=1,
                                      key_enable=False, key_clip=5000,
                                      key_gain=7000, key_premultiplied=False,
                                      key_invert=False, style=13,
                                      reverse=False, flipflop=False)},
        'transition-stinger': {0: _Source(rate=25, mediaplayer=3010,
                                          duration=150, triggerpoint=34,
                                          preroll=48, key_clip=5000,
                                          key_gain=7000,
                                          key_premultiplied=True,
                                          key_invert=False)},
        'fade-to-black': {0: _Source(rate=25)},
        'fade-to-black-state': {0: _Source(done=False, transitioning=False,
                                          frames_remaining=0)},
        'aux-output-source': {i: _Source(source=10010) for i in range(6)},
        'color-generator': {i: _Source(hue=0.0, saturation=0.0, luma=0.5)
                            for i in range(2)},
        'mediaplayer-selected': {i: _Source(source_type=1, slot=0)
                                 for i in range(2)},
        'input-properties': {
            i: _Source(index=i, name=f'Camera {i}', short_name=f'Cam{i}',
                       port_type=0, source_ports=1)
            for i in range(1, 11)
        },
    }

    p = Profile.from_atem(_Source(mixerstate=mx))
    out = p.to_xml()
    # Round-trip the synthetic profile.
    p2 = Profile.from_xml(out)
    assert p2.product == 'ATEM 1 M/E Constellation HD'
    assert p2.video_mode == '1080p60'
    # Every mandatory top-level child should be present.
    for section in ('MixEffectBlocks', 'DownstreamKeys', 'ColorGenerators',
                    'Auxiliaries', 'Settings', 'VideoMode', 'HyperDecks',
                    'FairlightAudioMixer', 'MediaPlayers', 'MediaPool',
                    'CameraControl', 'MacroPool', 'MacroControl', 'Counters'):
        assert p2.root.find(section) is not None, f"missing section {section}"
