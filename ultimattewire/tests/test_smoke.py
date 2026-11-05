# SPDX-License-Identifier: MIT
"""python -m ultimattewire.smoke, against the fake unit: read only unless
--write, and a write is always put back."""

import io
import json
import socket
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
