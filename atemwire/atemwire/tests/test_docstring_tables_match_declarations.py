# SPDX-License-Identifier: LGPL-3.0-only
"""Every Send / Recv docstring offset table agrees with the class's DSL
declarations (0.15).

The tables are the documentation people read; the declarations are what
goes on the wire. Before this test they were the same fact written twice,
and the DVE-rate bug of 2026-08 lived exactly in that gap. Rules:

- Each declared field needs a table row at its offset with the same size
  and a compatible type (``bool`` rows accept ``boolean`` or ``u8``
  declarations, ``s16`` means ``i16``, an array row such as ``u8[]`` or
  ``bytes`` covers every declared byte inside its span, a ``tail`` matches
  any row at ``SIZE``).
- Each typed table row needs a declared field at its offset, unless it is
  the mask (``MASK_AT``), is typed ``?`` / ``x`` / ``...``, describes
  itself as unknown / padding / reserved / unused, or its offset is listed
  in the class's ``HAND_PARSED_OFFSETS`` (bytes the class decodes by hand
  and documents, like an IPv4 address).
- Classes that carry a table but pack or parse entirely by hand are listed
  in ``HAND_CODED``. A new one must be declared or added here; one that
  gains declarations must be removed here, so the list cannot rot.
"""

import importlib
import inspect
import pkgutil
import re

import atemwire.messages as pkg
from atemwire.messages._dsl import Recv, Send, boolean, i8, i16, i32, string, tail, u8, u16, u32

HAND_CODED = frozenset({
    'audio_legacy.AudioMixerTallyField',
    'camera_control.CameraControlDataPacketField',
    'fairlight.FairlightAudioInputField',
    'fairlight.FairlightHeadphonesField',
    'fairlight.FairlightMasterCompressorPropertiesCommand',
    'fairlight.FairlightMasterCompressorPropertiesField',
    'fairlight.FairlightMasterEqBandPropertiesCommand',
    'fairlight.FairlightMasterLimiterPropertiesCommand',
    'fairlight.FairlightMasterLimiterPropertiesField',
    'fairlight.FairlightMasterPropertiesCommand',
    'fairlight.FairlightMasterPropertiesField',
    'file_transfer.FileTransferDataField',
    'file_transfer.TransferDataCommand',
    'file_transfer.TransferFileDataCommand',
    'hyperdeck.HyperdeckSettingsCommand',
    'input_video.InputPropertiesField',
    'input_video.VideoModeCapabilityField',
    'macros.MacroPlayStatusField',
    'macros.MacroPropertiesField',
    'macros.MacroRecordCommand',
    'media.MediaplayerFileInfoField',
    'meters.AudioMeterLevelsField',
    'meters.FairlightMasterLevelsField',
    'meters.FairlightMeterLevelsField',
    'supersource.SupersourceBoxPropertiesField',
    'supersource.SupersourcePropertiesField',
    'tally.TallyIndexField',
    'tally.TallySourceField',
    'transition.TransitionSettingsField',
    'upstream_keyer.KeyPropertiesFlyKeyframeField',
})

TYPE_TOKENS = {
    u8: {'u8', 'uint8', 'int8'}, u16: {'u16', 'uint16'}, u32: {'u32', 'uint32'},
    i8: {'i8', 's8', 'int8'}, i16: {'i16', 's16', 'int16'}, i32: {'i32', 's32', 'int32'},
    boolean: {'bool', 'boolean', 'u8'}, string: {'str', 'string', 'char'},
}
ROW = re.compile(r'^\s*(\d+)\s+(\d+|\.\.\.|\?)\s+(\S+)\s*(.*)$')
EXEMPT_TYPES = {'?', 'x', '...', '-'}
EXEMPT_WORDS = ('unknown', 'padding', 'reserved', 'unused')


def parse_table(doc):
    rows, in_table, seen_header = [], False, False
    for line in (doc or '').splitlines():
        t = line.strip()
        if t.startswith('======'):
            if not in_table:
                in_table = True
            elif seen_header and rows:
                in_table = False
            continue
        if in_table and t.startswith('Offset'):
            seen_header = True
            continue
        if in_table and seen_header:
            m = ROW.match(line)
            if m:
                rows.append((int(m.group(1)), m.group(2), m.group(3).lower(), m.group(4).strip()))
    return rows


def message_classes():
    for m in pkgutil.iter_modules(pkg.__path__):
        if m.name.startswith('_'):
            continue
        mod = importlib.import_module(f'atemwire.messages.{m.name}')
        for name, cls in inspect.getmembers(mod, inspect.isclass):
            if cls.__module__ == mod.__name__ and issubclass(cls, (Send, Recv)) and cls not in (Send, Recv):
                yield f'{m.name}.{name}', cls


def check(cls, rows):
    """Return a list of human-readable disagreements for one class."""
    out = []
    fields = cls._fields
    arrays = [(o, int(sz)) for o, sz, ty, d in rows if (ty.endswith('[]') or ty == 'bytes') and sz.isdigit()]
    by_off = {}
    for o, sz, ty, d in rows:
        by_off.setdefault(o, []).append((sz, ty, d))
    for fname, f in fields.items():
        if isinstance(f, tail):
            if not any(o == f.at for o in by_off):
                out.append(f'tail field {fname} at {f.at} has no table row')
            continue
        if any(o <= f.at < o + n for o, n in arrays) and isinstance(f, u8):
            continue
        cands = by_off.get(f.at, [])
        if not any((not sz.isdigit() or int(sz) == f.size) and ty in TYPE_TOKENS.get(type(f), set())
                   for sz, ty, d in cands):
            out.append(f'field {fname} = {type(f).__name__}(at={f.at}, size={f.size}) vs table rows {cands or "none"}')
    declared = {f.at for f in fields.values()}
    hand = set(getattr(cls, 'HAND_PARSED_OFFSETS', ()))
    for o, sz, ty, d in rows:
        if ty in EXEMPT_TYPES or ty.endswith('[]') or ty == 'bytes':
            continue
        if any(w in d.lower() for w in EXEMPT_WORDS):
            continue
        if 'mask' in d.lower() and getattr(cls, 'MASK_AT', None) == o:
            continue
        if o in declared or o in hand:
            continue
        out.append(f'table row {o} {sz} {ty} "{d[:50]}" has no declared field')
    return out


def test_every_table_agrees_with_its_declarations():
    failures, seen_hand_coded = [], set()
    for qual, cls in message_classes():
        rows = parse_table(cls.__doc__)
        if not rows:
            continue
        if not cls._fields:
            seen_hand_coded.add(qual)
            if qual not in HAND_CODED:
                failures.append(f'{qual}: has an offset table but declares no fields; declare them or add it to HAND_CODED')
            continue
        if qual in HAND_CODED:
            failures.append(f'{qual}: declares fields now; remove it from HAND_CODED')
        for problem in check(cls, rows):
            failures.append(f'{qual}: {problem}')
    stale = HAND_CODED - seen_hand_coded
    for qual in sorted(stale):
        failures.append(f'{qual}: listed in HAND_CODED but not found (renamed, removed, or no table); update the list')
    assert not failures, '\n' + '\n'.join(failures)


def test_declared_classes_are_the_majority():
    """A guard on direction: the DSL is the norm, hand-coding the exception."""
    declared = sum(1 for _, cls in message_classes() if cls._fields)
    assert declared > 3 * len(HAND_CODED)
