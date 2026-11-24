"""The network matrix's fast subset (``tools/netmatrix/netmatrix.py
--quick``): the real client against the fake switcher through the relay,
one run of each cell, on every push. The full matrix (every path through
every profile, three runs each) is the script; see tools/netmatrix/README.md.

A cell passes when the path finished and its data checked out; the
seconds are not asserted (CI machines vary), only that it did not hang.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'netmatrix'))
import netmatrix  # noqa: E402


@pytest.mark.timeout(300)
@pytest.mark.parametrize('profile,path', netmatrix.QUICK,
                         ids=['%s-%s' % c for c in netmatrix.QUICK])
def test_cell(profile, path):
    r = netmatrix.run_one(profile, path)
    assert r['ok'] is not False, r
    assert r['ok'] is True, 'void run: %s' % r['what']
    # Every session ends: the goodbye goes three times (bmdwire#103), so
    # one lost on a lossy link no longer leaves it to the switcher's reaper.
    assert not r['ghosts'], 'sessions left on the switcher: %s' % r['ghosts']
