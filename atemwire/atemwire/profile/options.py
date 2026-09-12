# SPDX-License-Identifier: LGPL-3.0-only
"""
Save / Apply / Result dataclasses for the profile package.

``ApplyOptions`` and ``SaveOptions`` are passed in by callers to gate
which sections of a profile are written or read; ``ApplyResult`` is
what ``Profile.apply`` returns.
"""

import logging
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


# ATEM platform ceiling on M/E count (Constellation 8K). Default option
# lists are built at this length so a no-args SaveOptions()/ApplyOptions()
# means "everything on" on any model — save/apply only ever loop the
# connected switcher's real me_count, so extra entries are inert.
PLATFORM_MAX_MES = 4


@dataclass
class MixEffectOptions:
    """Per-M/E selection — the six per-element flags of one
    ``<MixEffectBlock>`` in Software Control's Save / Restore dialog
    grid (Program, Preview, Next Transition, Transition Style, Fade to
    Black, USK 1-4).

    Shared between ``SaveOptions.mes`` (gates element emission) and
    ``ApplyOptions.mes`` (gates element restore); the save/apply
    naming difference (bare vs ``restore_*``) lives on the coarse
    top-level flags only — inside the per-M/E block the names are bare.
    """

    program: bool = True
    preview: bool = True
    next_transition: bool = True   # selection mask (BG / Key1-4)
    transition_style: bool = True  # style + per-style params
    fade_to_black: bool = True
    # usk[i] gates the Key element with index=i. Save sizes the loop to
    # the M/E's real keyer count; apply skips Keys whose index is out
    # of range of this list.
    usk: List[bool] = field(
        default_factory=lambda: [True, True, True, True])

    def any_selected(self) -> bool:
        """True when at least one of the M/E's elements is selected —
        a fully-deselected M/E emits/applies nothing (no block)."""
        return (self.program or self.preview or self.next_transition
                or self.transition_style or self.fade_to_black
                or any(self.usk))

    @classmethod
    def none(cls) -> 'MixEffectOptions':
        """Fully-deselected M/E (no element saved/applied)."""
        return cls(program=False, preview=False, next_transition=False,
                   transition_style=False, fade_to_black=False,
                   usk=[False, False, False, False])

    @classmethod
    def apply_default(cls) -> 'MixEffectOptions':
        """Apply-side default: everything on except the cuts-to-air
        gates (Program/Preview), mirroring the historical flat
        ``restore_*`` defaults."""
        return cls(program=False, preview=False)


class _PerMeOptions:
    """Shared ``mes[]`` accessor for SaveOptions / ApplyOptions."""

    def me_options(self, me: int) -> MixEffectOptions:
        """The per-M/E option block for M/E ``me``. Indices beyond the
        list are fully deselected — save emits no block, apply skips
        the block — so a short ``mes`` list (e.g. the single-tab
        dialog's 1-element mapping) leaves higher M/Es untouched."""
        if 0 <= me < len(self.mes):
            return self.mes[me]
        return MixEffectOptions.none()


@dataclass
class ApplyOptions(_PerMeOptions):
    """Per-section flags for ``Profile.apply``.

    Granular at the level of the M/E grid in Software Control's Save /
    Restore dialog: per-M/E ``MixEffectOptions`` blocks under ``mes``
    (Program, Preview, Next Transition, Transition Style, Fade to
    Black, USK 1-4 — one block per M/E) plus coarse flags for
    everything outside the box. Each flag controls exactly the XML
    element it names — no coalescing.

    Defaults are conservative: Program/Preview not changed (cuts-to-air
    gate); sections with no setter implementation (multiview routing,
    camera control apply, talkback) gated off.
    """

    # Per-M/E blocks. mes[i] gates the <MixEffectBlock index="i">;
    # blocks whose index is beyond the list are skipped entirely, and
    # blocks whose index is beyond the connected switcher's me_count
    # are skipped regardless (cross-model load).
    mes: List[MixEffectOptions] = field(
        default_factory=lambda: [MixEffectOptions.apply_default()
                                 for _ in range(PLATFORM_MAX_MES)])

    # Sections still gated off because no setter operation exists in
    # atemwire (see PROFILE_FORMAT.md gaps). Flipping these to True is a
    # no-op until the underlying ops are implemented.
    restore_multiview: bool = False
    restore_camera_control: bool = False
    restore_talkback: bool = False

    # HyperDeck bindings (network address + switcher input) — implemented via
    # CXMS (2026-06-09), so on by default like the other restorable sections.
    restore_hyperdecks: bool = True

    # Fly keyframes (A/B). Default OFF like the cuts-to-air gates: restoring
    # a keyframe drives the LIVE USK DVE through that keyframe's geometry
    # (the ATEM has no direct keyframe-write — see _apply_fly_keyframe), so
    # it's visible on-air. Also gated per-keyer by mes[me].usk[i].
    restore_fly_keyframes: bool = False

    # Switcher-global (siblings of the M/E box in the dialog).
    restore_downstream_keys: bool = True
    restore_color_generators: bool = True
    restore_audio: bool = True            # Fairlight master + per-strip
                                          # gain/EQ + per-band EQ + per-strip
                                          # compressor/limiter/expander +
                                          # master compressor/limiter (CMCP/
                                          # CMLP). Master has no Expander.

    # On by default — these sections all have working ops. Cloning one
    # switcher's configuration onto another wants them on.
    restore_video_mode: bool = True       # destructive (resync); see
                                          # _apply_video_mode for the
                                          # warning + settle wait.
    restore_inputs: bool = True           # input rename (long/short labels)
    restore_macros: bool = True           # MSRc → replay ops as control
                                          # commands → MAct stop. ATEM records
                                          # server-side; see MACRO_FORMAT.md.
    restore_aux: bool = True
    restore_media_players: bool = True
    restore_settings_flags: bool = True   # FtB enabled etc.

    # Upload media-pool image data alongside metadata. ``Profile.apply``
    # itself never touches image bytes: when True and the profile XML's
    # <MediaPool><Stills> entries reference image files, the caller is
    # expected to supply those bytes and push them into the media pool
    # through its own upload path after applying the rest of the config.
    # Defaults True so cloning one switcher's configuration onto another
    # moves the media pool too.
    restore_media_pool_images: bool = True

    @classmethod
    def macros_only(cls) -> 'ApplyOptions':
        """Restore the <MacroPool> section and nothing else.

        For callers (e.g. a macro-swap upload job) whose
        intent is fixed at "exactly the macros, never anything else,"
        not an operator-tunable choice. Lists every field explicitly so
        any future True-by-default addition to ``ApplyOptions`` stays
        off here instead of silently turning on.
        """
        return cls(
            mes=[MixEffectOptions.none() for _ in range(PLATFORM_MAX_MES)],
            restore_multiview=False,
            restore_camera_control=False,
            restore_hyperdecks=False,
            restore_talkback=False,
            restore_fly_keyframes=False,
            restore_downstream_keys=False,
            restore_color_generators=False,
            restore_audio=False,
            restore_video_mode=False,
            restore_inputs=False,
            restore_macros=True,
            restore_aux=False,
            restore_media_players=False,
            restore_settings_flags=False,
            restore_media_pool_images=False,
        )


@dataclass
class SaveOptions(_PerMeOptions):
    """Section filter for ``Profile.from_atem``.

    Granular at the level of the M/E grid in Software Control's Save
    dialog: per-M/E ``MixEffectOptions`` blocks under ``mes``
    (program, preview, next_transition, transition_style,
    fade_to_black, usk[] — one block per M/E) plus coarse flags for
    everything outside the box. Each flag gates emission of exactly
    the corresponding XML element — no coalescing.

    All flags default to True — the no-args call produces a profile
    with every supported section emitted, preserving the byte-identical
    round-trip on the reference XML. Set a flag False to skip the
    corresponding ``<...>`` element from the output.

    A section-selection dialog in the application layer typically
    drives this from the operator's checkbox state.
    """

    # Per-M/E blocks. Save loops the connected switcher's real
    # me_count and emits one <MixEffectBlock index="N"> per M/E whose
    # mes[N] has anything selected; a fully-deselected (or missing —
    # index beyond the list) M/E emits no block.
    mes: List[MixEffectOptions] = field(
        default_factory=lambda: [MixEffectOptions()
                                 for _ in range(PLATFORM_MAX_MES)])

    # Switcher / global.
    downstream_keys: bool = True
    color_generators: bool = True
    fairlight: bool = True
    camera_control: bool = True

    # Media
    media_pool_metadata: bool = True       # the <MediaPool> XML element
    media_pool_images: bool = True         # capture each used slot's PNG
    media_players: bool = True

    # Input / Output
    auxiliaries: bool = True
    counters: bool = True

    # Others
    settings: bool = True
    video_mode: bool = True
    hyperdecks: bool = True
    macros: bool = True


@dataclass
class ApplyResult:
    """Per-section breakdown of what apply did."""

    applied: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def note_applied(self, section: str) -> None:
        self.applied.append(section)

    def note_skipped(self, section: str, reason: str = '') -> None:
        self.skipped.append(f"{section}: {reason}" if reason else section)

    def note_error(self, section: str, exc: Exception) -> None:
        self.errors.append(f"{section}: {type(exc).__name__}: {exc}")
        logger.warning("Profile apply error in %s: %s", section, exc)

    def summary(self) -> str:
        parts = []
        if self.applied:
            parts.append(f"applied: {len(self.applied)}")
        if self.skipped:
            parts.append(f"skipped: {len(self.skipped)}")
        if self.errors:
            parts.append(f"errors: {len(self.errors)}")
        return ', '.join(parts) if parts else 'no changes'
