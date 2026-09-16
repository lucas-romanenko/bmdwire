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

Two layers, both supported from 1.0.0. ``__all__`` below is the short
way in. Building your own frontend needs more than that, so these are
public too and covered by the version number:

    atemwire.state       ATEMStateMixin / build_full_state — the whole
                         switcher as one dict, plus me_count,
                         me_keyer_count, decode_name, md5_hex
    atemwire.messages.*  one module per feature, holding that feature's
                         wire formats, operations and readers
    atemwire.pool        acquire_connection / ATEMInstanceManager — one
                         pooled session per switcher
    atemwire.profile     save and restore switcher state as XML
    atemwire.ready       wait_ready

Below that line (protocol, transport, helpers and anything named with a
leading underscore) is the wire itself. It is importable, and it may
change in a minor release.
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
