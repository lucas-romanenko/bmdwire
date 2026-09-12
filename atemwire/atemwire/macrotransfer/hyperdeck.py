# Copyright 2021 - 2022, Martijn Braam and the OpenAtem contributors
# SPDX-License-Identifier: LGPL-3.0-only
"""Macro codec — HyperDeck binding ops (network address + switcher input).

Reverse-engineered live 2026-06-09 by recording HyperDeck settings changes
on an ATEM and decoding the macro (the record, save, download, decode
round-trip). Two op codes:

  0x0110  HyperDeckNetworkAddress  [u8 slot][u8 0][u16 marker 0x024a][u32 IP]
          The IPv4 is stored byte-REVERSED (little-endian) at 4-7; bytes 2-3
          are a constant 0x024a marker observed in every capture.
  0x011A  HyperDeckInput           [u8 slot][u8 0][u16 LE switcher input]
"""

import socket
import struct

from atemwire.macrotransfer._helpers import _u16le

# Constant in every observed 0x0110 op (between slot and the reversed IP).
_HD_IP_MARKER = b'\x4a\x02'


# --- decoders ---

def _d_hd_network_address(params, mx):
    # IPv4 is stored reversed (little-endian byte order) at offset 4-7.
    return {'slot': str(params[0]),
            'networkAddress': socket.inet_ntoa(bytes(params[4:8][::-1]))}

def _d_hd_input(params, mx):
    return {'slot': str(params[0]),
            'input': str(_u16le(params, 2))}


# --- encoders ---

def _e_hd_network_address(attrs):
    ip_rev = socket.inet_aton(attrs['networkAddress'])[::-1]
    return bytes([int(attrs['slot']), 0]) + _HD_IP_MARKER + ip_rev

def _e_hd_input(attrs):
    return bytes([int(attrs['slot']), 0]) + struct.pack('<H', int(attrs['input']))
