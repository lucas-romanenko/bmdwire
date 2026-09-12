# SPDX-License-Identifier: MIT
"""
ultimattewire — Blackmagic Ultimatte 12 / 12 4K control library.

Protocol layer reverse-engineered from packet captures against real
hardware; opcodes, frame format, byte order, and sequence must match
the unit's parser exactly. See ultimattewire.profile for the Smart
Remote-compatible Archive/Restore feature (the first public
implementation of it outside BMD's own tools).
"""

from ultimattewire.profile import archive_unit_to_bytes, restore_unit_from_bytes

__all__ = [
    "archive_unit_to_bytes",
    "restore_unit_from_bytes",
]
