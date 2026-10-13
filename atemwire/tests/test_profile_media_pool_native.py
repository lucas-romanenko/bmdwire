# SPDX-License-Identifier: LGPL-3.0-only
"""download_media_pool_images on the native interleaved path
(ASC-style step 3, 2026-07-02).

Contract under test:
  * requires an ATEM/ATEMConnection exposing ``download_still`` (a bare
    protocol has no worker pumping the transfer) and a ready connection;
  * a per-request cache hit skips the wire entirely (no download_still
    call) and marks the entry from_cache;
  * fresh slots call conn.download_still (not the raw/exclusive path) —
    per-slot failures are skipped, not fatal.
"""
import types

import pytest

from atemwire.profile.save import download_media_pool_images


def _mpfe(is_used=True, name='Still', hash_bytes=b'\x11' * 16):
    return types.SimpleNamespace(is_used=is_used, name=name, hash=hash_bytes)


def _conn(slots, download=None, connected=True, ready=None):
    ns = types.SimpleNamespace()
    ns.is_connected = connected
    ns.is_ready = connected if ready is None else ready
    ns.mixerstate = {
        'mediaplayer-file-info': slots,
        'video-mode': None,
    }
    calls = []

    def _download_still(slot, timeout=30.0, **kw):
        calls.append(slot)
        if download is None:
            raise RuntimeError('wire should not be touched')
        return download(slot)

    ns.download_still = _download_still
    ns._download_calls = calls
    return ns


def test_requires_native_capable_connection():
    bare = types.SimpleNamespace(mixerstate={})   # no download_still
    with pytest.raises(RuntimeError, match='download_still'):
        download_media_pool_images(bare)


def test_requires_ready_connection():
    conn = _conn({}, connected=False)
    with pytest.raises(RuntimeError, match='not ready'):
        download_media_pool_images(conn)


def test_requires_a_current_session():
    # bmdwire#8: connected (worker alive) but mid re-handshake.
    conn = _conn({0: _mpfe()}, connected=True, ready=False)
    with pytest.raises(RuntimeError, match='not ready'):
        download_media_pool_images(conn)
    assert conn._download_calls == []


def test_a_conn_without_is_ready_falls_back_to_is_connected():
    conn = _conn({0: _mpfe(name='A')}, download=lambda slot: b'\x00' * 16)
    del conn.is_ready
    conn.mixerstate['video-mode'] = types.SimpleNamespace(get_resolution=lambda: (2, 2))
    out = download_media_pool_images(conn)
    assert [e['slot'] for e in out] == [0]


def test_capture_stops_when_the_session_goes():
    # The connection stops being ready after the first slot: the capture
    # stops there instead of sitting out a timeout on every remaining slot.
    def download(slot):
        conn.is_ready = False                     # re-handshake starts
        return b'\x00' * 16

    conn = _conn({0: _mpfe(name='A'), 1: _mpfe(name='B'), 2: _mpfe(name='C')},
                 download=download)
    conn.mixerstate['video-mode'] = types.SimpleNamespace(get_resolution=lambda: (2, 2))

    out = download_media_pool_images(conn)

    assert conn._download_calls == [0]
    assert [e['slot'] for e in out] == [0]        # the partial list


def test_cache_hit_skips_wire_entirely():
    conn = _conn({0: _mpfe(name='Logo')})         # download_still would raise

    out = download_media_pool_images(
        conn,
        cache_get=lambda slot, hash_hex: b'PNGBYTES',
        cache_put=lambda *a: (_ for _ in ()).throw(AssertionError('no put on hit')),
    )

    assert conn._download_calls == []             # never touched the wire
    assert out == [{'slot': 0, 'name': 'Logo',
                    'png_bytes': b'PNGBYTES', 'from_cache': True}]


def test_fresh_slot_failure_is_skipped_not_fatal():
    conn = _conn({0: _mpfe(name='A'), 2: _mpfe(name='B')},
                 download=lambda slot: (_ for _ in ()).throw(
                     TimeoutError('slot busy')))

    out = download_media_pool_images(conn)

    assert conn._download_calls == [0, 2]         # tried both via native path
    assert out == []                              # both skipped, no raise
