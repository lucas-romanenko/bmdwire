# SPDX-License-Identifier: MIT
"""videohubwire — Blackmagic Videohub Ethernet Protocol client.

Synchronous, stdlib-only. See ``client.py`` for the protocol
notes. Public surface::

    from videohubwire import Videohub, VideohubError

    with Videohub('192.0.2.31') as vh:
        snapshot = vh.state()
        vh.route(dest=3, src=12)
"""

from videohubwire.client import Videohub, VideohubError

__all__ = ['Videohub', 'VideohubError']
