# SPDX-License-Identifier: LGPL-3.0-only
"""Migration tests for the ``system_info`` feature.

Phase 3 batch 4: 4 readers migrated to atemwire.messages.system_info.
No operations — system_info is read-only at the app layer.

  video_mode             (bucket B — 2-field dict with leading-'f' strip)
  available_video_modes  (bucket B — iterates _VMC.modes list)
  video_resolution       (bucket B — calls .get_resolution() method)
  product_name           (bucket B — bytes decode via decode_name)

display_fps already lives in atemwire._state from the fade_to_black
commit, so it's not part of this migration.
"""


from atemwire.messages.system_info import (
    available_video_modes,
    product_name,
    video_mode,
    video_resolution,
)


class _VidM:
    def __init__(self, label='1080p60', mode=27, resolution=(1920, 1080)):
        self._label = label
        self.mode = mode
        self._resolution = resolution

    def get_label(self):
        return self._label

    def get_resolution(self):
        return self._resolution


class _VMC:
    def __init__(self, modes):
        self.modes = modes


class _Pin:
    def __init__(self, name=b'', product_name=None):
        self.name = name
        if product_name is not None:
            self.product_name = product_name


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def test_video_mode_returns_format_and_id():
    mx = {'video-mode': _VidM(label='1080p60', mode=27)}
    assert video_mode(mx) == {'format': '1080p60', 'id': 27}


def test_video_mode_strips_leading_f():
    """Legacy normalization: 'f1080p60' → '1080p60'."""
    mx = {'video-mode': _VidM(label='f1080p60', mode=27)}
    assert video_mode(mx)['format'] == '1080p60'


def test_video_mode_none_when_missing():
    """Distinguishes 'no video-mode packet seen yet' from a known mode."""
    assert video_mode({}) is None


def test_video_mode_label_failure_returns_empty_string():
    """If get_label() raises, fall back to empty label rather than
    crashing the snapshot assembler."""
    class _Bad:
        mode = 1
        def get_label(self):
            raise RuntimeError("bad mode")
    result = video_mode({'video-mode': _Bad()})
    assert result == {'format': '', 'id': 1}


def test_available_video_modes_iterates_and_builds_list():
    mx = {'video-mode-capability': _VMC(modes=[
        {'modenum': 27, 'mode': _VidM(label='1080p60')},
        {'modenum': 12, 'mode': _VidM(label='1080p50')},
    ])}
    result = available_video_modes(mx)
    assert {'id': 27, 'label': '1080p60'} in result
    assert {'id': 12, 'label': '1080p50'} in result
    assert len(result) == 2


def test_available_video_modes_empty_during_handshake():
    """_VMC arrives later than _ver / VidM during handshake; reader
    should return [] rather than raise."""
    assert available_video_modes({}) == []


def test_available_video_modes_skips_malformed_entries():
    class _BadField:
        def get_label(self):
            raise RuntimeError("bad")
    mx = {'video-mode-capability': _VMC(modes=[
        {'modenum': 27, 'mode': _VidM(label='1080p60')},
        {'modenum': 'not_an_int', 'mode': _VidM()},  # int() will raise
    ])}
    result = available_video_modes(mx)
    # Only the valid entry survives
    assert len(result) == 1
    assert result[0]['id'] == 27


def test_video_resolution_calls_helper():
    mx = {'video-mode': _VidM(resolution=(3840, 2160))}
    assert video_resolution(mx) == (3840, 2160)


def test_video_resolution_default_when_missing():
    assert video_resolution({}) == (1920, 1080)
    assert video_resolution({}, default=(640, 480)) == (640, 480)


def test_product_name_decodes_bytes_name():
    mx = {'product-name': _Pin(name=b'ATEM 1 M/E Constellation HD\x00\x00')}
    assert product_name(mx) == 'ATEM 1 M/E Constellation HD'


def test_product_name_falls_back_to_product_name_attr():
    """Some firmwares use ``product_name``, others use ``name``. Reader
    tries name first, then product_name."""
    mx = {'product-name': _Pin(name=None, product_name=b'Studio HD')}
    assert product_name(mx) == 'Studio HD'


def test_product_name_default_when_missing():
    assert product_name({}) == ''
    assert product_name({}, default='Unknown') == 'Unknown'
