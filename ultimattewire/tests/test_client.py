# SPDX-License-Identifier: MIT
"""Ultimatte (the 9998 text protocol client) against the fake unit built
from the protocol document, and against scripted sockets for what an
Ultimatte 12 4K was seen to send differently."""

import json
import logging
import socket
import time

import pytest

from ultimattewire import (ControlValueError, Ultimatte, UltimatteConnectionError,
                           UltimatteError, UltimatteMessageError, UltimatteNak,
                           UltimatteNetworkError, UltimatteTimeout,
                           UnsupportedError)
from ultimattewire import controls as C

IDLE = 1.0


def session(unit, **kw):
    kw.setdefault("idle_timeout", IDLE)
    kw.setdefault("function_idle_timeout", IDLE)
    u = Ultimatte("127.0.0.1", unit.port, **kw)
    u.connect()
    return u


def sent(unit):
    """Blocks the fake received, without the status dump requests."""
    return [b for b in unit.received if "\n" in b]


def settle(unit, n, timeout=2.0):
    end = time.monotonic() + timeout
    while len(unit.received) < n and time.monotonic() < end:
        time.sleep(0.01)


# ---- the status dump ------------------------------------------------------

def test_connect_reads_every_block_of_the_dump(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        s = u.state
        assert s.prelude_complete
        assert s.protocol_version == "2.0" and s.protocol == (2, 0)
        assert (s.identity.model, s.identity.label, s.identity.unique_id) == (
            "Ultimatte 12 8K", "Ultimatte 12 8K", "12345678")
        assert s.family == C.MODEL_8K
        assert s.network["Interface count"] == "1"
        iface = s.interfaces[0]
        assert (iface.name, iface.address, iface.netmask, iface.gateway) == (
            "Cadence GigE Ethernet MAC", "10.0.0.2", "255.255.255.0", "10.0.0.1")
        assert iface.dynamic is False            # the document's "DynamicIP"
        assert (s.version.product_id, s.version.hardware_version,
                s.version.software_version, s.version.software_release) == (
            "BE84", "0100", "09A89B7A", "2.0")
        assert s.device.video_format == "2160p60"
        assert s.device.reference_source == "Foreground"
        assert len(s.device.inputs) == 9
        assert s.device.locked("FG") is True
        assert s.device.locked("REF In") is True        # sent as "REFIn"
        assert s.device.locked("LAYER MATTE") is True
        assert s.device.locked("nope") is None
        assert len(s.video_formats) == 38 and s.video_formats[0] == "auto detect"
        assert len(s.controls) == 289 and len(s.control_defaults) == 289
        assert s.controls["Matte Density"] == 5000
        assert s.controls["Matte Enable"] is False
        assert s.controls["Backing Color"] == "green"
        assert s.controls["Tally Active"] is False and s.tally is False
        assert s.controls["Factory Defaults"] is False
        assert s.current_file.name == "Ultimatte Defaults"
        assert s.current_file.status == "" and s.current_file.modified is None
        assert s.file_list == ["Studio A", "Studio B"]
        assert sorted(s.gpi_lists) == [1, 2, 3, 4, 5]
        assert s.gpi_lists[1].events == ["Studio A", "Studio B"]
        assert s.gpi_lists[1].index == 0
        assert s.image_list == ["Blue Sky", "Newsroom"]
        fb = s.frame_buffer
        assert fb.count == 46
        assert sorted(fb.buffers) == ["BG 1", "BG 2", "GM", "HM", "LY 1", "LY 2"]
        assert fb.buffers["BG 1"].enable is False and fb.buffers["BG 1"].index == 0
        assert fb.mix == {"BG": 0, "LY": 0} and fb.duration == {"BG": 0, "LY": 0}
        assert fb.images == {1: "Blue Sky", 2: "Newsroom"}
        assert fb.image_number("newsroom") == 2
        assert s.camera_id is None
        assert s.other == {}
    assert not u.connected


@pytest.mark.parametrize("model,inputs,controls,gpis,camera", [
    ("8K", 9, 289, 5, None), ("4K", 9, 289, 5, None),
    ("HD", 6, 284, 5, None), ("HD Mini", 2, 260, 0, 1),
])
def test_each_model_sends_its_subset(fake_unit, model, inputs, controls, gpis, camera):
    with session(fake_unit(model)) as u:
        assert len(u.state.device.inputs) == inputs
        assert len(u.state.controls) == controls
        assert len(u.state.gpi_lists) == gpis
        assert u.state.camera_id == camera


def test_protocol_1_2_has_no_output_range(fake_unit):
    with session(fake_unit("8K", protocol="1.2")) as u:
        assert "Output Range" not in u.state.controls
        with pytest.raises(UnsupportedError):
            u.set_control("Output Range", "full")


def test_state_to_dict_is_json(fake_unit):
    with session(fake_unit("HD Mini")) as u:
        d = json.loads(json.dumps(u.state.to_dict()))
    assert d["family"] == C.MODEL_HD_MINI
    assert d["frame_buffer"]["images"] == {"1": "Blue Sky", "2": "Newsroom"}
    assert d["interfaces"]["0"]["address"] == "10.0.0.2"
    assert d["tally"] is False and d["camera_id"] == 1


# ---- what a 12 4K sends, scripted -------------------------------------------

class ScriptedSocket:
    def __init__(self, chunks, delay=0.0):
        self.chunks = list(chunks)
        self.delay = delay
        self.sent = b""
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if self.delay:
            time.sleep(self.delay)
        if not self.chunks:
            if self.timeout == 0.0:
                raise BlockingIOError()
            raise socket.timeout()
        head = self.chunks.pop(0)
        if isinstance(head, Exception):
            raise head
        return head

    def shutdown(self, how):
        pass

    def close(self):
        pass


def scripted(chunks, **kw):
    sock = ScriptedSocket(chunks, **{k: kw.pop(k) for k in ("delay",) if k in kw})
    u = Ultimatte("192.0.2.21", socket_factory=lambda addr, timeout=None: sock,
                  idle_timeout=kw.pop("idle_timeout", 0.2), **kw)
    return u, sock


# Sections as an Ultimatte 12 4K with software 2.3 sent them (2026-10-27,
# see test_preamble.py), with CR LF line ends, a block the document doesn't
# have, a line it doesn't have, and END PRELUDE with no blank line after.
PRELUDE_12_4K = (
    "PROTOCOL PREAMBLE:\r\nVersion: 2.3\r\n\r\n"
    "IDENTITY:\r\nModel: Ultimatte 12 4K\r\nLabel: Studio Keyer\r\n"
    "Unique ID: 00000000000000000000000000000000\r\n\r\n"
    "VERSION:\r\nProduct ID: BE8F\r\nHardware Version: 0100\r\n"
    "Software Version: 2AE2D1CA\r\nSoftware Release: 2.3\r\n\r\n"
    "NETWORK INTERFACE 0:\r\nName: BMD 10G Ethernet MAC\r\nDynamic IP: true\r\n"
    "Current Addresses: 192.0.2.98/255.255.252.0\r\n\r\n"
    "CONTROL:\r\nMonitor Out: fg\r\nActive Unit: off\r\nVideo Format: auto detect\r\n"
    "Matte Density: 2600\r\n\r\n"
    "SOMETHING NEW:\r\nFoo: 1\r\n\r\n"
    "CURRENT FILE:\r\nName: 3\r\nStatus: Modified\r\n\r\n"
    "FILE LIST:\r\n1\r\n2\r\n\r\n"
    "END PRELUDE:\r\n"
)


def test_the_12_4k_forms_are_read():
    u, _ = scripted([PRELUDE_12_4K[:100].encode(), PRELUDE_12_4K[100:].encode()])
    s = u.connect()
    assert s.prelude_complete and s.protocol == (2, 3)
    assert s.family == C.MODEL_4K
    assert s.current_file.name == "3" and s.current_file.modified is True
    assert s.file_list == ["1", "2"]
    assert s.controls["Monitor Out"] == "fg"
    assert s.controls["Active Unit"] == "off"       # kept, not in the document
    assert s.controls["Matte Density"] == 2600
    assert s.interfaces[0].dynamic is True
    assert s.other == {"SOMETHING NEW:": ["Foo: 1"]}


def test_a_control_the_unit_has_but_the_document_lacks_can_be_set():
    u, sock = scripted([PRELUDE_12_4K.encode(), b"ACK\n\nCONTROL:\nActive Unit: on\n\n"])
    u.connect()
    assert u.set_control("active unit", True) == "on"
    assert sock.sent == b"CONTROL:\nActive Unit: on\n\n"


def test_a_control_nobody_has_is_refused_unless_unvalidated():
    u, sock = scripted([PRELUDE_12_4K.encode(), b"NAK\n\n"])
    u.connect()
    with pytest.raises(ControlValueError):
        u.set_control("Made Up", 1)
    assert sock.sent == b""
    with pytest.raises(UltimatteNak):
        u.set_control("Made Up", 1, validate=False)
    assert sock.sent == b"CONTROL:\nMade Up: 1\n\n"


def test_a_slow_dump_that_keeps_coming_is_not_timed_out():
    # 12 chunks 0.05 s apart: 0.6 s in all, three times idle_timeout.
    data = PRELUDE_12_4K.encode()
    chunks = [data[i:i + 40] for i in range(0, len(data), 40)]
    assert len(chunks) >= 12
    u, _ = scripted(chunks, delay=0.05)
    t0 = time.monotonic()
    assert u.connect().prelude_complete
    assert time.monotonic() - t0 > u.idle_timeout


def test_a_stalled_dump_times_out_on_silence():
    u, _ = scripted([PRELUDE_12_4K[:80].encode()])
    with pytest.raises(UltimatteTimeout, match="stalled"):
        u.connect()
    assert not u.connected


def test_silence_from_the_start_says_so():
    u, _ = scripted([])
    with pytest.raises(UltimatteTimeout, match="not an Ultimatte"):
        u.connect()


def test_a_close_mid_dump_is_named():
    u, _ = scripted([PRELUDE_12_4K[:80].encode(), b""])
    with pytest.raises(UltimatteConnectionError, match="middle of its status dump"):
        u.connect()


def test_a_close_without_a_word_is_named(fake_unit):
    unit = fake_unit("8K", close_on_connect=True)
    with pytest.raises(UltimatteConnectionError, match="without a word"):
        Ultimatte("127.0.0.1", unit.port, idle_timeout=IDLE).connect()


def test_a_refused_connection_is_named(monkeypatch):
    import ultimattewire.client as client
    monkeypatch.setattr(client, "CONNECT_RETRY_DELAY_S", 0)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(UltimatteConnectionError, match="refused"):
        Ultimatte("127.0.0.1", port).connect()


def test_connect_retries_once(monkeypatch):
    import ultimattewire.client as client
    monkeypatch.setattr(client, "CONNECT_RETRY_DELAY_S", 0)
    sock = ScriptedSocket([PRELUDE_12_4K.encode()])
    calls = []

    def factory(addr, timeout=None):
        calls.append(addr)
        if len(calls) == 1:
            raise socket.timeout()
        return sock
    u = Ultimatte("192.0.2.21", socket_factory=factory, idle_timeout=0.2)
    assert u.connect().prelude_complete and len(calls) == 2


def test_errors_share_one_base():
    for e in (UltimatteConnectionError, UltimatteTimeout, UltimatteNak,
              UltimatteMessageError, UltimatteNetworkError):
        assert issubclass(e, UltimatteError)


def test_commands_need_a_session():
    with pytest.raises(UltimatteConnectionError, match="not connected"):
        Ultimatte("192.0.2.21").set_control("Matte Density", 1)


# ---- controls ---------------------------------------------------------------

def test_set_control_sends_the_documented_block_and_returns_the_echo(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        assert u.set_control("Matte Density", 100) == 100
        assert u.state.controls["Matte Density"] == 100
        assert unit.controls["Matte Density"] == "100"
        assert sent(unit) == ["CONTROL:\nMatte Density: 100"]


def test_values_go_out_lower_case_and_come_back_typed(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        assert u.set_control("Backing Color", "Blue") == "blue"
        assert u.set_control("Matte Enable", True) is True
        assert u.set_control("matte enable", "off") is False
        assert u.set_control("Monitor Out", "Processed GM") == "processed gm"
        assert u.set_control("Video Format", "1080p25") == "1080p25"
        assert sent(unit) == [
            "CONTROL:\nBacking Color: blue", "CONTROL:\nMatte Enable: on",
            "CONTROL:\nMatte Enable: off", "CONTROL:\nMonitor Out: processed gm",
            "CONTROL:\nVideo Format: 1080p25"]


def test_bad_values_never_reach_the_wire(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        for name, value in [("Matte Density", 10001), ("Transition Rate", 0),
                            ("Backing Color", "purple"), ("Matte Enable", 2),
                            ("Window Position Top", 2161), ("Filter Mode", "max")]:
            with pytest.raises(ControlValueError):
                u.set_control(name, value)
        with pytest.raises(ControlValueError, match="read only"):
            u.set_control("Tally Active", True)
        with pytest.raises(ControlValueError, match="line break"):
            u.set_control("Made Up", "a\nb", validate=False)
        assert u.set_control("Window Position Top", 2160) == 2160   # 2160p60
    assert sent(unit) == ["CONTROL:\nWindow Position Top: 2160"]


def test_unvalidated_values_go_out_and_a_nak_is_raised(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        with pytest.raises(UltimatteNak, match="Matte Density: 99999"):
            u.set_control("Matte Density", 99999, validate=False)
        assert u.state.controls["Matte Density"] == 5000


@pytest.mark.parametrize("model,name,value", [
    ("HD Mini", "GP Out Delay", 5), ("HD Mini", "GM Freeze", True),
    ("HD Mini", "Color Space", "Rec.2020"), ("HD Mini", "GP Out", True),
    ("HD", "Talent Mirror", True), ("HD", "Monitor To Talent", True),
    ("HD", "Talent Highlight Level", 1),
])
def test_model_footnotes_refuse_before_sending(fake_unit, model, name, value):
    unit = fake_unit(model)
    with session(unit) as u:
        with pytest.raises(UnsupportedError):
            u.set_control(name, value)
    assert sent(unit) == []


def test_video_format_is_checked_against_the_units_own_list(fake_unit):
    unit = fake_unit("HD Mini")
    with session(unit) as u:
        with pytest.raises(ControlValueError):
            u.set_control("Video Format", "1080PsF25")
        with pytest.raises(ControlValueError):
            u.set_control("Video Format", "4320p25")
        assert u.set_control("Video Format", "1080p50") == "1080p50"


def test_set_controls_is_one_block(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        got = u.set_controls({"Red Density": 10, "Green Density": 20,
                              "Shadow Level": 30})
        assert got == {"Red Density": 10, "Green Density": 20, "Shadow Level": 30}
        with pytest.raises(ControlValueError):
            u.set_controls({"Red Density": 1, "Green Density": -1})
        with pytest.raises(ValueError):
            u.set_controls({})
    assert sent(unit) == ["CONTROL:\nRed Density: 10\nGreen Density: 20\nShadow Level: 30"]


def test_offset_is_relative(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        assert u.offset_control("Matte Density", 10) == 5010
        assert u.offset_control("Matte Density", -20) == 4990
        with pytest.raises(ControlValueError):
            u.offset_control("Matte Enable", 1)
        with pytest.raises(ControlValueError):
            u.offset_control("Made Up", 1)
    assert sent(unit) == ["CONTROL:\nOffset Matte Density: 10",
                          "CONTROL:\nOffset Matte Density: -20"]


def test_set_without_waiting(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        assert u.set_control("Matte Density", 7, wait=False) == 7
        assert u.offset_control("Matte Density", 1, wait=False) is None
        u.wait_for(lambda s: s.controls["Matte Density"] == 8)


def test_functions_wait_for_yes(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        u.set_control("Matte Density", 1)
        u.run_function("Factory Defaults")
        assert u.state.controls["Matte Density"] == 5000
        assert u.state.controls["Factory Defaults"] is True
        u.run_function("auto screen sample")
        with pytest.raises(ControlValueError):
            u.run_function("Matte Density")
    assert sent(unit)[-2:] == ["CONTROL:\nFactory Defaults: yes",
                               "CONTROL:\nAuto Screen Sample: yes"]


def test_a_function_that_never_finishes_times_out(fake_unit):
    unit = fake_unit("8K", function_silent=True)
    with session(unit) as u:
        t0 = time.monotonic()
        with pytest.raises(UltimatteTimeout, match="update after the ACK"):
            u.run_function("Matte Reset", idle_timeout=0.3)
        assert time.monotonic() - t0 < 2


def test_a_unit_that_never_answers_times_out(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        unit.mute = True
        with pytest.raises(UltimatteTimeout, match="no ACK.*connect again"):
            u.set_control("Matte Density", 1)
        assert not u.connected


# ---- pushes and subscriptions -------------------------------------------------

def test_panel_changes_reach_subscribers(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        seen, controls_only = [], []
        off = u.subscribe(seen.append)
        u.subscribe(controls_only.append, headers=["CONTROL"])
        unit.panel("CONTROL", {"Matte Density": "1234", "Tally Active": "on"})
        unit.panel("FRAME BUFFER", {"BG 1 Frame Buffer Enable": "on"})
        updates = u.poll(1.0)
        while len(updates) < 2:
            updates += u.poll(1.0)
        assert [x.header for x in updates] == ["CONTROL:", "FRAME BUFFER:"]
        assert updates[0].changes == {"Matte Density": 1234, "Tally Active": True}
        assert u.state.tally is True
        assert u.state.frame_buffer.buffers["BG 1"].enable is True
        assert [x.header for x in seen] == ["CONTROL:", "FRAME BUFFER:"]
        assert [x.header for x in controls_only] == ["CONTROL:"]
        off()
        unit.panel("CONTROL", {"Matte Density": "1"})
        u.wait_for(lambda s: s.controls["Matte Density"] == 1)
        assert len(seen) == 2 and len(controls_only) == 2
        assert u.poll(0) == []


def test_another_clients_change_is_pushed(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as a, session(unit) as b:
        a.set_control("Black Gloss", 77)
        b.wait_for(lambda s: s.controls["Black Gloss"] == 77)


def test_pushes_during_a_command_are_applied(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as a, session(unit) as b:
        got = []
        a.subscribe(got.append)
        b.set_control("Cool", 5)
        time.sleep(0.2)       # b's push is now waiting in a's socket
        a.set_control("Skin Tone", 6)
        assert a.state.controls["Cool"] == 5
        assert [x.changes for x in got] == [{"Cool": 5}, {"Skin Tone": 6}]


def test_a_failing_subscriber_is_logged_not_raised(fake_unit, caplog):
    unit = fake_unit("8K")
    with session(unit) as u:
        def boom(update):
            raise RuntimeError("subscriber bug")
        u.subscribe(boom)
        with caplog.at_level(logging.ERROR, logger="ultimattewire.client"):
            assert u.set_control("Cool", 1) == 1
        assert "subscriber failed" in caplog.text


def test_wait_for_times_out_on_silence(fake_unit):
    with session(fake_unit("8K")) as u:
        with pytest.raises(UltimatteTimeout):
            u.wait_for(lambda s: False, idle_timeout=0.2)


def test_a_lost_session_is_named(fake_unit):
    unit = fake_unit("8K")
    u = session(unit)
    unit.stop()
    with pytest.raises(UltimatteConnectionError):
        u.poll(1.0)
    assert not u.connected


def test_unsolicited_message_is_kept(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        unit.broadcast("MESSAGE:\nWarning: Something\n\n")
        u.wait_for(lambda s: s.messages)
        assert u.state.messages == [{"Warning": "Something"}]


# ---- status dump requests -----------------------------------------------------

@pytest.mark.parametrize("header,n", [
    ("PROTOCOL PREAMBLE", 1), ("IDENTITY", 3), ("NETWORK", 4),
    ("NETWORK INTERFACE 0", 8), ("VERSION", 4), ("DEVICE", 11),
    ("VIDEO FORMATS", 38), ("CONTROL", 289), ("CONTROL DEFAULT", 289),
    ("CURRENT FILE", 1), ("FILE LIST", 2), ("GPI LIST", 4),
    ("IMAGE LIST", 2), ("FRAME BUFFER", 19),
])
def test_refresh_every_block(fake_unit, header, n):
    unit = fake_unit("8K")
    with session(unit) as u:
        lines = u.refresh(header.lower())
        assert lines[0] == header + ":"
        assert len([ln for ln in lines[1:] if ln]) == n
    # the four Software Control asks for go as it spells them (captured)
    sent = {"FRAME BUFFER": "Frame Buffer", "IMAGE LIST": "Image List",
            "FILE LIST": "File List", "CURRENT FILE": "Current File"}
    assert unit.received == [sent.get(header, header) + ":"]


def test_refresh_replaces_state(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        unit.files.append("Quiet")         # changed without a push
        u.refresh("FILE LIST")
        assert u.state.file_list == ["Studio A", "Studio B", "Quiet"]


def test_refresh_of_a_block_the_model_lacks_is_a_nak(fake_unit):
    with session(fake_unit("HD Mini")) as u:
        with pytest.raises(UltimatteNak):
            u.refresh("GPI LIST")
        assert u.refresh("CAMERACONTROL")[1] == "Camera Id: 1"


def test_an_unknown_block_is_a_nak(fake_unit):
    with session(fake_unit("8K")) as u:
        with pytest.raises(UltimatteNak):
            u.send_block("NOT A BLOCK", ["X: 1"])


def test_send_block_raw(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        lines = u.send_block("control", ["Matte Density: 9"], expect="CONTROL")
        assert lines == ["CONTROL:", "Matte Density: 9"]
        assert u.send_block("CONTROL", ["Cool: 3"]) is None
        with pytest.raises(ControlValueError):
            u.send_block("CONTROL", ["a\nb"])


def test_network_interface_push_merges_into_state(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        unit.broadcast("NETWORK INTERFACE 0:\nStatic Gateway: 10.0.0.254\n\n")
        u.wait_for(lambda s: s.interfaces[0].static_gateway == "10.0.0.254")
        assert u.state.interfaces[0].address == "10.0.0.2"
        assert u.state.interfaces[0].name == "Cadence GigE Ethernet MAC"


# ---- files ----------------------------------------------------------------

def test_files(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        assert u.load_file("Studio B").name == "Studio B"
        assert u.save_file("Evening") == ["Studio A", "Studio B", "Evening"]
        assert u.rename_file("Evening", "Late") == ["Studio A", "Studio B", "Late"]
        assert u.delete_file("Studio A") == ["Studio B", "Late"]
    assert sent(unit) == ["FILE:\nLoad: Studio B", "FILE:\nSave: Evening",
                          "FILE:\nRename: Evening\nTo: Late",
                          "FILE:\nDelete: Studio A"]


def test_a_file_message_raises(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        with pytest.raises(UltimatteMessageError) as e:
            u.load_file("Nope")
        assert e.value.message == {"Error": "File not found"}
        assert u.state.messages[-1] == {"Error": "File not found"}
        assert u.state.current_file.name == "Ultimatte Defaults"
        with pytest.raises(ControlValueError):
            u.save_file("two\nlines")


def test_load_by_another_client_is_pushed(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as a, session(unit) as b:
        a.load_file("Studio A")
        b.wait_for(lambda s: s.current_file.name == "Studio A")


# ---- GPI ------------------------------------------------------------------

def test_gpi_events(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        assert u.gpi_insert(2, "Studio A").events == ["Studio A"]
        assert u.gpi_insert(2, "Studio B", at=0).events == ["Studio B", "Studio A"]
        assert u.gpi_set_index(2, 1).index == 1
        assert u.gpi_remove(2, 1).events == ["Studio A"]
        assert u.gpi_remove(2, 0).events == []
    assert sent(unit) == [
        "GPI:\nID: 2\nInsert: Studio A\nAt: -1", "GPI:\nID: 2\nInsert: Studio B\nAt: 0",
        "GPI:\nID: 2\nIndex: 1", "GPI:\nID: 2\nRemove: 1", "GPI:\nID: 2\nRemove: 0"]


def test_gpi_event_limit_message(fake_unit, fake_module):
    unit = fake_unit("8K")
    with session(unit) as u:
        for _ in range(fake_module.GPI_EVENT_LIMIT - 2):
            u.gpi_insert(1, "Studio A")
        with pytest.raises(UltimatteMessageError, match="Event limit exceeded"):
            u.gpi_insert(1, "Studio A")


def test_gpi_refusals(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        for call in (lambda: u.gpi_insert(0, "x"), lambda: u.gpi_insert(1, "x", at=-2),
                     lambda: u.gpi_remove(1, -1), lambda: u.gpi_set_index(1, -1)):
            with pytest.raises(ControlValueError):
                call()
        with pytest.raises(UltimatteNak):
            u.gpi_insert(9, "Studio A")
    with session(fake_unit("HD Mini")) as u:
        with pytest.raises(UnsupportedError):
            u.gpi_insert(1, "Studio A")


# ---- frame buffers ------------------------------------------------------------

def test_assign_image_to_a_frame_buffer(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        buf = u.assign_image("bg 1", "Newsroom")
        assert (buf.name, buf.index, buf.enable) == ("BG 1", 2, True)
        assert u.set_frame_buffer("BG 1", enable=False).enable is False
        assert u.set_frame_buffer_mix("bg", 5000) == 5000
        assert u.set_transition_duration("LY", 1500) == 1500
        assert u.state.frame_buffer.duration == {"BG": 0, "LY": 1500}
    assert sent(unit) == [
        "FRAME BUFFER:\nBG 1 Frame Buffer Index: 2\nBG 1 Frame Buffer Enable: on",
        "FRAME BUFFER:\nBG 1 Frame Buffer Enable: off",
        "FRAME BUFFER:\nBG Frame Buffer Mix: 5000",
        "FRAME BUFFER:\nLY Transition Duration: 1500"]


def test_frame_buffer_refusals(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        for call in (lambda: u.set_frame_buffer("BG 3", enable=True),
                     lambda: u.set_frame_buffer("BG 1", index=47),
                     lambda: u.set_frame_buffer("BG 1", index=-1),
                     lambda: u.set_frame_buffer_mix("GM", 1),
                     lambda: u.set_transition_duration("BG", -1),
                     lambda: u.assign_image("BG 1", "Not There")):
            with pytest.raises(ControlValueError):
                call()
        with pytest.raises(ValueError):
            u.set_frame_buffer("BG 1")
    assert sent(unit) == []


# ---- camera control -------------------------------------------------------------

def test_camera_id_on_the_hd_mini(fake_unit):
    unit = fake_unit("HD Mini")
    with session(unit) as u:
        assert u.set_camera_id(4) == 4
        with pytest.raises(ControlValueError):
            u.set_camera_id(-1)
    assert sent(unit) == ["CAMERACONTROL:\nCamera Id: 4"]


def test_camera_control_elsewhere_is_refused(fake_unit):
    unit = fake_unit("8K")
    with session(unit) as u:
        with pytest.raises(UnsupportedError):
            u.set_camera_id(4)
    assert sent(unit) == []


# ---- what a 12 4K did on 2026-11-12 (Software Control captures) -------------
#
# Replayed byte for byte from "2026-11-12 Ultimatte same-value write, sample,
# cursor, monitor out (81.42) - 9998 only.pcapng", then the same behaviour
# against the fake 12 4K with its timing scaled down.

PRELUDE_81_42 = (
    "PROTOCOL PREAMBLE:\nVersion: 2.3\n\n"
    "IDENTITY:\nModel: Ultimatte 12 4K\nLabel: Ultimatte Editor\n"
    "Unique ID: DF92D2DAAA8244C4ADA6D318E183B58D\n\n"
    "DEVICE:\nVideo Format: 1080p25\nReference Source: foreground\n"
    "FG In: Locked\nBG In: Not Locked\n\n"
    "CONTROL:\nMatte Density: 2500\nCursor X: 192\nCursor Y: 108\n"
    "Cursor 2 X: 1728\nCursor 2 Y: 972\nWall Cursor Position Enable: off\n"
    "Floor Cursor Position Enable: off\nMonitor Out: program\n\n"
    "CURRENT FILE:\nName: Ultimatte Defaults\nStatus: Clean\n\n"
    "END PRELUDE:\n\n"
)


def test_capture_same_value_write_is_echoed():
    # 9.904: Matte Density: 2500 while at 2500
    u, sock = scripted([PRELUDE_81_42.encode(), b"ACK\n\n",
                        b"CONTROL:\nMatte Density: 2500\n\n",
                        b"CURRENT FILE:\nName: Ultimatte Defaults\nStatus: Modified\n\n"])
    u.connect()
    assert u.set_control("Matte Density", 2500) == 2500
    assert sock.sent.endswith(b"CONTROL:\nMatte Density: 2500\n\n")
    u.poll(0)
    assert u.state.current_file.modified is True


def test_capture_cursor_enable_echo_after_colour_pushes():
    # 50.935: the echo came 550 ms after the ACK, after a colour push
    u, sock = scripted([PRELUDE_81_42.encode(), b"ACK\n\n",
                        b"CONTROL:\nWall Color Blue: 2956\n\n",
                        b"CONTROL:\nWall Color Green: 3744\n\n",
                        b"CONTROL:\nWall Color Red: 5490\n\n",
                        b"CONTROL:\nWall Cursor Position Enable: on\n\n",
                        b"CONTROL:\nWall Color Blue: 2956\n\n"])
    u.connect()
    assert u.set_control("Wall Cursor Position Enable", True) is True
    assert u.state.controls["Wall Color Red"] == 5490


def test_capture_cursor_move_with_the_cursor_off_raises():
    # 66.326: Offset Cursor X after Sample Wall had turned the cursor off
    u, sock = scripted([PRELUDE_81_42.encode(), b"ACK\n\n",
                        b"MESSAGE:\nControl Locked - Enable 'Cursor' function\n\n"])
    u.connect()
    with pytest.raises(UltimatteMessageError, match="Control Locked"):
        u.offset_control("Cursor X", -15)
    assert u.state.messages[-1] == {"": "Control Locked - Enable 'Cursor' function"}


def test_capture_sample_wall_completes_on_control_default():
    # 62.382: ACK, 4 s of nothing, cursor off, colours, CONTROL DEFAULT; no yes
    u, sock = scripted([PRELUDE_81_42.encode(), b"ACK\n\n",
                        b"CONTROL:\nWall Cursor Position Enable: off\n\n",
                        b"CONTROL:\nFloor Color Blue: 5958\n\n",
                        b"CONTROL:\nFloor Color Green: 6757\n\n",
                        b"CONTROL:\nFloor Color Red: 8333\n\n",
                        b"CONTROL:\nDirect Light Blue: 10000\n\n",
                        b"CONTROL:\nDirect Light Green: 3253\n\n",
                        b"CONTROL:\nDirect Light Red: 10000\n\n",
                        b"CONTROL DEFAULT:\nMatte Density: 2500\n"
                        b"Direct Light Green: 3253\n\n"])
    u.connect()
    u.run_function("Sample Wall")
    assert sock.sent.endswith(b"CONTROL:\nSample Wall: yes\n\n")
    c = u.state.controls
    assert c["Wall Cursor Position Enable"] is False
    assert (c["Floor Color Red"], c["Floor Color Green"], c["Floor Color Blue"]) \
        == (8333, 6757, 5958)
    assert c["Direct Light Green"] == 3253


def test_capture_monitor_out_full_lower_case_names():
    # 80.623: Software Control sends and the unit echoes "combined matte"
    u, sock = scripted([PRELUDE_81_42.encode(), b"ACK\n\n",
                        b"CONTROL:\nMonitor Out: combined matte\n\n"])
    u.connect()
    assert u.set_control("Monitor Out", "Combined Matte") == "combined matte"
    assert sock.sent.endswith(b"CONTROL:\nMonitor Out: combined matte\n\n")
    with pytest.raises(ControlValueError):
        u.set_control("Monitor Out", "matte")        # no such choice


def session_4k(unit, **kw):
    kw.setdefault("idle_timeout", 0.3)
    return Ultimatte("127.0.0.1", unit.port, **kw)


def test_4k_sample_wall_waits_out_the_silence(fake_4k):
    fake_4k.sample_silence = 0.5             # 4 s on the unit
    with session_4k(fake_4k, function_idle_timeout=1.5) as u:
        u.set_control("Wall Cursor Position Enable", True)
        t0 = time.monotonic()
        u.run_function("Sample Wall")
        assert time.monotonic() - t0 >= 0.45
        assert u.state.controls["Wall Cursor Position Enable"] is False
        assert u.state.controls["Floor Color Red"] == 8333
        assert u.state.control_defaults["Direct Light Green"] == 3253


def test_4k_sample_floor_without_yes(fake_4k):
    fake_4k.sample_silence = 0.2
    with session_4k(fake_4k, function_idle_timeout=1.0) as u:
        u.set_control("Floor Cursor Position Enable", True)
        seen = []
        u.subscribe(lambda upd: seen.append(upd.changes), ["CONTROL"])
        u.run_function("Sample Floor")
        assert u.state.controls["Floor Cursor Position Enable"] is False
        assert not any("Sample Floor" in c for c in seen)      # never "yes"


def test_4k_sample_silence_is_the_function_timeout_s_to_cover(fake_4k):
    fake_4k.sample_silence = 0.8
    with session_4k(fake_4k, function_idle_timeout=0.3) as u:
        with pytest.raises(UltimatteTimeout):
            u.run_function("Sample Wall")


def test_default_function_timeout_covers_the_captured_silence():
    from ultimattewire.client import DEFAULT_FUNCTION_IDLE_TIMEOUT
    assert DEFAULT_FUNCTION_IDLE_TIMEOUT >= 2 * 4.0     # 4.0 s seen twice


def test_4k_cursor_moves_need_their_cursor(fake_4k):
    with session_4k(fake_4k) as u:
        with pytest.raises(UltimatteMessageError, match="Enable 'Cursor'"):
            u.offset_control("Cursor X", 15)
        assert u.connected                   # a refusal, not a broken session
        u.set_control("Wall Cursor Position Enable", True)
        assert u.offset_control("Cursor X", 15) == 207
        with pytest.raises(UltimatteMessageError):
            u.offset_control("Cursor 2 Y", -15)      # floor cursor still off
        u.set_control("Floor Cursor Position Enable", True)
        assert u.offset_control("Cursor 2 Y", -15) == 957


def test_4k_same_value_write_is_echoed(fake_4k):
    with session_4k(fake_4k) as u:
        assert u.get_control("Matte Density") == 2500
        assert u.set_control("Matte Density", 2500) == 2500
        u.wait_for(lambda s: s.current_file.modified)
        assert fake_4k.received[-1] == "CONTROL:\nMatte Density: 2500"


def test_4k_colour_stream_does_not_hold_a_wait_open(fake_4k):
    fake_4k.stream_interval = 0.01
    fake_4k.silent_controls = {"Matte Density"}
    with session_4k(fake_4k, idle_timeout=0.3) as u:
        assert u.set_control("Wall Cursor Position Enable", True) is True
        t0 = time.monotonic()
        with pytest.raises(UltimatteTimeout, match="cursor colour stream"):
            u.set_control("Matte Density", 3000)
        assert time.monotonic() - t0 < 1.5
    with session_4k(fake_4k, idle_timeout=0.3) as u:
        t0 = time.monotonic()
        with pytest.raises(UltimatteTimeout):
            u.wait_for(lambda s: False)
        assert time.monotonic() - t0 < 1.5
        assert u.state.controls["Wall Color Red"] == 5490   # still applied


def test_4k_writes_go_through_the_colour_stream(fake_4k):
    fake_4k.stream_interval = 0.005
    with session_4k(fake_4k) as u:
        u.set_control("Wall Cursor Position Enable", True)
        u.set_control("Floor Cursor Position Enable", True)
        for v in (2600, 2700, 2500):
            assert u.set_control("Matte Density", v) == v
        assert u.offset_control("Cursor 2 X", -15) == 1713


def test_4k_refresh_spells_the_header_as_software_control(fake_4k):
    with session_4k(fake_4k) as u:
        for header in ("FRAME BUFFER", "IMAGE LIST", "FILE LIST", "CURRENT FILE"):
            assert u.refresh(header)[0] == header + ":"
    assert fake_4k.received == ["Frame Buffer:", "Image List:", "File List:",
                                "Current File:"]
