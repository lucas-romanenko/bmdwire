# SPDX-License-Identifier: LGPL-3.0-only
"""download_still(stall_timeout=...): wait while data arrives, give up when
it stops (2026-11-04). A 4K still is 12 MB or more on the wire and an 8K one
four times that, so one fixed timeout was either too short for them or far
too long to notice a transfer that had stopped."""
import threading
import time

import pytest

from atemwire.connection import ATEMConnection
from atemwire.tests.test_download_still import _FakeProto, _conn


class _Trickle(_FakeProto):
    """Sends progress every ``every`` seconds, ``ticks`` times, then either
    finishes or goes quiet."""

    def __init__(self, every, ticks, finish):
        super().__init__()
        self.every, self.ticks, self.finish = every, ticks, finish
        self._transfer_last_data = None

    def download(self, store, slot):
        super().download(store, slot)

        def run():
            for i in range(self.ticks):
                time.sleep(self.every)
                self._transfer_last_data = time.monotonic()
            if self.finish:
                self._raise('download-done', 0, slot, b'FRAME')
        threading.Thread(target=run, daemon=True).start()


def test_a_download_that_keeps_arriving_outlives_the_stall_timeout():
    p = _Trickle(every=0.05, ticks=12, finish=True)       # 0.6 s in all
    data = ATEMConnection.download_still(_conn(p), 1, timeout=5.0, stall_timeout=0.2)
    assert data == b'FRAME'


def test_a_download_that_stops_arriving_gives_up_after_the_stall_timeout():
    p = _Trickle(every=0.05, ticks=3, finish=False)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match='stalled'):
        ATEMConnection.download_still(_conn(p), 1, timeout=5.0, stall_timeout=0.2)
    assert time.monotonic() - started < 1.0
    assert p.aborted == 1


def test_the_whole_download_is_still_capped():
    p = _Trickle(every=0.05, ticks=100, finish=False)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r'timed out \(0.5s\)'):
        ATEMConnection.download_still(_conn(p), 1, timeout=0.5, stall_timeout=0.2)
    assert time.monotonic() - started < 1.5


def test_a_switcher_answering_busy_is_named():
    # Refused with FTDE 1 all along, the lock ours: not silence, a busy
    # transfer engine (2026-11-06: read as "no data" slot after slot).
    import types
    p = _FakeProto()
    p.locks = {0: True}
    p.transfer = types.SimpleNamespace(store=0, slot=1, try_again=7)
    with pytest.raises(TimeoutError, match='answered busy .try again. 7 times'):
        ATEMConnection.download_still(_conn(p), 1, timeout=5.0, stall_timeout=0.2)


# --- timeout alone is no-progress too (the network matrix's still_down) ------

def test_timeout_alone_is_time_without_data_not_the_whole_download():
    # 1.14.8: ``timeout`` alone capped the whole download (30 s by
    # default), and a 1080p still over 1 Mbit/s (130 s) or a slow site
    # (45 s) never made it. A download that keeps moving now takes as long
    # as the link needs.
    p = _Trickle(every=0.05, ticks=12, finish=True)       # 0.6 s in all
    data = ATEMConnection.download_still(_conn(p), 1, timeout=0.3)
    assert data == b'FRAME'


def test_timeout_alone_still_gives_up_when_nothing_arrives():
    p = _Trickle(every=0.05, ticks=2, finish=False)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r'stalled \(no data for 0.3s\)'):
        ATEMConnection.download_still(_conn(p), 1, timeout=0.3)
    assert time.monotonic() - started < 1.0


def test_a_macro_download_is_no_progress_too():
    p = _Trickle(every=0.05, ticks=12, finish=False)
    done = threading.Event()

    def finish():
        time.sleep(0.65)
        p._raise('download-done', 0xFFFF, 3, b'MACRO')
        done.set()
    threading.Thread(target=finish, daemon=True).start()
    assert ATEMConnection.download_macro(_conn(p), 3, timeout=0.3) == b'MACRO'
