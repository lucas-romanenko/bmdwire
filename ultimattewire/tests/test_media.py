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


# ---- JPG, streaming, masks (2026-11-12 captures) ----------------------------

def test_jpeg_size_from_the_frame_header(fake_4k):
    assert media.jpeg_size(fake_4k.jpg(1920, 1080)) == (1920, 1080)
    assert media.image_size(fake_4k.jpg(3840, 2160)) == ("jpeg", (3840, 2160))
    assert media.image_size(fake_4k.png(1920, 1080)) == ("png", (1920, 1080))
    # an EXIF block in front of the SOF is walked over
    assert media.jpeg_size(fake_4k.jpg(1280, 720, app_bytes=60000)) == (1280, 720)
    with pytest.raises(media.UltimatteImageError):
        media.jpeg_size(fake_4k.jpg(1920, 1080)[:12])   # ends before the SOF
    with pytest.raises(media.UltimatteImageError, match="PNG or JPG"):
        media.image_size(b"GIF89a" + b"\0" * 30)


def test_jpg_frame_is_the_captured_one(fake_4k, monkeypatch):
    # captured: 0101 00000011 "jpg-test-1080.jpg" 0000855e <JPEG> 00
    data = fake_4k.jpg(1920, 1080)
    data += b"\0" * (34142 - len(data))
    media.upload_image("127.0.0.1", "jpg-test-1080.jpg", data,
                       video_format="1080p25", port=fake_4k.media_port)
    assert fake_4k.uploads == [b"\x01\x01" + b"jpg-test-1080.jpg" + data + b"\0"]
    head = (b"\x01\x01\x00\x00\x00\x11jpg-test-1080.jpg\x00\x00\x85\x5e\xff\xd8\xff\xe0")
    sent = {}

    def fake_stream(host, op, name, size, chunks, **kw):
        sent["frame"] = (struct.pack(">HI", op, len(name)) + name.encode()
                         + struct.pack(">I", size) + b"".join(chunks))
    monkeypatch.setattr(media, "upload_stream", fake_stream)
    media.upload_image("h", "jpg-test-1080.jpg", data)
    assert sent["frame"].startswith(head)


def test_wrong_size_jpg_is_refused_before_sending(fake_4k):
    with pytest.raises(media.UltimatteImageError, match="3840x2160"):
        media.upload_image("127.0.0.1", "Big.jpg", fake_4k.jpg(3840, 2160),
                           video_format="1080p25", port=fake_4k.media_port)
    assert fake_4k.uploads == []


def test_upload_streams_from_a_path_a_file_and_chunks(fake_4k, tmp_path):
    data = fake_4k.jpg(1920, 1080, app_bytes=50000)
    path = tmp_path / "Studio.jpg"
    path.write_bytes(data)
    calls = []
    assert media.upload_image("127.0.0.1", "A.jpg", path, chunk_size=8192,
                              progress=lambda sent, total: calls.append((sent, total)),
                              port=fake_4k.media_port) == (1920, 1080)
    assert calls[-1] == (len(data), len(data)) and len(calls) == -(-len(data) // 8192)
    assert [s for s, _ in calls] == sorted(s for s, _ in calls)
    with open(path, "rb") as f:
        media.upload_image("127.0.0.1", "B.jpg", f, port=fake_4k.media_port)
    pieces = [data[i:i + 1000] for i in range(0, len(data), 1000)]
    media.upload_image("127.0.0.1", "C.jpg", iter(pieces), size=len(data),
                       port=fake_4k.media_port)
    calls.clear()
    media.upload_image("127.0.0.1", "D.png", fake_4k.png(1920, 1080),
                       progress=lambda s, t: calls.append(s), port=fake_4k.media_port)
    assert calls == [len(fake_4k.png(1920, 1080))]          # bytes still work
    assert len(fake_4k.uploads) == 4
    assert {n: fake_4k.pool[n][2] for n in ("A.jpg", "B.jpg", "C.jpg")} == \
        {"A.jpg": data, "B.jpg": data, "C.jpg": data}


def test_streams_that_cannot_say_their_length(fake_4k):
    data = fake_4k.jpg(1920, 1080)
    with pytest.raises(media.UltimatteImageError, match="size="):
        media.upload_image("127.0.0.1", "A.jpg", iter([data]), port=fake_4k.media_port)
    # chunks short of the size announced: the frame is never finished
    with pytest.raises(media.UltimatteImageError, match="ended after"):
        media.upload_image("127.0.0.1", "B.jpg", iter([data]), size=len(data) + 10,
                           port=fake_4k.media_port)
    with pytest.raises(media.UltimatteImageError, match="longer"):
        media.upload_image("127.0.0.1", "C.jpg", iter([data, b"x"]), size=len(data),
                           port=fake_4k.media_port)
    assert fake_4k.pool == {}


def test_a_progress_callback_can_abandon_the_upload(fake_4k):
    def stop(sent, total):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        media.upload_image("127.0.0.1", "A.png", fake_4k.png(1920, 1080),
                           progress=stop, port=fake_4k.media_port)
    assert fake_4k.pool == {}


def test_session_uploads_and_assigns_a_jpg(fake_4k, tmp_path):
    path = tmp_path / "jpg-test-1080.jpg"
    path.write_bytes(fake_4k.jpg(1920, 1080))
    seen = []
    with session(fake_4k) as u:
        assert u.upload_image("jpg-test-1080.jpg", path,
                              progress=lambda s, t: seen.append(s)) \
            == ["jpg-test-1080.jpg"]
        assert seen
        u.assign_image("Background 1", "jpg-test-1080.jpg")
        u.assign_image("Background 2", "jpg-test-1080.jpg")
        assert u.assigned_image("BG 2") == "jpg-test-1080.jpg"
        # the unit sets the BG matte input itself
        u.wait_for(lambda s: s.controls.get("BM Input Level") == 6878)
        assert u.state.controls["BM Input Offset"] == 10000


@pytest.mark.parametrize("buffer,short,prefix", [
    ("Garbage Matte", "GM", "GM"), ("Holdout Matte", "HM", "HM"),
    ("GM", "GM", "GM"), ("hm", "HM", "HM")])
def test_masks_assign_by_name(fake_4k, buffer, short, prefix):
    with session(fake_4k) as u:
        u.upload_image("mask.png", fake_4k.png(1920, 1080))
        u.assign_image(buffer, "mask.png")
        assert u.assigned_image(short) == "mask.png"
        long = {"GM": "Garbage Matte", "HM": "Holdout Matte"}[short]
        assert f"Frame Buffer:\n{long}: mask.png" in fake_4k.received
        u.wait_for(lambda s: s.controls.get(f"{prefix} Input Level") == 6878)
        assert u.state.controls[f"{prefix} Input Offset"] == 10000
        assert u.state.controls[f"{prefix} Enable"] is False   # not turned on
