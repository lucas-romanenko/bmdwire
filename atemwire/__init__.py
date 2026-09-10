# SPDX-License-Identifier: LGPL-3.0-only
"""
atemwire — control Blackmagic ATEM switchers from Python.

Typical usage::

    from atemwire import ATEM

    with ATEM('192.0.2.10') as atem:
        atem.set_program(5)
        atem.cut()
        print(atem.program_source, atem.video_mode)

For one-shot identity queries (warm if a session is already pooled
for the IP, ~6s cold)::

    from atemwire import probe
    info = probe('192.0.2.10')

Threading: ATEM instances are safe to call from any thread. Write
methods enqueue commands and return immediately; state property reads
are safe from any thread.

Internals (atemwire.messages, atemwire.protocol, atemwire.pool,
atemwire._state, atemwire.helpers, atemwire.transport, ...)
remain importable for advanced or low-level usage. The blessed public
surface is what's listed in ``__all__`` below.
"""

from atemwire.atem import ATEM
from atemwire.probe import probe
from atemwire.profile import ApplyOptions, ApplyResult, Profile

__all__ = [
    'ATEM',
    'ApplyOptions',
    'ApplyResult',
    'Profile',
    'probe',
]
