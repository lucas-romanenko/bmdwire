# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""
Manual / catch-all field — lets callers construct a fake "received"
field with any 4-char code and arbitrary raw bytes. Used by tests and
any feature that needs to synthesize a packet outside the normal
``FIELDNAME_PRETTY`` dispatch.

Doesn't fit the DSL pattern (the wire CODE is dynamic, not class-level)
so it lives as a hand-written subclass.
"""

from atemwire.messages._dsl import Recv


class ManualField(Recv):
    """Catch-all field whose wire code is set per-instance."""
    CODE = ''

    def __init__(self, fieldcode, raw):
        self.CODE = fieldcode
        self.raw = raw
        self.fieldcode = fieldcode

    def __repr__(self):
        return f'<manual {self.fieldcode}>'
