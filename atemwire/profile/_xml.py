# SPDX-License-Identifier: LGPL-3.0-only
"""
XML serialization helpers — format Python values into ATEM Software
Control's attribute-string conventions, parse them back. Used by both
the save (_build_*) and apply (_apply_*) paths.
"""

from typing import Any, Optional

from atemwire.profile._enums import EXT_PORT_TYPE_NAMES


def _fmt(v: Any) -> str:
    """Format a Python value as an XML attribute string in BMD's style.

    Conventions matching the reference XML:
      - bool      → 'True' / 'False'
      - int       → plain decimal
      - float NaN → 'nan'
      - float inf → 'inf' / '-inf'
      - other float → up to 6 decimal places, trailing zeros stripped;
                      '0' for the value 0.0 (not '' or '0.0')
      - str       → as-is
    """
    if isinstance(v, bool):
        return 'True' if v else 'False'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v:
            return 'nan'
        if v == float('inf'):
            return 'inf'
        if v == float('-inf'):
            return '-inf'
        if v == 0.0:
            return '0'
        s = f'{v:.6f}'.rstrip('0').rstrip('.')
        return s if s else '0'
    return str(v)


def _bool(s: Optional[str], default: bool = False) -> bool:
    if s is None:
        return default
    return str(s).strip().lower() in ('true', '1', 'yes', 'on')


def _int(s: Optional[str], default: int = 0) -> int:
    if s is None or s == '':
        return default
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _float(s: Optional[str], default: float = 0.0) -> float:
    if s is None or s == '':
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _ext_port_type_name(bitfield: int) -> str:
    """Resolve external_port_type bitfield to XML enum name. Only the
    lowest set bit wins (matches BMD's first-port-type behavior)."""
    for bit, name in EXT_PORT_TYPE_NAMES.items():
        if bitfield & bit:
            return name
    return 'SDI'


def _next_transition_to_str(selection: dict) -> str:
    """Pack a transition-selection dict {background, key1..key4} into the
    comma-joined XML form ('Background,Key1,...'). Empty selection — which
    the ATEM rejects on the wire — falls back to 'Background'."""
    parts = []
    if selection.get('background'):
        parts.append('Background')
    for n in (1, 2, 3, 4):
        if selection.get(f'key{n}'):
            parts.append(f'Key{n}')
    return ','.join(parts) if parts else 'Background'


def _str_to_next_transition(s: str) -> dict:
    """Reverse of _next_transition_to_str."""
    parts = {p.strip() for p in (s or '').split(',') if p.strip()}
    return {
        'background': 'Background' in parts,
        'key1': 'Key1' in parts,
        'key2': 'Key2' in parts,
        'key3': 'Key3' in parts,
        'key4': 'Key4' in parts,
    }
