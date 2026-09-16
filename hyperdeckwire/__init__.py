# SPDX-License-Identifier: MIT
"""hyperdeckwire — Blackmagic HyperDeck Studio control library.

Three pieces, one per channel the deck offers:

* ``Hyperdeck`` — the HyperDeck Ethernet Protocol (TCP 9993, text /
  line-oriented): transport, clips, timeline, codec configuration.
* ``upload_clip`` — FTP file upload onto the deck's storage.
* ``HyperdeckSetup`` — the deck's configuration API on its HTTP port, the
  one HyperDeck Setup drives: name, network, the FTP / Web Media Manager /
  Ethernet Protocol switches, certificate, users, date and time, reboot.
  None of those settings exist in the 9993 protocol.

Playback and upload stay on 9993 + FTP on purpose: every networked
HyperDeck has them, while the HTTP media/transport REST API of firmware
8.x is only on the Plus/Pro/HDR/Shuttle models. The configuration API is
on the Studio HD Mini too (checked on 9.0.2).

The 9993 protocol is documented in BMD's
``HyperDeckEthernetProtocol.pdf`` (December 2024).

Public surface::

    from hyperdeckwire import Hyperdeck, upload_clip

    # Control:
    with Hyperdeck('192.0.2.11') as hd:
        info = hd.device_info()        # dict
        clips = hd.disk_list()         # List[Clip]
        hd.stop()
        hd.clips_clear()
        hd.clips_add('my-clip.mp4')
        hd.play(loop=True, single_clip=True)

    # File upload:
    result = upload_clip('192.0.2.11', '/path/to/clip.mp4')
    print(result.throughput_mb_s)

    # Configuration (what HyperDeck Setup shows):
    deck = HyperdeckSetup('192.0.2.11')
    deck.network_access()              # {'FTP': 'Enabled', 'HTTP': 'Enabled'}
    deck.set_network(netmask='255.255.248.0')
"""

from hyperdeckwire.client import (
    Clip,
    Hyperdeck,
    HyperdeckError,
    Response,
)
from hyperdeckwire.setup import (
    HyperdeckSetup,
    HyperdeckSetupError,
    NetworkInterface,
    RemoteAdmin,
    SetupInfo,
    User,
)
from hyperdeckwire.upload import UploadResult, upload_clip

__all__ = [
    'Clip',
    'Hyperdeck',
    'HyperdeckError',
    'HyperdeckSetup',
    'HyperdeckSetupError',
    'NetworkInterface',
    'RemoteAdmin',
    'Response',
    'SetupInfo',
    'UploadResult',
    'User',
    'upload_clip',
]
