# SPDX-License-Identifier: MIT
"""
Ultimatte 9998 text channel: connect, read the prelude, and parse named
sections out of it. Used by profile/archive (to build the zip) and by
any future feature that needs the unit's current state (label, video
mode, FILE LIST, etc.).
"""

import re
import socket

from ultimattewire._protocol import CONNECT_TIMEOUT, CONTROL_PORT, _connect_with_retry


PREAMBLE_QUIET_TIMEOUT = 2.0


def read_preamble(host, connect_timeout=CONNECT_TIMEOUT,
                  quiet_timeout=PREAMBLE_QUIET_TIMEOUT):
    """Connect to 9998 and read until 'END PRELUDE:'."""
    s = _connect_with_retry(host, CONTROL_PORT, timeout=connect_timeout)
    s.settimeout(quiet_timeout)
    buf = bytearray()
    try:
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf.extend(chunk)
            if b"END PRELUDE:" in buf:
                s.settimeout(0.3)
                try:
                    while True:
                        more = s.recv(65536)
                        if not more:
                            break
                        buf.extend(more)
                except socket.timeout:
                    pass
                break
    finally:
        s.close()
    return buf.decode("utf-8", errors="replace")


def parse_section(text, header):
    """Extract the body of a named CAPS-header section from a prelude."""
    # NOTE: use \Z (end-of-string only), not $ — with re.MULTILINE, $ matches
    # at the end of every line, which would terminate the lazy capture after
    # the first line of the section. This bug caused multi-line FILE LIST
    # sections (e.g. with multiple presets) to be truncated to just the first
    # entry.
    # Also: use [ \t]* (not \s*) after the header colon — \s* matches newlines
    # too, which would cause an empty section to swallow the next section.
    pattern = rf"^{re.escape(header)}:[ \t]*\n(.*?)(?=\n[A-Z][A-Z0-9 ]*:\s*\n|\nEND PRELUDE:|\Z)"
    m = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def parse_file_list(text):
    """Return the list of preset slot names from the FILE LIST section."""
    section = parse_section(text, "FILE LIST")
    if not section:
        return []
    return [ln.strip() for ln in section.splitlines() if ln.strip()]


def extract_label(text):
    """Return the unit's Label field, sanitized for use as a filename root."""
    m = re.search(r"^Label:\s*(.+)$", text, re.MULTILINE)
    if m:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", m.group(1).strip())
    return "unknown"
