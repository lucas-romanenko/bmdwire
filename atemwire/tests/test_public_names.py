# SPDX-License-Identifier: LGPL-3.0-only
"""Names a downstream app reaches for are public since 1.14.1, and the
underscore names they had before still work as the same objects: a 1.x
release never drops either."""
from atemwire.messages.input_video import VideoModeField
from atemwire.state import ATEMStateMixin
from atemwire import testing


def test_mode_table_is_public_and_the_old_name_is_the_same_dict():
    assert VideoModeField._MODE_TABLE is VideoModeField.MODE_TABLE
    assert VideoModeField.MODE_TABLE[10] == (1080, False, 25, True)


def test_build_topology_is_public_and_the_old_name_is_the_same_function():
    assert ATEMStateMixin._build_topology is ATEMStateMixin.build_topology
    top = ATEMStateMixin.build_topology({})
    assert top['meCount'] == 0 and top['usksPerMe'] == []


def test_fake_video_mode_is_public_and_the_old_name_is_the_same_class():
    assert testing._FakeVideoMode is testing.FakeVideoMode
    assert testing.FakeVideoMode().get_label() == '1080p50'
