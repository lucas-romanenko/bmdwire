# SPDX-License-Identifier: MIT
"""Media pool: image upload (9996 opcode 0x0101), assignment by name,
delete and FLUSH SETTINGS, against a fake 12 4K built from the
2026-11-11 captures of Software Control."""

import struct

import pytest

from ultimattewire import (ControlValueError, Ultimatte, UltimatteError,
                           UltimatteMessageError)
from ultimattewire import media

IDLE = 1.0


def session(unit):
    u = Ultimatte("127.0.0.1", unit.port, idle_timeout=IDLE,
                  function_idle_timeout=IDLE, settings_port=unit.media_port)
    u.connect()
    return u


# ---- media helpers --------------------------------------------------------

def test_png_size_and_format_size(fake_4k):
    assert media.png_size(fake_4k.png(1920, 1080)) == (1920, 1080)
    with pytest.raises(media.UltimatteImageError):
        media.png_size(b"GIF89a" + b"\0" * 30)
    assert media.format_size("1080p25") == (1920, 1080)
    assert media.format_size("2160p50") == (3840, 2160)
    assert media.format_size("1080i59.94") == (1920, 1080)
    assert media.format_size("auto detect") is None


@pytest.mark.parametrize("name", ["", "a/b.png", "a\\b.png", "a\nb.png"])
def test_bad_names_are_refused(name):
    with pytest.raises(media.UltimatteImageError):
        media.check_name(name)


def test_upload_frame_is_the_captured_one(fake_4k):
    data = fake_4k.png(1920, 1080)
    media.upload_image("127.0.0.1", "Studio.png", data, port=fake_4k.media_port)
    frame = (struct.pack(">H", 0x0101) + b"Studio.png" + data + b"\0")
    assert fake_4k.uploads == [frame]
    assert "Studio.png" in fake_4k.pool


def test_wrong_size_is_refused_before_sending(fake_4k):
    with pytest.raises(media.UltimatteImageError, match="3840x2160"):
        media.upload_image("127.0.0.1", "Big.png", fake_4k.png(3840, 2160),
                           video_format="1080p25", port=fake_4k.media_port)
    assert fake_4k.uploads == []


# ---- the session ----------------------------------------------------------

def test_reads_the_4k_image_list_and_named_frame_buffers(fake_4k):
    fake_4k.pool["Old.png"] = (1920, 1080, b"")
    fake_4k.named["Background 1"] = "Old.png"
    with session(fake_4k) as u:
        s = u.state
        assert s.image_list == ["Old.png"]
        assert s.image_capacity == 6736052224 and s.image_available > 0
        assert s.frame_buffer.assigned["BG 1"] == "Old.png"
        assert s.frame_buffer.assigned["GM"] == ""
        assert s.frame_buffer.mix == {"BG": 0, "LY": 0}
        assert u.assigned_image("BG 1") == "Old.png"
        assert u.assigned_image("Background 1") == "Old.png"


def test_upload_assign_delete(fake_4k):
    with session(fake_4k) as u:
        assert u.upload_image("Test.png", fake_4k.png(1920, 1080)) == ["Test.png"]
        u.assign_image("BG 1", "Test.png")
        assert u.assigned_image("BG 1") == "Test.png"
        assert "Frame Buffer:\nBackground 1: Test.png" in fake_4k.received
        assert u.delete_image("Test.png") == []
        assert "Image:\nDelete: Test.png" in fake_4k.received
        assert u.assigned_image("BG 1") == ""        # the unit cleared it


def test_assign_refused_without_fg_is_an_error(fake_4k):
    fake_4k.fg_locked = False
    with session(fake_4k) as u:
        u.upload_image("Test.png", fake_4k.png(1920, 1080))
        with pytest.raises(UltimatteMessageError, match="No Foreground"):
            u.assign_image("BG 1", "Test.png")


def test_assign_refused_for_mismatched_format(fake_4k):
    with session(fake_4k) as u:
        u.upload_image("Big.png", fake_4k.png(3840, 2160), check_format=False)
        with pytest.raises(UltimatteMessageError, match="Mismatched formats"):
            u.assign_image("BG 1", "Big.png")


def test_assign_and_delete_need_an_image_in_the_pool(fake_4k):
    with session(fake_4k) as u:
        assert u.state.image_list == [] and u.state.frame_buffer.assigned
        with pytest.raises(ControlValueError):
            u.assign_image("BG 1", "Missing.png")
        with pytest.raises(ControlValueError):
            u.delete_image("Missing.png")


def test_flush_settings(fake_4k):
    with session(fake_4k) as u:
        u.flush_settings(quiet=0.2)
    assert fake_4k.flushes == 1
    assert "CONTROL:\nFLUSH SETTINGS: YES" in fake_4k.received


def test_flush_refused_without_fg(fake_4k):
    fake_4k.fg_locked = False
    with session(fake_4k) as u:
        with pytest.raises(UltimatteMessageError, match="No Foreground"):
            u.flush_settings(quiet=0.2)
    assert fake_4k.flushes == 0


def test_documented_form_still_assigns_by_number(fake_unit):
    unit = fake_unit("8K")
    with Ultimatte("127.0.0.1", unit.port, idle_timeout=IDLE) as u:
        u.assign_image("BG 1", "Newsroom")
        assert u.assigned_image("BG 1") == "Newsroom"
