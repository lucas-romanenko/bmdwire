# SPDX-License-Identifier: LGPL-3.0-only
"""
ATEM device info probe — read a few identity fields from a connection.

Intended for UIs that want to show "what kind of ATEM is at this IP, and
what video mode is it running." Goes through ``atemwire.pool.acquire_connection``
so a probe hits the warm path (~0ms) when the pool already has a connection
for this IP — and pays the full handshake cost only on a cold pool miss.

Callers that want rate-limiting or caching should wrap this in their own
layer; this module intentionally has no opinion about caching policy.

Returned dict shape:
    {
        'video_format':      '1080p50' (str) or None,
        'atem_model':        'ATEM 1 M/E Production Studio 4K' (str) or None,
        'connection_status': bool,
        'error':             str   # only present on exception
    }
"""

import logging

from atemwire.messages.system_info import product_name, video_mode
from atemwire.pool import acquire_connection

logger = logging.getLogger(__name__)


def probe(ip_address: str) -> dict:
    """Acquire a connection to an ATEM, read product-name + video-mode,
    release.

    Warm pool: ~0 ms. Cold / unreachable: pays ``atemwire.connection
    .CONNECT_TIMEOUT`` (~6 s) before returning the failure dict.

    On timeout the returned dict has ``connection_status=False`` and the
    other fields are None. On unexpected exception the dict additionally
    carries an ``error`` key with the exception message.
    """
    result = {
        "video_format": None,
        "atem_model": None,
        "connection_status": False,
    }

    try:
        with acquire_connection(ip_address) as conn:
            result["connection_status"] = True
            result["atem_model"] = product_name(conn.mixerstate, None) or None
            vm = video_mode(conn.mixerstate)
            if vm is not None:
                result["video_format"] = vm.get('format') or None
            return result

    except RuntimeError:
        # acquire_connection raises on connect timeout; return the standard
        # failure dict (not the error-populated one — timeout is expected for
        # unreachable IPs, not an unexpected exception).
        return result
    except Exception as e:
        logger.error(f"Error probing ATEM at {ip_address}: {e}")
        return {
            "video_format": None,
            "atem_model": None,
            "connection_status": False,
            "error": str(e),
        }
