# SPDX-License-Identifier: MIT
"""
ultimattewire — Blackmagic Ultimatte 12 control library.

Two TCP channels. 9998 is Blackmagic's documented text protocol
("Ultimatte 12 Ethernet Protocol" v2.0): ``Ultimatte`` is a client for
all of it (state, live updates, every control, files, GPI event lists,
frame buffers, camera control), and ``read_network`` / ``set_network``
drive its network block. 9996 is an undocumented binary channel,
reverse-engineered from packet captures against real hardware; opcodes,
frame format, byte order, and sequence must match the unit's parser
exactly. See ultimattewire.profile for the Smart Remote-compatible
Archive/Restore built on it (the first public implementation of it
outside BMD's own tools).
"""

from ultimattewire._errors import UltimatteError
from ultimattewire.client import (CurrentFile, DeviceInfo, FrameBuffer,
                                  FrameBufferState, GpiList, Identity,
                                  Ultimatte, UltimatteConnectionError,
                                  UltimatteMessageError, UltimatteNak,
                                  UltimatteState, UltimatteTimeout, Update,
                                  VersionInfo)
from ultimattewire.controls import ControlValueError, UnsupportedError
from ultimattewire.media import UltimatteImageError
from ultimattewire.network import (NetworkInterface, UltimatteNetworkError,
                                   read_network, set_network)
from ultimattewire.profile import archive_unit_to_bytes, restore_unit_from_bytes

__all__ = [
    "archive_unit_to_bytes",
    "restore_unit_from_bytes",
    "read_network",
    "set_network",
    "NetworkInterface",
    "UltimatteNetworkError",
    "Ultimatte",
    "UltimatteState",
    "Update",
    "Identity",
    "VersionInfo",
    "DeviceInfo",
    "CurrentFile",
    "GpiList",
    "FrameBuffer",
    "FrameBufferState",
    "UltimatteError",
    "UltimatteConnectionError",
    "UltimatteTimeout",
    "UltimatteNak",
    "UltimatteMessageError",
    "UltimatteImageError",
    "ControlValueError",
    "UnsupportedError",
]
