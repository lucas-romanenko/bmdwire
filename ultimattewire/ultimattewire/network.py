# SPDX-License-Identifier: MIT
"""
Ultimatte 9998 text channel: read and set the unit's network interface.

This is the Ultimatte half of what ATEM Setup does over its REST API —
address, netmask, gateway, DNS and static-vs-DHCP — for units that are
reachable on the network but not in front of you. Reverse-engineered from
an Ultimatte 12 4K (protocol version 2.1, software 2.1) in 2026-09.

**The block protocol.** After the prelude, the 9998 channel takes blocks:
a CAPS section header ending in a colon, then ``key: value`` lines, then a
BLANK LINE which is what actually submits the block. Nothing happens until
that blank line arrives — a lone command line just sits in the unit's
buffer, which is why single-line probes look like the unit is ignoring you.

The unit answers::

    ACK\\n\\n<the section, echoed back with its CURRENT values>
    NAK\\n\\n                      (unknown section, or a value it won't take)

so a write is self-verifying: the echo is the unit telling you what it now
holds, and callers should believe the echo rather than the ACK.

An EMPTY block — header, blank line, no fields — sets nothing and returns
the same echo, so the read path and the write path are one code path.

**Addresses are one combined field.** ``Static Addresses`` carries
``<ip>/<dotted-netmask>`` (``192.168.1.10/255.255.252.0``) — not a prefix
length, and not two fields. Sending the address without the mask, or with
a ``/21``-style suffix, is rejected.

**No policy here.** ``set_network`` writes what it is told; deciding whether
a change is safe belongs to the caller. Re-addressing a unit over the
network can put it out of reach until someone visits it with a monitor.
"""

import re
import socket
import time
from dataclasses import dataclass, field
from typing import List, Optional

from ultimattewire._protocol import (CONNECT_TIMEOUT, CONTROL_PORT,
                                     _connect_with_retry)

ACK = "ACK"
NAK = "NAK"
PRELUDE_END = b"END PRELUDE:"
REPLY_QUIET_TIMEOUT = 2.0
PRELUDE_QUIET_TIMEOUT = 2.0


class UltimatteNetworkError(Exception):
    """The unit refused a block (NAK), or never answered one."""


@dataclass
class NetworkInterface:
    """One ``NETWORK INTERFACE n`` section, as the unit reports it.

    ``address`` / ``netmask`` are the CURRENT ones (what the unit is using);
    ``static_address`` / ``static_netmask`` are the CONFIGURED ones. They
    differ while a unit is on DHCP, or between setting a static address and
    the interface reapplying.
    """
    index: int = 0
    name: str = ""
    mac: str = ""
    priority: Optional[int] = None
    dynamic: Optional[bool] = None
    address: str = ""
    netmask: str = ""
    gateway: str = ""
    dns: List[str] = field(default_factory=list)
    static_address: str = ""
    static_netmask: str = ""
    static_gateway: str = ""
    static_dns: List[str] = field(default_factory=list)
    raw: str = ""


def section_name(index=0):
    return f"NETWORK INTERFACE {int(index)}"


def _split_address(value):
    """``'192.168.1.10/255.255.252.0'`` -> ``('192.168.1.10', '255.255.252.0')``.

    The unit may list more than one address; the first is the one that
    matters to callers. An address with no mask yields an empty mask rather
    than raising — reading must never fail on an odd unit.
    """
    first = (value or "").replace(",", " ").split()
    if not first:
        return "", ""
    head = first[0]
    if "/" in head:
        ip, _, mask = head.partition("/")
        return ip.strip(), mask.strip()
    return head.strip(), ""


def _split_list(value):
    return [p for p in (value or "").replace(",", " ").split() if p]


def parse_interface(text, index=0):
    """Parse an echoed ``NETWORK INTERFACE n`` section into a dataclass."""
    def get(key):
        m = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", text, re.MULTILINE)
        return m.group(1).strip() if m else ""

    iface = NetworkInterface(index=index, raw=text)
    iface.name = get("Name")
    iface.mac = get("MAC Address")
    priority = get("Priority")
    iface.priority = int(priority) if priority.isdigit() else None
    dynamic = get("Dynamic IP").lower()
    iface.dynamic = True if dynamic == "true" else False if dynamic == "false" else None
    iface.address, iface.netmask = _split_address(get("Current Addresses"))
    iface.gateway = get("Current Gateway")
    iface.dns = _split_list(get("Current DNS Servers"))
    iface.static_address, iface.static_netmask = _split_address(get("Static Addresses"))
    iface.static_gateway = get("Static Gateway")
    iface.static_dns = _split_list(get("Static DNS Servers"))
    return iface


def build_block(index=0, **fields):
    """Render a submittable block. Field ORDER is preserved; an empty
    ``fields`` gives the query form (header + the submitting blank line)."""
    lines = [f"{section_name(index)}:"]
    lines += [f"{key}: {value}" for key, value in fields.items()]
    return "\n".join(lines) + "\n\n"


def _drain_prelude(sock, quiet_timeout):
    sock.settimeout(quiet_timeout)
    buf = bytearray()
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        buf.extend(chunk)
        if PRELUDE_END in buf:
            break
    return bytes(buf)


def _read_reply(sock, quiet_timeout):
    sock.settimeout(quiet_timeout)
    buf = bytearray()
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        buf.extend(chunk)
        text = buf.decode("utf-8", errors="replace")
        # NAK is the whole reply; ACK is followed by the echoed section,
        # which ends with its own blank line.
        if text.startswith(NAK):
            break
        if text.startswith(ACK) and text.rstrip().endswith(("\n", ":")) and text.count("\n\n") >= 2:
            break
    return buf.decode("utf-8", errors="replace")


def exchange(host, block, connect_timeout=CONNECT_TIMEOUT,
             quiet_timeout=REPLY_QUIET_TIMEOUT,
             prelude_timeout=PRELUDE_QUIET_TIMEOUT):
    """Send one block on a fresh 9998 session; return the echoed body.

    Raises ``UltimatteNetworkError`` on NAK or on no reply at all.
    """
    sock = _connect_with_retry(host, CONTROL_PORT, timeout=connect_timeout)
    try:
        _drain_prelude(sock, prelude_timeout)
        sock.sendall(block.encode("utf-8"))
        reply = _read_reply(sock, quiet_timeout)
    finally:
        try:
            sock.close()
        except Exception:                                      # noqa: BLE001
            pass
    stripped = reply.lstrip()
    if stripped.startswith(NAK):
        raise UltimatteNetworkError(
            f"unit refused the block (NAK):\n{block.strip()}")
    if not stripped.startswith(ACK):
        raise UltimatteNetworkError(
            f"no ACK from {host} (got {reply[:80]!r})")
    return stripped[len(ACK):].lstrip("\n")


def read_network(host, index=0, **kw):
    """Read ``NETWORK INTERFACE <index>`` from the unit at ``host``.

    Uses the query form of the block protocol — one short session, no
    prelude parsing, nothing written.
    """
    return parse_interface(exchange(host, build_block(index), **kw), index)


def set_network(host, index=0, *, address=None, netmask=None, gateway=None,
                dns=None, dynamic=None, settle=0.0, **kw):
    """Set static network parameters and return the unit's echoed state.

    ``address`` and ``netmask`` travel together in one ``Static Addresses``
    field, so supplying one without the other is an error — the unit would
    be told to drop the half you left out. Pass ``dns=[]`` to clear the DNS
    list; ``dns=None`` leaves it alone.

    Writes what it is told: see the module docstring — the caller owns the
    question of whether a change is safe.

    ``settle`` sleeps before returning, for a caller that wants the echo to
    reflect a reapplied interface rather than the instant of the write.
    """
    if (address is None) != (netmask is None):
        raise ValueError(
            "address and netmask are one field on the wire — pass both or neither")
    fields = {}
    if dynamic is not None:
        fields["Dynamic IP"] = "true" if dynamic else "false"
    if address is not None:
        fields["Static Addresses"] = f"{address}/{netmask}"
    if gateway is not None:
        fields["Static Gateway"] = gateway
    if dns is not None:
        fields["Static DNS Servers"] = " ".join(dns)
    if not fields:
        raise ValueError("nothing to set")
    echoed = exchange(host, build_block(index, **fields), **kw)
    if settle:
        time.sleep(settle)
    return parse_interface(echoed, index)
