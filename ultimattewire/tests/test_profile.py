# SPDX-License-Identifier: MIT
"""Archive and restore: zip layout, ordering, failure handling. All I/O is
faked at the module seams (``read_preamble`` / ``binary_request`` /
``upload_one``); the blobs are synthetic."""

import io
import zipfile

import pytest

from ultimattewire import archive_unit_to_bytes, restore_unit_from_bytes
from ultimattewire.profile import archive as archive_mod
from ultimattewire.profile import restore as restore_mod

RESOURCES = {"GPISettings": b"gpi-bytes", "SavedSettings": b"saved!"}
SLOTS = {"preset-a": b"blob-a", "preset-b": b"blob-bb"}


def _fake_binary_request(failing=()):
    def _req(host, opcode, payload, timeout=None):
        name = payload.decode() if isinstance(payload, bytes) else payload
        if name in failing:
            raise IOError(f"simulated failure reading {name}")
        if opcode == archive_mod.OP_READ_SLOT:
            return SLOTS[name]
        if opcode == archive_mod.OP_READ_RESOURCE:
            return RESOURCES[name]
        raise AssertionError(f"unexpected opcode {opcode:#06x}")
    return _req


@pytest.fixture
def archived(monkeypatch, host, sample_prelude):
    monkeypatch.setattr(archive_mod, "read_preamble",
                        lambda h, connect_timeout=None, quiet_timeout=None: sample_prelude)
    monkeypatch.setattr(archive_mod, "binary_request", _fake_binary_request())
    return archive_unit_to_bytes(host)


# ---- archive ---------------------------------------------------------------

def test_archive_zip_member_order_matches_smart_remote(archived):
    zip_bytes, info = archived
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert zf.namelist() == [
            "images/", "GPISettings", "SavedSettings", "quickfiles/",
            "presets/", "presets/preset-a", "presets/preset-b",
            "Keyer_A_config_readable.txt",
        ]
        assert zf.read("presets/preset-a") == b"blob-a"
        assert zf.read("GPISettings") == b"gpi-bytes"
        assert zf.getinfo("presets/").external_attr >> 16 == 0o40755
        assert b"Matte Density: 5000" in zf.read("Keyer_A_config_readable.txt")


def test_archive_info_reports_sizes_and_no_warnings(archived):
    _, info = archived
    assert info["label"] == "Keyer_A"
    assert info["slots"] == ["preset-a", "preset-b"]
    assert info["slot_sizes"] == {"preset-a": 6, "preset-b": 7}
    assert info["gpi_size"] == 9 and info["saved_size"] == 6
    assert info["warnings"] == []


def test_archive_omits_failed_resource_instead_of_placeholder(monkeypatch, host, sample_prelude):
    monkeypatch.setattr(archive_mod, "read_preamble",
                        lambda h, connect_timeout=None, quiet_timeout=None: sample_prelude)
    monkeypatch.setattr(archive_mod, "binary_request",
                        _fake_binary_request(failing={"GPISettings", "preset-b"}))
    zip_bytes, info = archive_unit_to_bytes(host)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
    assert "GPISettings" not in names and "presets/preset-b" not in names
    assert "SavedSettings" in names and "presets/preset-a" in names
    assert info["gpi_size"] == 0
    assert "preset-b" not in info["slot_sizes"]
    assert len(info["warnings"]) == 2


def test_archive_empty_preamble_is_a_hard_failure(monkeypatch, host):
    monkeypatch.setattr(archive_mod, "read_preamble",
                        lambda h, connect_timeout=None, quiet_timeout=None: "")
    with pytest.raises(IOError, match="empty preamble"):
        archive_unit_to_bytes(host)


# ---- restore ---------------------------------------------------------------

def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_restore_pushes_presets_then_gpi_then_saved_settings(monkeypatch, host):
    calls = []
    monkeypatch.setattr(restore_mod, "upload_one",
                        lambda h, opcode, name, payload, connect_timeout=None,
                        read_timeout=None: calls.append((opcode, name, payload)))
    zip_bytes = _zip({
        "SavedSettings": b"S", "presets/": b"", "presets/zeta": b"Z",
        "presets/alpha": b"A", "GPISettings": b"G", "notes.txt": b"ignored",
    })
    ok, info = restore_unit_from_bytes(host, zip_bytes)
    assert ok is True
    assert [(op, name.rsplit("/", 1)[-1], data) for op, name, data in calls] == [
        (restore_mod.OP_WRITE_SLOT, "alpha", b"A"),
        (restore_mod.OP_WRITE_SLOT, "zeta", b"Z"),
        (restore_mod.OP_WRITE_RESOURCE, "GPISettings", b"G"),
        (restore_mod.OP_WRITE_RESOURCE, "SavedSettings", b"S"),
    ]
    assert all("/presets/" in name for op, name, _ in calls if op == restore_mod.OP_WRITE_SLOT)
    assert info == {"presets": [("alpha", True, None), ("zeta", True, None)],
                    "gpi": (True, None), "saved": (True, None), "stopped_at": None}


def test_restore_aborts_at_first_failure(monkeypatch, host):
    calls = []

    def upload_one(h, opcode, name, payload, connect_timeout=None, read_timeout=None):
        calls.append(name)
        if name.endswith("/presets/beta"):
            raise IOError("non-zero ACK from unit (likely error): 010000000000")

    monkeypatch.setattr(restore_mod, "upload_one", upload_one)
    zip_bytes = _zip({"presets/alpha": b"A", "presets/beta": b"B",
                      "GPISettings": b"G", "SavedSettings": b"S"})
    ok, info = restore_unit_from_bytes(host, zip_bytes)
    assert ok is False
    assert info["stopped_at"] == "preset:beta"
    assert info["presets"][1][1] is False and "non-zero ACK" in info["presets"][1][2]
    assert info["gpi"] is None and info["saved"] is None
    assert len(calls) == 2  # nothing after the failure was attempted


def test_restore_skips_absent_resources(monkeypatch, host):
    calls = []
    monkeypatch.setattr(restore_mod, "upload_one",
                        lambda h, opcode, name, payload, connect_timeout=None,
                        read_timeout=None: calls.append(name))
    ok, info = restore_unit_from_bytes(host, _zip({"presets/only": b"O"}))
    assert ok is True
    assert len(calls) == 1
    assert info["gpi"] is None and info["saved"] is None
