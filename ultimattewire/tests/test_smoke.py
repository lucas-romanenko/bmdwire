# SPDX-License-Identifier: MIT
"""python -m ultimattewire.smoke, against the fake unit: read only unless
--write, and a write is always put back."""

import io
import json
import socket
import struct
import threading

from ultimattewire import smoke


def run(*argv):
    out = io.StringIO()
    rc = smoke.main([str(a) for a in argv], out=out)
    return rc, out.getvalue()


def test_read_only_by_default(fake_unit):
    unit = fake_unit("8K")
    rc, text = run("127.0.0.1", "--port", unit.port, "--idle-timeout", 1)
    assert rc == 0
    # every block it sent was a status dump request: a header, no fields
    assert unit.received and all("\n" not in b for b in unit.received)
    assert len(unit.received) == len(smoke.DUMP_HEADERS)
    assert "model         'Ultimatte 12 8K'" in text
    assert "controls      289" in text
    assert "request CAMERACONTROL        NAK" in text
    assert "== differences from the protocol document\n  none" in text


def test_differences_from_the_document_are_listed(fake_unit):
    unit = fake_unit("HD")
    unit.controls["Active Unit"] = "off"
    unit.controls["Talent Mirror"] = "off"
    del unit.controls["Cool"]
    unit.controls["Matte Density"] = "20000"
    rc, text = run("127.0.0.1", "--port", unit.port, "--idle-timeout", 1)
    assert rc == 0
    assert "control not in the document: 'Active Unit'" in text
    assert "the document says the Ultimatte 12 HD has no 'Talent Mirror'" in text
    assert "documented control missing on the unit: 'Cool'" in text
    assert "'Matte Density' = 20000 outside the document's 0-10000" in text


def test_write_changes_one_step_and_restores(fake_unit):
    unit = fake_unit("8K")
    rc, text = run("127.0.0.1", "--port", unit.port, "--idle-timeout", 1, "--write")
    assert rc == 0
    writes = [b for b in unit.received if "\n" in b]
    assert writes == ["CONTROL:\nMatte Density: 5001", "CONTROL:\nMatte Density: 5000"]
    assert unit.controls["Matte Density"] == "5000"
    assert "restored Matte Density to 5000 OK" in text


def test_write_at_the_top_of_the_range_steps_down(fake_unit):
    unit = fake_unit("8K")
    unit.controls["Cool"] = "10000"
    rc, _ = run("127.0.0.1", "--port", unit.port, "--idle-timeout", 1,
                "--write", "--control", "Cool")
    assert rc == 0
    assert [b for b in unit.received if "\n" in b] == [
        "CONTROL:\nCool: 9999", "CONTROL:\nCool: 10000"]


def test_write_refused_on_air_unless_forced(fake_unit):
    unit = fake_unit("8K")
    unit.controls["Tally Active"] = "on"
    rc, text = run("127.0.0.1", "--port", unit.port, "--idle-timeout", 1, "--write")
    assert rc == 1 and "Tally Active is on" in text
    assert not [b for b in unit.received if "\n" in b]
    rc, _ = run("127.0.0.1", "--port", unit.port, "--idle-timeout", 1, "--write", "--force")
    assert rc == 0 and unit.controls["Matte Density"] == "5000"


def test_write_refuses_a_control_that_isnt_numeric(fake_unit):
    unit = fake_unit("8K")
    rc, text = run("127.0.0.1", "--port", unit.port, "--idle-timeout", 1,
                   "--write", "--control", "Matte Enable")
    assert rc == 1 and "isn't a numeric control" in text


def test_a_failed_write_is_still_restored(fake_unit):
    unit = fake_unit("8K")
    original = unit._control

    def refuse_once(sock, items, state={"n": 0}):
        state["n"] += 1
        if state["n"] == 1:
            return unit._send(sock, "NAK\n\n")
        return original(sock, items)
    unit._control = refuse_once
    rc, text = run("127.0.0.1", "--port", unit.port, "--idle-timeout", 1, "--write")
    assert rc == 1
    assert "write failed" in text and "restored Matte Density to 5000 OK" in text


def test_watch_prints_pushes(fake_unit):
    unit = fake_unit("8K")
    timer = threading.Timer(0.4, unit.panel, ("CONTROL", {"Cool": "42"}))
    timer.start()
    try:
        rc, text = run("127.0.0.1", "--port", unit.port, "--idle-timeout", 1,
                       "--watch", 1.0)
    finally:
        timer.cancel()
    assert rc == 0
    assert "CONTROL: {'Cool': 42}" in text and "1 updates" in text


def test_json_state(fake_unit, tmp_path):
    unit = fake_unit("HD Mini")
    path = tmp_path / "state.json"
    rc, _ = run("127.0.0.1", "--port", unit.port, "--idle-timeout", 1, "--json", path)
    assert rc == 0
    assert json.loads(path.read_text())["identity"]["model"] == "Ultimatte 12 HD Mini"


def test_no_unit_exits_2():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    rc, text = run("127.0.0.1", "--port", port)
    assert rc == 2 and "could not connect" in text


# ---- --image / --flush (the 12 4K as captured) ------------------------------

def run_4k(unit, *extra):
    return run("127.0.0.1", "--port", unit.port, "--settings-port",
               unit.media_port, "--idle-timeout", 1, *extra)


def test_image_uploads_assigns_and_undoes(fake_4k):
    fake_4k.pool["Studio.png"] = (1920, 1080, b"")
    fake_4k.named["Background 1"] = "Studio.png"
    rc, text = run_4k(fake_4k, "--image")
    assert rc == 0, text
    assert "uploaded ultimattewire-smoke.png (png 1920x1080" in text
    assert "unit shows 'ultimattewire-smoke.png' OK" in text
    assert "put 'Studio.png' back on BG 1 OK" in text
    assert list(fake_4k.pool) == ["Studio.png"]          # the test image is gone
    assert fake_4k.named["Background 1"] == "Studio.png"
    w, h = struct.unpack(">II", fake_4k.uploads[0][2 + 23 + 16:2 + 23 + 24])
    assert (w, h) == (1920, 1080)


def test_image_refusal_is_reported_and_cleaned_up(fake_4k):
    fake_4k.fg_locked = False
    rc, text = run_4k(fake_4k, "--image")
    assert rc == 1
    assert "No Foreground source present" in text
    assert fake_4k.pool == {}


def test_image_refused_on_air(fake_4k):
    fake_4k.controls["Tally Active"] = "on"
    rc, text = run_4k(fake_4k, "--image")
    assert rc == 1 and "Tally Active is on" in text
    assert fake_4k.uploads == []


def test_flush(fake_4k):
    rc, text = run_4k(fake_4k, "--flush")
    assert rc == 0 and "ACK, no refusal" in text and fake_4k.flushes == 1
    fake_4k.fg_locked = False
    rc, text = run_4k(fake_4k, "--flush")
    assert rc == 1 and "No Foreground source present" in text


# ---- --image with a JPG, --sample (2026-11-12 captures) ---------------------

def test_image_takes_a_jpg_file(fake_4k, tmp_path):
    path = tmp_path / "jpg-test-1080.jpg"
    path.write_bytes(fake_4k.jpg(1920, 1080, app_bytes=30000))
    rc, text = run_4k(fake_4k, "--image", path, "--buffer", "Background 2")
    assert rc == 0, text
    assert "uploaded jpg-test-1080.jpg (jpeg 1920x1080" in text
    assert f"sent {path.stat().st_size} of {path.stat().st_size} bytes" in text
    assert fake_4k.pool == {}


def test_sample_reports_how_it_ended_and_puts_controls_back(fake_4k):
    fake_4k.sample_silence = 0.3
    before = dict(fake_4k.controls)
    rc, text = run_4k(fake_4k, "--sample", "wall")
    assert rc == 0, text
    assert "Wall Cursor Position Enable: unit reports True" in text
    assert "Sample Wall: ended after" in text
    assert "by CONTROL DEFAULT (no yes)" in text
    assert "changed Floor Color Red: 0 -> 8333" in text
    assert "controls back OK" in text
    assert {k: v for k, v in fake_4k.controls.items() if before.get(k) != v} == {}


def test_sample_refused_on_air(fake_4k):
    fake_4k.controls["Tally Active"] = "on"
    rc, text = run_4k(fake_4k, "--sample", "floor")
    assert rc == 1 and "Tally Active is on" in text
    assert not any("Sample" in r for r in fake_4k.received)
