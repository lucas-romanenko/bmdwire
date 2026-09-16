# SPDX-License-Identifier: LGPL-3.0-only
"""
atemwire.profile — Read/write Blackmagic ATEM Software Control "Save Switcher
State" XML profiles.

This is the first publicly-implemented support for the format outside of
Blackmagic's own tools. The XML is created **client-side** by ATEM Software
Control: the tool receives the same wire-protocol state-dump packets atemwire
parses, then serializes them. To restore, software walks the XML and issues
the corresponding wire commands. That's the model this module follows.

Public surface::

    from atemwire import Profile, ApplyOptions

    # Save:
    with ATEM(ip) as atem:
        profile = Profile.from_atem(atem)
        profile.to_file('/tmp/state.xml')

    # Round-trip:
    Profile.from_xml(s).to_xml()      # diff-equivalent to s

    # Restore (best-effort; most sections default ON — see ApplyOptions):
    profile = Profile.from_file('/tmp/state.xml')
    with ATEM(ip) as atem:
        result = profile.apply(atem)            # leaves Program/Preview alone
        # Program/Preview gating is per-M/E (there is no
        # restore_program_preview field):
        opts = ApplyOptions()
        opts.mes[0].program = opts.mes[0].preview = True
        result = profile.apply(atem, opts)

See ``atemwire/docs/PROFILE_FORMAT.md`` for the format reference.

Scope notes:
    - Targets the format version emitted by ATEM 1 M/E Constellation HD,
      majorVersion=2 minorVersion=1. Other models likely emit a strict
      superset; the parser is permissive (unknown attrs preserved
      verbatim) but ``apply`` may simply have nothing to do.
    - ``apply`` defaults most sections ON (video mode, input rename,
      audio, macros, hyperdecks, transitions, keyers, media players,
      …). Only the cuts-to-air gates (Program/Preview) and the
      sections with no setter implementation (multiview routing,
      camera control, talkback) default off — see ``ApplyOptions``.
    - Macros: fully restorable since 2026-04-29 — ``apply`` uploads
      macro opcode bytecode via the file-transfer path
      (``atemwire.macrotransfer``); ``restore_macros`` defaults True.

Layout (this is a package — historical ``profile.py`` was split for
maintenance):

    _enums.py    — format constants + enum lookup tables
    _xml.py      — XML serialization helpers (``_fmt``, ``_int``, ...)
    _common.py   — state / connection resolution helpers
    options.py   — ``ApplyOptions`` / ``SaveOptions`` / ``ApplyResult``
    save.py      — ``_build_*`` (read mixerstate → emit XML) +
                   ``download_media_pool_images``
    apply.py     — ``_apply_*`` (walk XML → emit wire commands)

The section-selection dialog descriptors (which used to live in
``dialog.py``) moved out to the application layer —
they're UI scaffolding for a save/load dialog, not part of the
save/restore feature.
"""

import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Optional

from atemwire.messages.system_info import product_name
from atemwire.profile._common import (
    _resolve_connection,
    _resolve_mixerstate,
    _resolve_protocol_or_none,
)
from atemwire.ready import wait_state_settled
from atemwire.profile._enums import (
    PROFILE_MAJOR_VERSION,
    PROFILE_MINOR_VERSION,
)
from atemwire.profile._xml import _int
from atemwire.profile.apply import (
    _apply_auxiliaries,
    _apply_color_generators,
    _apply_downstream_keys,
    _apply_fade_to_black,
    _apply_fairlight,
    _apply_hyperdecks,
    _apply_inputs,
    _apply_macros,
    _apply_media_players,
    _apply_program_preview,
    _apply_settings_flags,
    _apply_transition,
    _apply_upstream_keyers,
    _apply_video_mode,
)
from atemwire.profile.options import (
    ApplyOptions,
    ApplyResult,
    MixEffectOptions,
    SaveOptions,
)
from atemwire.profile.save import (
    _build_auxiliaries,
    _build_camera_control,
    _build_color_generators,
    _build_counters,
    _build_downstream_keys,
    _build_fairlight,
    _build_hyperdecks,
    _build_macro_control,
    _build_macro_pool,
    _build_media_players,
    _build_media_pool_stills,
    _build_mix_effect_blocks,
    _build_settings,
    _build_video_mode,
    download_media_pool_images,
)

logger = logging.getLogger(__name__)

# SECURITY CONTROL — DO NOT REMOVE (this is not dead code / a lint nit).
# Guards Profile.from_xml against entity-expansion ("billion laughs") DoS:
# stdlib ElementTree expands internal entities with no built-in guard, and the
# attack is only possible via a DTD/DOCTYPE carrying <!ENTITY> declarations —
# which ATEM Software Control profile XML never contains (all '<' inside values
# is escaped, so a raw '<!DOCTYPE'/'<!ENTITY' can only be a real declaration).
# Cheaper + dependency-free vs. a hardened third-party parser. Comments/CDATA
# are NOT matched ('<!--', '<![CDATA[' don't start with the keyword).
# NOTE: this text check PAIRS with the NUL-byte guard in from_xml — a UTF-16
# encoded DTD sidesteps this regex (NULs split '<!DOCTYPE') but is caught by the
# NUL guard. Both are required; see tests/unit/test_profile_xml_hardening.py.
_DTD_DECL_RE = re.compile(r'<!(?:DOCTYPE|ENTITY)\b', re.IGNORECASE)


class Profile:
    """In-memory representation of an ATEM Software Control 'Save Switcher
    State' XML profile.

    Internally the profile keeps the parsed XML tree (``self._root``) — round
    trip is automatic. Typed accessors are provided for inspection. Saving
    via ``from_atem`` builds a fresh tree from the connected ATEM's
    mixerstate. Restoring via ``apply`` walks the tree and dispatches to
    atemwire operations.
    """

    def __init__(self, root: ET.Element):
        self._root = root

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_xml(cls, xml: str) -> 'Profile':
        if isinstance(xml, bytes):
            xml = xml.decode('utf-8')
        # Wide-encoding bypass guard (must precede the DTD check). Bytes that
        # are really UTF-16/UTF-32 decode (as utf-8) to a NUL-laden string: the
        # text guard below can't see a DTD split by NULs, but ET.fromstring
        # re-encodes and lets expat AUTO-DETECT the wide encoding, reviving the
        # DTD and expanding entities (verified exploitable 2026-07-28).
        # Legitimate profile XML is UTF-8/ASCII and never contains a NUL.
        if '\x00' in xml:
            raise ValueError(
                "profile XML must be UTF-8 text (NUL byte found — wide-encoding "
                "content is refused)"
            )
        if _DTD_DECL_RE.search(xml):
            raise ValueError(
                "profile XML must not contain a DTD/DOCTYPE or <!ENTITY> "
                "declaration (entity-expansion guard)"
            )
        return cls(ET.fromstring(xml))

    @classmethod
    def from_file(cls, path) -> 'Profile':
        with open(path, 'rb') as f:
            data = f.read()
        return cls.from_xml(data)

    @classmethod
    def from_atem(cls, atem,
                  options: Optional['SaveOptions'] = None) -> 'Profile':
        """Build a Profile from a connected ATEM by reading its mixerstate.

        Accepts either an ``ATEM`` instance or any object exposing a
        ``mixerstate`` dict (an ``ATEMConnection``, an ``AtemProtocol``).

        ``options`` controls which sections are emitted. ``None``
        (default) means "everything on" — the byte-identical round-trip
        on the reference XML depends on this default behavior, so the
        no-args call must remain equivalent to ``SaveOptions()``.

        On a freshly-opened connection the ATEM's state dump arrives in
        bursts after the handshake — atemwire.ready.wait_ready returns once
        the video-mode packet shows up, but other packets (aux outputs,
        media player files, transition styles, etc.) keep streaming for
        another second or two. Block briefly here until the dump looks
        settled so the resulting profile reflects the full state.
        """
        wait_state_settled(atem)

        opts = options or SaveOptions()

        # Resolve the underlying mixerstate dict.
        mx = _resolve_mixerstate(atem)
        product = product_name(mx, '')

        root = ET.Element('Profile')
        root.set('majorVersion', str(PROFILE_MAJOR_VERSION))
        root.set('minorVersion', str(PROFILE_MINOR_VERSION))
        if product:
            root.set('product', product)

        # M/E elements are gated per cell (and per M/E) in the dialog.
        # _build_mix_effect_blocks self-gates: it loops the switcher's
        # real me_count, emits one block per M/E with anything selected
        # in opts.mes[me], and omits the MixEffectBlocks wrapper
        # entirely when every M/E is fully deselected.
        _build_mix_effect_blocks(root, mx, opts)
        if opts.downstream_keys:
            _build_downstream_keys(root, mx)
        if opts.color_generators:
            _build_color_generators(root, mx)
        if opts.auxiliaries:
            _build_auxiliaries(root, mx)
        if opts.settings:
            _build_settings(root, mx)
        if opts.video_mode:
            _build_video_mode(root, mx)
        if opts.hyperdecks:
            _build_hyperdecks(root, mx)
        if opts.fairlight:
            _build_fairlight(root, mx)
        if opts.media_players:
            _build_media_players(root, mx)
        if opts.media_pool_metadata:
            _build_media_pool_stills(root, mx)
        if opts.camera_control:
            _build_camera_control(root, mx)
        if opts.macros:
            _build_macro_pool(root, mx, atem)
        if opts.macros:
            # MacroControl is part of the macro section group; tied to
            # the same flag.
            _build_macro_control(root, mx)
        if opts.counters:
            _build_counters(root, mx)
        return cls(root)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_xml(self) -> str:
        """Serialize as XML matching ATEM Software Control's output style:
        4-space indent, self-closing tags without leading space, UTF-8
        XML declaration."""
        # NOTE: this is NOT a copy — _clone_for_serialize returns the
        # profile's own root, so ET.indent mutates our tree's whitespace
        # in place. Deliberate: nothing depends on pre-serialize
        # whitespace, and profiles are large (media pool images).
        tree_root = self._clone_for_serialize()
        ET.indent(tree_root, space='    ')
        body = ET.tostring(tree_root, encoding='unicode',
                           short_empty_elements=True)
        # Python's ET emits "<X attr="v" />" — normalize to "<X attr="v"/>".
        body = re.sub(r'\s+/>', '/>', body)
        return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'

    def to_file(self, path) -> None:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(self.to_xml())

    def _clone_for_serialize(self) -> ET.Element:
        # ET.indent mutates whitespace, which is fine for our use case
        # (we don't care about preserving whitespace from a previous
        # to_xml). Just return the root directly — callers shouldn't be
        # depending on internal whitespace.
        return self._root

    # ------------------------------------------------------------------
    # Inspection accessors
    # ------------------------------------------------------------------

    @property
    def root(self) -> ET.Element:
        """The underlying XML element. For advanced inspection / editing."""
        return self._root

    @property
    def major_version(self) -> int:
        return _int(self._root.get('majorVersion'), PROFILE_MAJOR_VERSION)

    @property
    def minor_version(self) -> int:
        return _int(self._root.get('minorVersion'), PROFILE_MINOR_VERSION)

    @property
    def product(self) -> str:
        return self._root.get('product', '')

    @property
    def video_mode(self) -> str:
        node = self._root.find('VideoMode')
        return node.get('videoMode', '') if node is not None else ''

    def macros(self) -> List[dict]:
        """List of macro dicts: ``{index, name, description, ops: [...]}``."""
        out = []
        pool = self._root.find('MacroPool')
        if pool is None:
            return out
        for m in pool.findall('Macro'):
            out.append({
                'index': _int(m.get('index')),
                'name': m.get('name', ''),
                'description': m.get('description', ''),
                'ops': [
                    {'id': o.get('id', ''), **{k: v for k, v in o.attrib.items() if k != 'id'}}
                    for o in m.findall('Op')
                ],
            })
        return out

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def apply(self, atem, options: Optional[ApplyOptions] = None) -> ApplyResult:
        """Restore this profile to a connected ATEM. Best-effort — failures
        in one section don't abort others. Returns an ``ApplyResult`` with
        per-section status.

        Apply order is set by ``_SECTION_ORDER`` row order: video mode
        first (changes resolution), program/preview last (cuts to air).
        """
        opts = options or ApplyOptions()
        result = ApplyResult()
        conn = _resolve_connection(atem)

        # Section row: (label, gated_on, action, skipped_reason)
        # - action=None → unimplemented gap; ``note_skipped`` is always
        #   emitted (with skipped_reason when the operator opted in,
        #   else the generic "gated off" reason).
        # - skipped_reason=None on an implemented section → quiet when
        #   gated off (no ``note_skipped`` is emitted).
        sections = [
            ('video_mode',       opts.restore_video_mode,
             _apply_video_mode,        'gated off (option default)'),
            ('inputs',           opts.restore_inputs,
             _apply_inputs,            'gated off (no setter op)'),
            ('settings_flags',   opts.restore_settings_flags,
             _apply_settings_flags,    None),
            ('color_generators', opts.restore_color_generators,
             _apply_color_generators,  None),
            ('aux',              opts.restore_aux,
             _apply_auxiliaries,       None),
            # Transition: next-transition (mask) and transition-style
            # (style + per-style params) gate independently, per M/E —
            # the ``_apply_transition`` body reads opts.mes and skips
            # halves per block.
            ('transition',
             any(m.next_transition or m.transition_style for m in opts.mes),
             lambda c, r, res: _apply_transition(c, r, res, opts), None),
            # USK: per-M/E per-keyer gating via opts.mes[me].usk[i].
            ('upstream_keyers',  any(any(m.usk) for m in opts.mes),
             lambda c, r, res: _apply_upstream_keyers(c, r, res, opts),
             None),
            ('downstream_keys',  opts.restore_downstream_keys,
             _apply_downstream_keys,   None),
            ('fade_to_black',    any(m.fade_to_black for m in opts.mes),
             lambda c, r, res: _apply_fade_to_black(c, r, res, opts),
             None),
            ('media_players',    opts.restore_media_players,
             _apply_media_players,     None),
            ('audio',            opts.restore_audio,
             _apply_fairlight,         'gated off (option default)'),
            ('multiview',        opts.restore_multiview,
             None,                     'multiview ops not implemented (gap)'),
            ('macros',           opts.restore_macros,
             _apply_macros,            'gated off (option default)'),
            ('camera_control',   opts.restore_camera_control,
             None,                     'camera control ops not implemented (gap)'),
            ('hyperdecks',       opts.restore_hyperdecks,
             _apply_hyperdecks,        None),
            ('talkback',         opts.restore_talkback,
             None,                     'talkback ops not implemented (gap)'),
            # Last — both halves are the cuts-to-air gate.
            ('program_preview',
             any(m.program or m.preview for m in opts.mes),
             lambda c, r, res: _apply_program_preview(c, r, res, opts),
             'gated off (default — would cut to air)'),
        ]

        for label, gated_on, action, reason in sections:
            if action is None:
                # Unimplemented gap. Emit the gap reason when opted in,
                # the generic "gated off" when not — preserves the
                # legacy dual-skip message shape.
                result.note_skipped(
                    label,
                    reason if gated_on else 'gated off (option default)',
                )
            elif gated_on:
                action(conn, self._root, result)
            elif reason is not None:
                result.note_skipped(label, reason)

        return result



__all__ = [
    'Profile',
    'ApplyOptions',
    'SaveOptions',
    'MixEffectOptions',
    'ApplyResult',
    'download_media_pool_images',
    'PROFILE_MAJOR_VERSION',
    'PROFILE_MINOR_VERSION',
]
