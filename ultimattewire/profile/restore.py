# SPDX-License-Identifier: MIT
"""
Ultimatte 12 / 12 4K restore — pushes a Smart Remote-style archive zip
back to a unit. Replicates the protocol Software Control uses for
"Restore".

This module is a port of the standalone restore script that was
reverse-engineered from packet captures against a real
Ultimatte 12 4K. Protocol details (opcodes, frame format, trailing 0x00
byte, restore order, 'name' field format) MUST NOT be changed without
re-validating against hardware — the unit will not ACK frames that
don't match.

Protocol (TCP 9996, one upload per connection):
    Request : [opcode:2BE][name_len:4BE][name:N][data_len:4BE][data:M][0x00]
    Response: 6 bytes of zeros = success ACK

    Opcodes:
        0x0100  write a preset slot (mirror of read 0x0000)
        0x0103  write GPISettings or SavedSettings (mirror of read 0x0003)

    Software Control puts the full filesystem path of the extracted zip
    member into the 'name' field. The unit only cares about the last
    path segment (and that "presets/" appears for slot writes), so we
    mimic the Software Control path layout for safety. The trailing
    0x00 byte is required — the unit will not ACK without it.

Restore order (matches Software Control captures):
    1. presets/<slot>  for every slot in the zip
    2. GPISettings
    3. SavedSettings   (overwrites live state, so save it for last)
"""

import io
import zipfile

from ultimattewire._protocol import CONNECT_TIMEOUT, READ_TIMEOUT, upload_one


OP_WRITE_SLOT = 0x0100         # for presets/<slot>
OP_WRITE_RESOURCE = 0x0103     # for GPISettings, SavedSettings

# Path prefix mimics Software Control captures. The unit only inspects
# the last path segment + the "presets/" anchor; the rest is cosmetic
# but kept identical for forensic parity.
_WIRE_NAME_PREFIX = (
    "/private/var/folders/00/000000000000000000000000000000"
    "/T/Ultimatte Software Control-PYREST"
)


def restore_unit_from_bytes(host, zip_bytes, connect_timeout=CONNECT_TIMEOUT,
                            read_timeout=READ_TIMEOUT):
    """Restore one Ultimatte from in-memory zip bytes.

    Returns (ok: bool, info: dict). info contains the per-resource log:
        {
            "presets": [(slot, ok, err_or_None), ...],
            "gpi": (ok, err_or_None) or None if absent,
            "saved": (ok, err_or_None) or None if absent,
            "stopped_at": "<resource>" or None,  # set if a failure aborted
        }
    """
    info = {"presets": [], "gpi": None, "saved": None, "stopped_at": None}

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()

        # 1) Push all presets first
        preset_names = sorted(
            n for n in names if n.startswith("presets/") and not n.endswith("/")
        )
        for member in preset_names:
            slot = member.split("/", 1)[1]
            blob = zf.read(member)
            wire_name = f"{_WIRE_NAME_PREFIX}/presets/{slot}"
            try:
                upload_one(host, OP_WRITE_SLOT, wire_name, blob,
                           connect_timeout=connect_timeout,
                           read_timeout=read_timeout)
                info["presets"].append((slot, True, None))
            except Exception as e:
                info["presets"].append((slot, False, str(e)))
                info["stopped_at"] = f"preset:{slot}"
                return False, info

        # 2) GPISettings
        if "GPISettings" in names:
            blob = zf.read("GPISettings")
            wire_name = f"{_WIRE_NAME_PREFIX}/GPISettings"
            try:
                upload_one(host, OP_WRITE_RESOURCE, wire_name, blob,
                           connect_timeout=connect_timeout,
                           read_timeout=read_timeout)
                info["gpi"] = (True, None)
            except Exception as e:
                info["gpi"] = (False, str(e))
                info["stopped_at"] = "GPISettings"
                return False, info

        # 3) SavedSettings last (this one overwrites the live state)
        if "SavedSettings" in names:
            blob = zf.read("SavedSettings")
            wire_name = f"{_WIRE_NAME_PREFIX}/SavedSettings"
            try:
                upload_one(host, OP_WRITE_RESOURCE, wire_name, blob,
                           connect_timeout=connect_timeout,
                           read_timeout=read_timeout)
                info["saved"] = (True, None)
            except Exception as e:
                info["saved"] = (False, str(e))
                info["stopped_at"] = "SavedSettings"
                return False, info

    return True, info
