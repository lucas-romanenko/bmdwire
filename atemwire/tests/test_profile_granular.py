# SPDX-License-Identifier: LGPL-3.0-only
"""Per-element granular-gating tests for Profile.apply and
Profile.from_atem.

Each test in this file targets exactly one of the nine M/E grid
flags (program, preview, next_transition, transition_style,
fade_to_black, usk[0..3]) and verifies that toggling it flips
emission/application of just that XML element / wire command,
nothing else. The Stage 4C-1 section at the bottom adds the per-M/E
dimension (multi-M/E emission, per-M/E gating, cross-model apply,
and the pre-4C-1 byte-identical golden).

The tests use a recording connection (no live ATEM) and hand-built
profile XMLs so the assertions stay tight to the gate behaviour.
"""

import os

from atemwire.profile import (
    ApplyOptions, MixEffectOptions, Profile, SaveOptions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _RecordingConn:
    """Captures every Command passed to ``send`` so tests can assert
    on the wire commands that the apply path emits."""

    def __init__(self, mixerstate=None):
        self.sent = []
        self.mixerstate = mixerstate or {}

    def send(self, command):
        self.sent.append(command)


# The historical flat restore_* spellings the single-M/E tests use,
# mapped onto their mes[0] (MixEffectOptions) attribute.
_ME_APPLY_FLAGS = {
    'restore_program': 'program',
    'restore_preview': 'preview',
    'restore_next_transition': 'next_transition',
    'restore_transition_style': 'transition_style',
    'restore_fade_to_black': 'fade_to_black',
    'restore_usk': 'usk',
}


def _all_off_apply_options(me_count=1, **overrides):
    """Build an ApplyOptions with every flag off; tests turn ON exactly
    the flag(s) they're testing. Per-M/E overrides keep the historical
    flat ``restore_*`` spelling and map onto ``mes[0]``; ``me_count``
    sizes the (all-off) ``mes`` list for the multi-M/E tests."""
    opts = ApplyOptions(
        mes=[MixEffectOptions.none() for _ in range(me_count)],
        restore_downstream_keys=False, restore_color_generators=False,
        restore_audio=False, restore_aux=False,
        restore_video_mode=False, restore_inputs=False,
        restore_macros=False, restore_media_players=False,
        restore_settings_flags=False, restore_media_pool_images=False,
    )
    for k, v in overrides.items():
        if k in _ME_APPLY_FLAGS:
            setattr(opts.mes[0], _ME_APPLY_FLAGS[k], v)
        else:
            setattr(opts, k, v)
    return opts


def _full_me_profile():
    """Return a Profile with one MixEffectBlock containing all nine
    addressable elements: Program, Preview, NextTransition,
    TransitionStyle (+ children), FadeToBlack, and four Keys."""
    xml = """<Profile majorVersion="2" minorVersion="1">
    <MixEffectBlocks>
        <MixEffectBlock index="0">
            <Program input="3010"/>
            <Preview input="1"/>
            <NextTransition selection="Background,Key1" nextSelection="Background,Key1"/>
            <TransitionStyle style="Mix" nextStyle="Mix" previewTransition="False" transitionPosition="0">
                <MixParameters rate="25"/>
                <DipParameters rate="25" input="2001"/>
                <WipeParameters rate="25" pattern="LeftToRightBar" symmetry="50" xPosition="0.5" yPosition="0.5" reverseDirection="False" flipFlip="False" borderInput="2001" borderWidth="0" borderSoftness="0"/>
                <StingerParameters source="MediaPlayer1" preMultipliedKey="True" clip="50" gain="70" invert="False" clipDuration="73" triggerPoint="34" mixRate="5" preroll="0"/>
                <DVEParameters rate="25" logoRate="25" reverseDirection="False" flipFlop="False" effect="PushRight" fillSource="1" keySource="1" enableKey="False" preMultipliedKey="False" clip="50" gain="70" invertKey="False"/>
            </TransitionStyle>
            <Keys>
                <Key index="0" type="Luma" inputCut="1" inputFill="1" onAir="False" masked="False" maskTop="0" maskBottom="0" maskLeft="0" maskRight="0">
                    <LumaParameters preMultiplied="False" clip="50" gain="70" inverse="False"/>
                </Key>
                <Key index="1" type="Luma" inputCut="2" inputFill="2" onAir="False" masked="False" maskTop="0" maskBottom="0" maskLeft="0" maskRight="0">
                    <LumaParameters preMultiplied="False" clip="50" gain="70" inverse="False"/>
                </Key>
                <Key index="2" type="Luma" inputCut="3" inputFill="3" onAir="False" masked="False" maskTop="0" maskBottom="0" maskLeft="0" maskRight="0">
                    <LumaParameters preMultiplied="False" clip="50" gain="70" inverse="False"/>
                </Key>
                <Key index="3" type="Luma" inputCut="4" inputFill="4" onAir="False" masked="False" maskTop="0" maskBottom="0" maskLeft="0" maskRight="0">
                    <LumaParameters preMultiplied="False" clip="50" gain="70" inverse="False"/>
                </Key>
            </Keys>
            <FadeToBlack rate="25" isFullyBlack="False"/>
        </MixEffectBlock>
    </MixEffectBlocks>
</Profile>
"""
    return Profile.from_xml(xml)


# ---------------------------------------------------------------------------
# Apply-path gates: program / preview
# ---------------------------------------------------------------------------

def test_apply_program_only_does_not_set_preview():
    from atemwire.messages import ProgramInputCommand, PreviewInputCommand

    p = _full_me_profile()
    conn = _RecordingConn()
    p.apply(conn, _all_off_apply_options(restore_program=True))

    program_cmds = [c for c in conn.sent if isinstance(c, ProgramInputCommand)]
    preview_cmds = [c for c in conn.sent if isinstance(c, PreviewInputCommand)]
    assert len(program_cmds) == 1
    assert len(preview_cmds) == 0


def test_apply_preview_only_does_not_set_program():
    from atemwire.messages import ProgramInputCommand, PreviewInputCommand

    p = _full_me_profile()
    conn = _RecordingConn()
    p.apply(conn, _all_off_apply_options(restore_preview=True))

    program_cmds = [c for c in conn.sent if isinstance(c, ProgramInputCommand)]
    preview_cmds = [c for c in conn.sent if isinstance(c, PreviewInputCommand)]
    assert len(program_cmds) == 0
    assert len(preview_cmds) == 1


def test_apply_neither_program_nor_preview_emits_nothing_on_bus():
    from atemwire.messages import ProgramInputCommand, PreviewInputCommand

    p = _full_me_profile()
    conn = _RecordingConn()
    p.apply(conn, _all_off_apply_options())

    assert not any(isinstance(c, (ProgramInputCommand, PreviewInputCommand))
                   for c in conn.sent)


# ---------------------------------------------------------------------------
# Apply-path gates: next_transition / transition_style
# ---------------------------------------------------------------------------

def test_apply_next_transition_only_no_style_packet():
    """``restore_next_transition=True`` + ``restore_transition_style=False``
    sends the TrSS selection mask but NOT the per-style commands
    (set_transition_style + set_mix_rate / dip / wipe / stinger / dve)."""
    from atemwire.messages import (
        TransitionSettingsCommand, MixSettingsCommand,
        DipSettingsCommand, WipeSettingsCommand,
    )

    p = _full_me_profile()
    conn = _RecordingConn()
    p.apply(conn, _all_off_apply_options(restore_next_transition=True))

    # Exactly one TrSS command (the next-transition mask).
    trss = [c for c in conn.sent if isinstance(c, TransitionSettingsCommand)]
    assert len(trss) == 1
    assert trss[0].next_transition is not None
    # No per-style packets at all.
    assert not any(isinstance(c, (MixSettingsCommand, DipSettingsCommand,
                                  WipeSettingsCommand))
                   for c in conn.sent)


def test_apply_transition_style_only_no_next_transition_mask():
    """``restore_transition_style=True`` + ``restore_next_transition=False``
    sends the per-style packets but no TrSS mask."""
    from atemwire.messages import (
        TransitionSettingsCommand, MixSettingsCommand,
    )

    p = _full_me_profile()
    conn = _RecordingConn()
    p.apply(conn, _all_off_apply_options(restore_transition_style=True))

    # No TrSS with next_transition set.
    trss_with_mask = [c for c in conn.sent
                      if isinstance(c, TransitionSettingsCommand)
                      and c.next_transition is not None]
    assert len(trss_with_mask) == 0
    # At least one MixSettingsCommand (per-style emit).
    assert any(isinstance(c, MixSettingsCommand) for c in conn.sent)


# ---------------------------------------------------------------------------
# Apply-path gates: fade_to_black
# ---------------------------------------------------------------------------

def test_apply_fade_to_black_only():
    from atemwire.messages import (
        FadeToBlackConfigCommand, ProgramInputCommand,
    )

    p = _full_me_profile()
    conn = _RecordingConn()
    p.apply(conn, _all_off_apply_options(restore_fade_to_black=True))

    ftb_cmds = [c for c in conn.sent if isinstance(c, FadeToBlackConfigCommand)]
    assert len(ftb_cmds) >= 1
    assert not any(isinstance(c, ProgramInputCommand) for c in conn.sent)


# ---------------------------------------------------------------------------
# Apply-path gates: per-USK
# ---------------------------------------------------------------------------

def _key_setters_per_keyer(sent):
    """Group emitted KeyType commands by their keyer attribute. Each
    USK that the apply path touches issues at least one KeyTypeCommand
    (set_usk_type runs first so subsequent params target the right type)."""
    from atemwire.messages import KeyTypeCommand
    out = {0: 0, 1: 0, 2: 0, 3: 0}
    for c in sent:
        if isinstance(c, KeyTypeCommand):
            k = c.keyer
            if k in out:
                out[k] += 1
    return out


def test_apply_usk_2_only():
    """Apply with only usk_3 (keyer index 2) selected. Verify keyers
    0, 1, 3 don't get touched and keyer 2 does."""
    p = _full_me_profile()
    conn = _RecordingConn()
    p.apply(conn, _all_off_apply_options(
        restore_usk=[False, False, True, False]))

    counts = _key_setters_per_keyer(conn.sent)
    assert counts[0] == 0, f'USK 1 (idx 0) leaked: {counts}'
    assert counts[1] == 0, f'USK 2 (idx 1) leaked: {counts}'
    assert counts[2] >= 1, f'USK 3 (idx 2) missing: {counts}'
    assert counts[3] == 0, f'USK 4 (idx 3) leaked: {counts}'


def test_apply_usk_skip_2_keep_others():
    """Apply with usk_2 (keyer index 1) unchecked. Verify USK 1, 3, 4
    apply but not USK 2."""
    p = _full_me_profile()
    conn = _RecordingConn()
    p.apply(conn, _all_off_apply_options(
        restore_usk=[True, False, True, True]))

    counts = _key_setters_per_keyer(conn.sent)
    assert counts[0] >= 1
    assert counts[1] == 0, f'USK 2 (idx 1) leaked: {counts}'
    assert counts[2] >= 1
    assert counts[3] >= 1


def test_apply_no_usk_emits_no_key_setters():
    """All four USK flags off → no KeyType / KeyFill / KeyCut commands."""
    from atemwire.messages import KeyTypeCommand, KeyFillCommand, KeyCutCommand

    p = _full_me_profile()
    conn = _RecordingConn()
    p.apply(conn, _all_off_apply_options(
        restore_usk=[False, False, False, False]))

    assert not any(isinstance(c, (KeyTypeCommand, KeyFillCommand,
                                  KeyCutCommand)) for c in conn.sent)


# ---------------------------------------------------------------------------
# Save-path gates: emission of XML elements
# ---------------------------------------------------------------------------

class _S:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _ProductName: name = b'Test'


class _VideoMode:
    rate = 60
    def get_label(self): return '1080p60'
    def get_resolution(self): return (1920, 1080)


def _synthetic_mixerstate(me_count=1):
    """Minimal synthetic mixerstate for save tests.

    The M/E 0 values are FROZEN — the pre-4C-1 byte-identical golden
    (``profile_single_me_save_golden.xml``) was generated from exactly
    this 1-M/E shape; don't change them without regenerating it.

    ``me_count > 1`` adds the ``_MeC`` entries (M/E 1 sized to 2
    keyers, the rest 4) plus per-M/E bus/transition state — distinct
    Program/Preview sources (1+10·me / 2+10·me) and mix rates (25+me)
    so per-M/E emission is assertable; the other per-M/E keys share
    M/E 0's (read-only) value objects."""
    mx = {
        'product-name': _ProductName(),
        'video-mode': _VideoMode(),
        'program-bus-input': {0: _S(source=1)},
        'preview-bus-input': {0: _S(source=2)},
        'transition-settings': {0: _S(
            style=0, style_next=0,
            next_transition_bkgd=True,
            next_transition_key1=False, next_transition_key2=False,
            next_transition_key3=False, next_transition_key4=False)},
        'transition-position': {0: _S(in_transition=False, position=0,
                                      frames_remaining=0)},
        'transition-mix': {0: _S(rate=25)},
        'transition-dip': {0: _S(rate=25, source=2001)},
        'transition-wipe': {0: _S(rate=25, pattern=0, symmetry=5000,
                                  positionx=5000, positiony=5000,
                                  reverse=False, flipflop=False,
                                  softness=0, width=0, source=2001)},
        'transition-dve': {0: _S(rate=25, fill_source=1, key_source=1,
                                 key_enable=False, key_clip=5000,
                                 key_gain=7000, key_premultiplied=False,
                                 key_invert=False, style=13,
                                 reverse=False, flipflop=False)},
        'transition-stinger': {0: _S(rate=25, mediaplayer=3010,
                                     duration=150, triggerpoint=34,
                                     preroll=48, key_clip=5000,
                                     key_gain=7000, key_premultiplied=True,
                                     key_invert=False)},
        'fade-to-black': {0: _S(rate=25)},
        'fade-to-black-state': {0: _S(done=False, transitioning=False,
                                      frames_remaining=0)},
        'aux-output-source': {},
        'color-generator': {},
        'mediaplayer-selected': {},
        'input-properties': {},
    }
    if me_count > 1:
        mx['mixer-effect-config'] = {
            me: _S(keyers=(2 if me == 1 else 4)) for me in range(me_count)}
        shared_keys = (
            'transition-settings', 'transition-position', 'transition-dip',
            'transition-wipe', 'transition-dve', 'transition-stinger',
            'fade-to-black', 'fade-to-black-state',
        )
        for me in range(1, me_count):
            mx['program-bus-input'][me] = _S(source=1 + 10 * me)
            mx['preview-bus-input'][me] = _S(source=2 + 10 * me)
            mx['transition-mix'][me] = _S(rate=25 + me)
            for key in shared_keys:
                mx[key][me] = mx[key][0]
    return mx


def _build_synthetic_save_xml(opts, extra_mx=None, me_count=1):
    """Build a Profile from the synthetic mixerstate using ``opts`` and
    return the parsed XML root. ``extra_mx`` (if given) is merged into
    the mixerstate so a test can populate a feature key the baseline
    dict leaves empty (e.g. KKFP fly keyframes)."""
    mx = _synthetic_mixerstate(me_count)
    if extra_mx:
        mx.update(extra_mx)
    return Profile.from_atem(_S(mixerstate=mx), opts).root


# The six per-M/E SaveOptions cells (live on mes[0] for the single-tab
# flat spelling these tests use).
_ME_SAVE_FLAGS = ('program', 'preview', 'next_transition',
                  'transition_style', 'fade_to_black', 'usk')


def _save_with(**flags):
    """Run from_atem with all SaveOptions defaulting True except those
    overridden in ``flags`` (per-M/E flags map onto ``mes[0]``).
    Returns the root Element."""
    opts = SaveOptions()
    for k, v in flags.items():
        if k in _ME_SAVE_FLAGS:
            setattr(opts.mes[0], k, v)
        else:
            setattr(opts, k, v)
    return _build_synthetic_save_xml(opts)


def test_save_program_off_omits_program_element():
    root = _save_with(program=False)
    block = root.find('MixEffectBlocks/MixEffectBlock')
    assert block is not None
    assert block.find('Program') is None
    assert block.find('Preview') is not None  # preview kept


def test_save_preview_off_omits_preview_element():
    root = _save_with(preview=False)
    block = root.find('MixEffectBlocks/MixEffectBlock')
    assert block.find('Preview') is None
    assert block.find('Program') is not None


def test_save_next_transition_off_omits_next_transition_element():
    root = _save_with(next_transition=False)
    block = root.find('MixEffectBlocks/MixEffectBlock')
    assert block.find('NextTransition') is None
    # TransitionStyle stays
    assert block.find('TransitionStyle') is not None


def test_save_transition_style_off_omits_style_and_per_style_children():
    root = _save_with(transition_style=False)
    block = root.find('MixEffectBlocks/MixEffectBlock')
    assert block.find('TransitionStyle') is None
    assert block.find('NextTransition') is not None  # next_transition kept


def test_save_fade_to_black_off_omits_ftb_element():
    root = _save_with(fade_to_black=False)
    block = root.find('MixEffectBlocks/MixEffectBlock')
    assert block.find('FadeToBlack') is None


def test_save_usk_2_off_omits_only_key_index_1():
    root = _save_with(usk=[True, False, True, True])
    keys = root.find('MixEffectBlocks/MixEffectBlock/Keys')
    assert keys is not None
    indices = sorted(int(k.get('index')) for k in keys.findall('Key'))
    assert indices == [0, 2, 3]


def test_save_no_usk_omits_keys_element_entirely():
    root = _save_with(usk=[False, False, False, False])
    block = root.find('MixEffectBlocks/MixEffectBlock')
    # Keys wrapper only emitted when at least one USK is selected.
    assert block.find('Keys') is None


def test_save_all_me_off_omits_mix_effect_blocks_entirely():
    root = _save_with(
        program=False, preview=False, next_transition=False,
        transition_style=False, fade_to_black=False,
        usk=[False, False, False, False])
    assert root.find('MixEffectBlocks') is None


def test_save_options_default_emits_all_nine_elements():
    """No-args defaults preserve the byte-identical round-trip — all
    nine M/E elements present."""
    root = _build_synthetic_save_xml(SaveOptions())
    block = root.find('MixEffectBlocks/MixEffectBlock')
    assert block.find('Program') is not None
    assert block.find('Preview') is not None
    assert block.find('NextTransition') is not None
    assert block.find('TransitionStyle') is not None
    assert block.find('FadeToBlack') is not None
    keys = block.find('Keys')
    indices = sorted(int(k.get('index')) for k in keys.findall('Key'))
    assert indices == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# ApplyOptions / SaveOptions field shape sanity
# ---------------------------------------------------------------------------

def test_save_options_default_has_nine_me_flags():
    """The nine M/E grid flags live on each mes[] entry; the default
    list is platform-max length with everything on."""
    opts = SaveOptions()
    assert len(opts.mes) == 4
    for me in opts.mes:
        assert me.program is True
        assert me.preview is True
        assert me.next_transition is True
        assert me.transition_style is True
        assert me.fade_to_black is True
        assert me.usk == [True, True, True, True]


def test_me_options_beyond_list_is_fully_deselected():
    """An M/E index beyond the mes list saves/applies nothing — the
    single-tab dialog's 1-element mapping leaves higher M/Es alone."""
    opts = SaveOptions(mes=[MixEffectOptions()])
    assert opts.me_options(0).any_selected() is True
    assert opts.me_options(1).any_selected() is False
    assert opts.me_options(7).any_selected() is False


def test_apply_options_old_field_names_are_gone():
    """The Path B → Path A split removed the coarse fields. Make sure
    they're truly gone (catches accidental aliases)."""
    opts = ApplyOptions()
    for old in ('restore_program_preview', 'restore_transition',
                'restore_keyers', 'restore_upstream_keyers'):
        assert not hasattr(opts, old), f'old field {old} should be removed'


# ---------------------------------------------------------------------------
# Fly keyframe save — KeyFrameA AND KeyFrameB
# ---------------------------------------------------------------------------

class _KKFP:
    """KKFP stand-in for usk_fly_keyframe (reads wire-level attrs via
    getattr). Only the fields a test cares about need setting; the reader
    defaults the rest."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fly_params_for(root, me=0, k=0):
    return root.find(
        f"MixEffectBlocks/MixEffectBlock[@index='{me}']"
        f"/Keys/Key[@index='{k}']/FlyParameters")


def test_save_emits_both_keyframe_a_and_b():
    """Both KKFP keyframes stored → save emits <KeyFrameA> AND <KeyFrameB>
    with their distinct geometry. Regression guard for the save gap where
    only KeyFrameA was ever written (KeyFrameB stranded despite KKFP
    parsing/keying both and apply restoring both)."""
    kkfp = {0: {0: {
        1: _KKFP(size_x=1500, size_y=500, pos_y=0),       # A
        2: _KKFP(size_x=1500, size_y=500, pos_y=13000),   # B
    }}}
    root = _build_synthetic_save_xml(
        SaveOptions(), extra_mx={'key-properties-fly-keyframe': kkfp})

    fly = _fly_params_for(root, me=0, k=0)
    assert fly is not None
    kf_a = fly.find('KeyFrameA')
    kf_b = fly.find('KeyFrameB')
    assert kf_a is not None
    assert kf_b is not None, 'KeyFrameB must be emitted, not only KeyFrameA'
    # Geometry divided by 1000; A and B differ in yPosition (0 vs 13.0).
    assert float(kf_a.get('xSize')) == 1.5
    assert float(kf_a.get('yPosition')) == 0.0
    assert float(kf_b.get('yPosition')) == 13.0


def test_save_emits_only_stored_keyframe():
    """Each KeyFrame is gated on its keyframe being stored — only A present
    → no spurious <KeyFrameB>."""
    kkfp = {0: {0: {1: _KKFP(size_x=1500, pos_y=0)}}}   # A only
    root = _build_synthetic_save_xml(
        SaveOptions(), extra_mx={'key-properties-fly-keyframe': kkfp})

    fly = _fly_params_for(root, me=0, k=0)
    assert fly.find('KeyFrameA') is not None
    assert fly.find('KeyFrameB') is None


# ---------------------------------------------------------------------------
# Per-M/E dimension (Stage 4C-1)
# ---------------------------------------------------------------------------

_GOLDEN_PATH = os.path.join(os.path.dirname(__file__),
                            'profile_single_me_save_golden.xml')


def test_single_me_save_byte_identical_to_pre_4c1_golden():
    """The golden XML was generated by the pre-4C-1 (single-M/E
    hardcoded) save code from exactly ``_synthetic_mixerstate(1)`` with
    default SaveOptions. The per-M/E loop must reproduce it
    byte-for-byte on a 1-M/E switcher."""
    xml = Profile.from_atem(
        _S(mixerstate=_synthetic_mixerstate()), SaveOptions()).to_xml()
    with open(_GOLDEN_PATH, encoding='utf-8') as f:
        golden = f.read()
    assert xml == golden


def _me_blocks_by_index(root):
    return {int(b.get('index')): b
            for b in root.findall('MixEffectBlocks/MixEffectBlock')}


def test_multi_me_save_emits_one_block_per_me_with_per_me_values():
    """Default SaveOptions over a fake 4-M/E mixerstate emits
    <MixEffectBlock index="0..3"> with each M/E's own values."""
    root = _build_synthetic_save_xml(SaveOptions(), me_count=4)
    blocks = _me_blocks_by_index(root)
    assert sorted(blocks) == [0, 1, 2, 3]
    for me, block in blocks.items():
        assert block.find('Program').get('input') == str(1 + 10 * me)
        assert block.find('Preview').get('input') == str(2 + 10 * me)
        mix = block.find('TransitionStyle/MixParameters')
        assert mix.get('rate') == str(25 + me)


def test_multi_me_save_sizes_usk_to_each_mes_keyer_count():
    """M/E 1's _MeC entry carries keyers=2 — its <Keys> holds indices
    0..1 while the 4-keyer M/Es hold 0..3."""
    root = _build_synthetic_save_xml(SaveOptions(), me_count=4)
    blocks = _me_blocks_by_index(root)
    for me, expected in ((0, [0, 1, 2, 3]), (1, [0, 1]),
                         (2, [0, 1, 2, 3]), (3, [0, 1, 2, 3])):
        indices = sorted(int(k.get('index'))
                         for k in blocks[me].findall('Keys/Key'))
        assert indices == expected, f'M/E {me}: {indices}'


def test_multi_me_save_gates_one_mes_program_only():
    """mes[2].program=False omits only M/E 2's <Program>."""
    opts = SaveOptions()
    opts.mes[2].program = False
    root = _build_synthetic_save_xml(opts, me_count=4)
    blocks = _me_blocks_by_index(root)
    for me in (0, 1, 3):
        assert blocks[me].find('Program') is not None
    assert blocks[2].find('Program') is None
    assert blocks[2].find('Preview') is not None  # rest of M/E 2 kept


def test_multi_me_save_fully_deselected_me_emits_no_block():
    opts = SaveOptions()
    opts.mes[1] = MixEffectOptions.none()
    root = _build_synthetic_save_xml(opts, me_count=4)
    assert sorted(_me_blocks_by_index(root)) == [0, 2, 3]


def test_multi_me_save_short_mes_list_emits_only_listed_mes():
    """A 1-element mes list (the single-tab dialog mapping) emits only
    M/E 0 even on a 4-M/E switcher."""
    opts = SaveOptions(mes=[MixEffectOptions()])
    root = _build_synthetic_save_xml(opts, me_count=4)
    assert sorted(_me_blocks_by_index(root)) == [0]


def _four_me_profile():
    """A 4-block profile produced by the save side from the fake 4-M/E
    mixerstate (so apply-side tests exercise the real XML shape)."""
    return Profile(_build_synthetic_save_xml(SaveOptions(), me_count=4))


def _four_me_conn():
    """RecordingConn whose mixerstate reports me_count=4."""
    return _RecordingConn(mixerstate={
        'mixer-effect-config': {
            me: _S(keyers=(2 if me == 1 else 4)) for me in range(4)}})


def test_apply_four_blocks_targets_each_me():
    """A 4-block XML applies each block to its own M/E — the emitted
    ops carry me=0..3 with that M/E's saved source."""
    from atemwire.messages import ProgramInputCommand

    conn = _four_me_conn()
    opts = _all_off_apply_options(me_count=4)
    for me_opts in opts.mes:
        me_opts.program = True
    _four_me_profile().apply(conn, opts)

    progs = {c.index: c.source for c in conn.sent
             if isinstance(c, ProgramInputCommand)}
    assert progs == {0: 1, 1: 11, 2: 21, 3: 31}


def test_apply_mes1_usk0_false_skips_only_that_key():
    """ApplyOptions.mes[1].usk[0]=False skips exactly M/E 1's keyer 0;
    every other (me, keyer) pair in the profile still applies."""
    from atemwire.messages import KeyTypeCommand

    conn = _four_me_conn()
    opts = _all_off_apply_options(me_count=4)
    for me_opts in opts.mes:
        me_opts.usk = [True, True, True, True]
    opts.mes[1].usk[0] = False
    _four_me_profile().apply(conn, opts)

    pairs = {(c.index, c.keyer) for c in conn.sent
             if isinstance(c, KeyTypeCommand)}
    expected = {(me, k) for me in (0, 2, 3) for k in range(4)}
    expected |= {(1, 1)}   # M/E 1 saved 2 keyers; keyer 0 gated off
    assert pairs == expected


def test_apply_cross_model_four_blocks_onto_one_me_switcher():
    """A 4-block XML applied with connected me_count=1 applies only
    index 0 — no error, blocks 1..3 skipped silently."""
    from atemwire.messages import ProgramInputCommand

    conn = _RecordingConn()   # empty mixerstate → me_count 1
    opts = _all_off_apply_options(me_count=4)
    for me_opts in opts.mes:
        me_opts.program = True
    result = _four_me_profile().apply(conn, opts)

    progs = [c for c in conn.sent if isinstance(c, ProgramInputCommand)]
    assert len(progs) == 1
    assert (progs[0].index, progs[0].source) == (0, 1)
    assert result.errors == []


def test_apply_legacy_single_block_xml_still_applies():
    """Legacy single-block (index 0) XML applies unchanged through the
    findall path."""
    from atemwire.messages import ProgramInputCommand

    p = _full_me_profile()
    conn = _RecordingConn()
    result = p.apply(conn, _all_off_apply_options(restore_program=True))

    progs = [c for c in conn.sent if isinstance(c, ProgramInputCommand)]
    assert len(progs) == 1
    assert (progs[0].index, progs[0].source) == (0, 3010)
    assert result.errors == []


# ---------------------------------------------------------------------------
# DSK count (the save-side range(2) cap fix)
# ---------------------------------------------------------------------------

def test_save_emits_all_dsks_from_topology_count():
    """A 4-DSK switcher saves all four DownstreamKey blocks — the old
    range(2) loop silently dropped DSK 3/4 on multi-DSK models."""
    extra = {
        'topology': _S(downstream_keyers=4),
        'dkey-state': {i: _S(on_air=False) for i in range(4)},
        'dkey-properties-base': {
            i: _S(fill_source=10 + i, key_source=20 + i) for i in range(4)},
    }
    root = _build_synthetic_save_xml(SaveOptions(), extra_mx=extra)
    blocks = root.findall('DownstreamKeys/DownstreamKey')
    assert [b.get('index') for b in blocks] == ['0', '1', '2', '3']
    assert [b.get('fillSource') for b in blocks] == ['10', '11', '12', '13']


def test_save_dsk_count_falls_back_to_dkey_state_entries():
    """No topology packet in mixerstate → DSK count falls back to the
    dkey-state entry count (mirrors _me_count's _MeC fallback)."""
    extra = {
        'dkey-state': {i: _S(on_air=False) for i in range(3)},
    }
    root = _build_synthetic_save_xml(SaveOptions(), extra_mx=extra)
    blocks = root.findall('DownstreamKeys/DownstreamKey')
    assert [b.get('index') for b in blocks] == ['0', '1', '2']


def test_save_dsk_count_defaults_to_one_block_pre_handshake():
    """No topology AND no dkey-state (the golden fixture's shape) → one
    default DSK 0 block, matching the old guarded-range(2) emission (the
    golden test pins this byte-identically; this spells the count out)."""
    root = _build_synthetic_save_xml(SaveOptions())
    blocks = root.findall('DownstreamKeys/DownstreamKey')
    assert [b.get('index') for b in blocks] == ['0']
