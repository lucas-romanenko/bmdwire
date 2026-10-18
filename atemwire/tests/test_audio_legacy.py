# SPDX-License-Identifier: LGPL-3.0-only
"""The legacy (pre-Fairlight) audio mixer: ``AMIP``, ``AMMO``, ``AMmO``,
``AMLv``.

Nothing here comes from a switcher. There is no capture from a legacy unit
in this repository; the payloads are built from the layouts in
``messages/audio_legacy.py`` and ``messages/meters.py``, which agree with
LibAtem's and atem-connection's. A hardware report from a Production Studio
4K or a Television Studio HD is still the only real check.

No hardware: ``AtemProtocol(ip=...)`` creates an unbound UDP socket but
starts no thread and sends nothing; ``save_field_data`` is driven directly.
"""
import struct

import pytest

from atemwire.protocol import AtemProtocol

_AMIP_FMT = '>HB 2x B x BB x Hh 2x'     # 16 bytes, ATEM 8.0 firmware on


def _amip(index, *, type=0, number=0, plug=1, state=1, volume=32768,
          balance=0, pad=0):
    return struct.pack(_AMIP_FMT, index, type, number, plug, state,
                       volume, balance) + bytes(pad)


@pytest.fixture
def proto():
    p = AtemProtocol('127.0.0.1')
    p.send_commands = lambda commands: None     # nothing reaches the wire
    try:
        yield p
    finally:
        p.transport.sock.close()
        p.transport.thread_queue.close()


@pytest.mark.parametrize('pad', [0, 4], ids=['16 bytes (8.0 on)', '20 bytes (before 8.0)'])
def test_amip_parses_either_length(proto, pad):
    """Before 8.0 firmware AMIP is 20 bytes. ``struct.unpack`` wanted exactly
    16, raised out of ``loop()`` and ended the worker: the whole connection,
    not just the audio panel."""
    proto.save_field_data(b'AMIP', _amip(3, plug=2, state=2, volume=65381,
                                         balance=-10000, pad=pad))
    node = proto.mixerstate['audio-input']['3.0']
    assert (node.index, node.type, node.plug, node.state, node.volume,
            node.balance) == (3, 0, 2, 2, 65381, -10000)
    assert node.plug_name() == 'HDMI'
