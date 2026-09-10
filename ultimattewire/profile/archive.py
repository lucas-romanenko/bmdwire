# SPDX-License-Identifier: MIT
"""
Ultimatte archive — produces a zip identical to Smart Remote's
"Archive All".

This module is a port of the standalone archive script that was
reverse-engineered from packet captures against a real
Ultimatte 12 4K. Protocol details (opcodes, frame format, zip layout,
byte order, ranges, sequence) MUST NOT be changed without re-validating
against hardware — the unit's parser is strict about all of them.

Zip structure (matches Smart Remote so the zip restores from the app):
    <label>.zip/
        images/                    (empty unless unit has images)
        GPISettings                (raw bytes, no length prefix)
        SavedSettings              (raw bytes, no length prefix)
        quickfiles/                (empty unless unit has quickfiles)
        presets/                   (explicit dir entry — Smart Remote requires)
        presets/<slot_name>        (one file per saved slot from FILE LIST)
        <label>_config_readable.txt (bonus: human-readable annotated state
                                    dump. Ignored by Smart Remote on restore.)
"""

import io
import zipfile
from datetime import datetime

from ultimattewire._preamble import (
    PREAMBLE_QUIET_TIMEOUT,
    extract_label,
    parse_file_list,
    read_preamble,
)
from ultimattewire._protocol import CONNECT_TIMEOUT, binary_request
from ultimattewire.profile._ranges import annotate_preamble


OP_READ_SLOT = 0x0000
OP_READ_RESOURCE = 0x0003


def _get_slot_blob(host, slot_name, timeout=CONNECT_TIMEOUT):
    return binary_request(host, OP_READ_SLOT, slot_name, timeout=timeout)


def _get_resource(host, name, timeout=CONNECT_TIMEOUT):
    return binary_request(host, OP_READ_RESOURCE, name, timeout=timeout)


def archive_unit_to_bytes(host, request_timeout=CONNECT_TIMEOUT):
    """Pull a full archive of one Ultimatte and return (zip_bytes, info).

    info is a dict:
        {
            "label": "<unit protocol label, sanitized>",
            "slots": ["slot1", "slot2", ...],     # slots present in FILE LIST
            "slot_sizes": {"slot1": int, ...},    # per-slot blob size; absent on per-slot failure
            "gpi_size": int,                      # 0 if read failed
            "saved_size": int,                    # 0 if read failed
            "warnings": ["...", ...],             # per-resource soft failures
        }

    Raises Exception on hard failures (cannot reach preamble at all).
    """
    warnings = []

    preamble = read_preamble(host, connect_timeout=request_timeout,
                             quiet_timeout=PREAMBLE_QUIET_TIMEOUT)
    if not preamble:
        raise IOError("empty preamble (unit unreachable or not responding)")

    label = extract_label(preamble)
    slots = parse_file_list(preamble)

    slot_blobs = {}
    slot_sizes = {}
    for slot in slots:
        try:
            blob = _get_slot_blob(host, slot, timeout=request_timeout)
            slot_blobs[slot] = blob
            slot_sizes[slot] = len(blob)
        except Exception as e:
            warnings.append(f"preset {slot!r}: {e}")

    # If a resource read fails, omit it from the zip rather than embedding
    # a placeholder — the restore side handles missing resources by
    # skipping them, but it would faithfully push a placeholder back to
    # the unit and silently corrupt the live config.
    gpi_settings = None
    try:
        gpi_settings = _get_resource(host, "GPISettings", timeout=request_timeout)
    except Exception as e:
        warnings.append(f"GPISettings: {e}")

    saved_settings = None
    try:
        saved_settings = _get_resource(host, "SavedSettings", timeout=request_timeout)
    except Exception as e:
        warnings.append(f"SavedSettings: {e}")

    readable_text = annotate_preamble(preamble)
    now = datetime.now().timetuple()[:6]

    def dir_entry(name):
        zi = zipfile.ZipInfo(name, date_time=now)
        zi.external_attr = 0o40755 << 16
        return zi

    # Build the zip in Smart Remote's exact format. Order and the explicit
    # "presets/" parent directory entry matter — Software Control's parser
    # rejects archives that don't match this layout.
    # Smart Remote order: images/, GPISettings, SavedSettings, quickfiles/,
    #                     presets/, presets/<slot>, presets/<slot>, ...
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(dir_entry("images/"), "")
        if gpi_settings is not None:
            zf.writestr("GPISettings", gpi_settings)
        if saved_settings is not None:
            zf.writestr("SavedSettings", saved_settings)
        zf.writestr(dir_entry("quickfiles/"), "")
        zf.writestr(dir_entry("presets/"), "")
        for slot, blob in slot_blobs.items():
            zf.writestr(f"presets/{slot}", blob)
        # Bonus: annotated state dump for humans. Smart Remote ignores unknown
        # entries on restore, so this just rides along inside the archive.
        zf.writestr(f"{label}_config_readable.txt", readable_text)

    return buf.getvalue(), {
        "label": label,
        "slots": slots,
        "slot_sizes": slot_sizes,
        "gpi_size": len(gpi_settings) if gpi_settings is not None else 0,
        "saved_size": len(saved_settings) if saved_settings is not None else 0,
        "warnings": warnings,
    }
