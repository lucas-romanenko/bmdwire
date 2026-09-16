# SPDX-License-Identifier: MIT
"""9998 block protocol: reading and setting the network interface.

The wire facts pinned here were taken off an Ultimatte 12 4K (protocol 2.1)
and are the whole reason the module works: the submitting BLANK LINE, the
ACK/NAK prefix, the echoed section, and ``<ip>/<dotted-mask>`` as ONE field.
Nothing here touches a network.
"""
import socket

import pytest

from ultimattewire import network
from ultimattewire.network import (NetworkInterface, UltimatteNetworkError,
                                   build_block, parse_interface, read_network,
                                   section_name, set_network)

PRELUDE = b"PROTOCOL PREAMBLE:\nVersion: 2.1\n\nEND PRELUDE:\n"

ECHO = (
    "NETWORK INTERFACE 0:\n"
    "Name: BMD 10G Ethernet MAC\n"
    "Priority: 0\n"
    "MAC Address: 7c:2e:0d:00:00:01\n"
    "Dynamic IP: false\n"
    "Current Addresses: 192.0.2.98/255.255.252.0\n"
    "Current Gateway: 192.0.2.1\n"
    "Current DNS Servers: \n"
    "Static Addresses: 192.0.2.98/255.255.252.0\n"
    "Static Gateway: 192.0.2.1\n"
    "Static DNS Servers:\n"
    "\n"
)


@pytest.fixture
def wired(monkeypatch, fake_socket_cls):
    """Script a unit one SESSION per reply — a set is two sessions (the write,
    then the read-back). Returns the list of sockets, so a test can inspect
    exactly what was sent on each."""
    def _wire(*replies):
        socks = []

        def connect(host, port, timeout=None):
            reply = replies[min(len(socks), len(replies) - 1)]
            sock = fake_socket_cls([PRELUDE, reply.encode(), socket.timeout()])
            socks.append(sock)
            return sock

        monkeypatch.setattr(network, "_connect_with_retry", connect)
        return socks
    return _wire


# ---- block rendering -------------------------------------------------------

def test_a_query_block_is_the_header_and_the_submitting_blank_line():
    assert build_block(0) == "NETWORK INTERFACE 0:\n\n"


def test_a_set_block_ends_with_the_blank_line_that_submits_it():
    block = build_block(0, **{"Static Gateway": "192.0.2.1"})
    assert block == "NETWORK INTERFACE 0:\nStatic Gateway: 192.0.2.1\n\n"
    assert block.endswith("\n\n"), "without the blank line the unit never processes it"


def test_the_index_selects_the_section():
    assert section_name(2) == "NETWORK INTERFACE 2"
    assert build_block(2).startswith("NETWORK INTERFACE 2:")


# ---- parsing ---------------------------------------------------------------

def test_parse_splits_the_combined_address_field():
    iface = parse_interface(ECHO)
    assert (iface.address, iface.netmask) == ("192.0.2.98", "255.255.252.0")
    assert (iface.static_address, iface.static_netmask) == ("192.0.2.98", "255.255.252.0")


def test_parse_reads_the_rest_of_the_section():
    iface = parse_interface(ECHO)
    assert iface.mac == "7c:2e:0d:00:00:01"
    assert iface.name == "BMD 10G Ethernet MAC"
    assert iface.gateway == "192.0.2.1"
    assert iface.dynamic is False
    assert iface.priority == 0
    assert iface.dns == [] and iface.static_dns == []


def test_dynamic_ip_true_is_read_as_dhcp():
    assert parse_interface(ECHO.replace("Dynamic IP: false", "Dynamic IP: true")).dynamic is True


def test_an_absent_dynamic_flag_is_unknown_not_false():
    """A unit that does not report the flag must not be read as 'static'."""
    assert parse_interface(ECHO.replace("Dynamic IP: false\n", "")).dynamic is None


def test_an_address_without_a_mask_does_not_raise():
    iface = parse_interface(ECHO.replace("192.0.2.98/255.255.252.0", "192.0.2.98"))
    assert (iface.address, iface.netmask) == ("192.0.2.98", "")


def test_multiple_addresses_take_the_first():
    iface = parse_interface(ECHO.replace(
        "Current Addresses: 192.0.2.98/255.255.252.0",
        "Current Addresses: 192.0.2.98/255.255.252.0, 10.0.0.5/255.0.0.0"))
    assert (iface.address, iface.netmask) == ("192.0.2.98", "255.255.252.0")


def test_dns_servers_parse_as_a_list():
    iface = parse_interface(ECHO.replace(
        "Static DNS Servers:", "Static DNS Servers: 8.8.8.8 8.8.4.4"))
    assert iface.static_dns == ["8.8.8.8", "8.8.4.4"]


# ---- read ------------------------------------------------------------------

def test_read_network_queries_and_parses(wired):
    socks = wired("ACK\n\n" + ECHO)
    iface = read_network("192.0.2.21")
    assert isinstance(iface, NetworkInterface)
    assert iface.netmask == "255.255.252.0"
    assert socks[0].sent == b"NETWORK INTERFACE 0:\n\n", "read must set nothing"
    assert socks[0].closed


def test_a_nak_is_an_error_not_an_empty_result(wired):
    wired("NAK\n\n")
    with pytest.raises(UltimatteNetworkError, match="NAK"):
        read_network("192.0.2.21")


def test_silence_is_an_error(wired):
    wired("")
    with pytest.raises(UltimatteNetworkError, match="no ACK"):
        read_network("192.0.2.21")


# ---- set -------------------------------------------------------------------

def test_set_sends_address_and_mask_as_one_field(wired):
    widened = ECHO.replace("255.255.252.0", "255.255.248.0")
    socks = wired("ACK\n\nNETWORK INTERFACE 0:\nStatic Addresses: x\n\n",
                  "ACK\n\n" + widened)
    iface = set_network("192.0.2.21", address="192.0.2.98", netmask="255.255.248.0")
    assert b"Static Addresses: 192.0.2.98/255.255.248.0\n" in socks[0].sent
    assert socks[0].sent.endswith(b"\n\n")
    assert iface.static_netmask == "255.255.248.0"


def test_set_verifies_by_reading_back_not_by_trusting_the_echo(wired):
    """The set echo carries only the fields that were sent, so the state must
    come from a fresh query — the second session, whose block sets nothing."""
    socks = wired("ACK\n\nNETWORK INTERFACE 0:\nStatic Addresses: x\n\n",
                  "ACK\n\n" + ECHO)
    set_network("192.0.2.21", address="192.0.2.98", netmask="255.255.248.0")
    assert len(socks) == 2, "a set is the write plus a read-back"
    assert socks[1].sent == b"NETWORK INTERFACE 0:\n\n"


def test_set_returns_what_the_unit_says_not_what_was_asked(wired):
    """If the unit keeps the old value, the caller must be able to see that."""
    wired("ACK\n\nNETWORK INTERFACE 0:\nStatic Addresses: x\n\n", "ACK\n\n" + ECHO)
    iface = set_network("192.0.2.21", address="192.0.2.98", netmask="255.255.248.0")
    assert iface.static_netmask == "255.255.252.0"


REAPPLYING = ECHO.replace(
    "Current Addresses: 192.0.2.98/255.255.252.0",
    "Current Addresses: 0.0.0.0/255.255.0.0").replace(
    "Static Addresses: 192.0.2.98/255.255.252.0",
    "Static Addresses: 192.0.2.98/255.255.248.0")

SETTLED = ECHO.replace("255.255.252.0", "255.255.248.0")

SET_ACK = "ACK\n\nNETWORK INTERFACE 0:\nStatic Addresses: x\n\n"


def test_a_mid_reapply_read_is_not_accepted_as_settled(wired, monkeypatch):
    """For ~1s after an address change the unit answers but reports
    Current Addresses: 0.0.0.0 while Static already holds the new value.
    Returning that would hand the caller garbage in the live fields."""
    monkeypatch.setattr(network, "READBACK_POLL", 0)
    wired(SET_ACK, "ACK\n\n" + REAPPLYING, "ACK\n\n" + SETTLED)
    iface = set_network("192.0.2.21", address="192.0.2.98", netmask="255.255.248.0")
    assert iface.address == "192.0.2.98", "must not return the 0.0.0.0 window"
    assert iface.netmask == "255.255.248.0"


def test_an_interface_that_never_settles_raises(wired, monkeypatch):
    monkeypatch.setattr(network, "READBACK_POLL", 0)
    wired(SET_ACK, "ACK\n\n" + REAPPLYING)
    with pytest.raises(UltimatteNetworkError, match="did not settle"):
        set_network("192.0.2.21", address="192.0.2.98", netmask="255.255.248.0",
                    readback_timeout=0)


def test_current_must_match_static_before_a_set_is_believed(wired, monkeypatch):
    """Static taking the value is not the same as the interface running it."""
    monkeypatch.setattr(network, "READBACK_POLL", 0)
    stale = ECHO.replace("Static Addresses: 192.0.2.98/255.255.252.0",
                         "Static Addresses: 192.0.2.98/255.255.248.0")
    wired(SET_ACK, "ACK\n\n" + stale)
    with pytest.raises(UltimatteNetworkError, match="did not settle"):
        set_network("192.0.2.21", address="192.0.2.98", netmask="255.255.248.0",
                    readback_timeout=0)


def test_a_set_that_touches_no_address_does_not_wait_for_the_interface(wired):
    """A gateway- or DNS-only change reapplies nothing, so it must not be
    held up waiting for Current to match."""
    socks = wired(SET_ACK, "ACK\n\n" + ECHO)
    iface = set_network("192.0.2.21", gateway="192.0.2.1")
    assert iface.static_gateway == "192.0.2.1"
    assert len(socks) == 2


def test_a_write_that_cannot_be_read_back_raises_loudly(wired):
    """Accepted-then-unreachable is the dangerous case: never report success."""
    wired(SET_ACK, "")
    with pytest.raises(UltimatteNetworkError, match="CHECK THIS UNIT"):
        set_network("192.0.2.21", address="192.0.2.98", netmask="255.255.248.0",
                    readback_timeout=0)


def test_an_address_without_its_mask_is_refused_before_the_wire(wired):
    socks = wired("ACK\n\n" + ECHO)
    with pytest.raises(ValueError, match="one field"):
        set_network("192.0.2.21", address="192.0.2.98")
    assert socks == [], "nothing may reach the unit"


def test_a_mask_without_its_address_is_refused_too(wired):
    with pytest.raises(ValueError, match="one field"):
        set_network("192.0.2.21", netmask="255.255.248.0")


def test_setting_nothing_is_refused(wired):
    with pytest.raises(ValueError, match="nothing to set"):
        set_network("192.0.2.21")


def test_gateway_and_dns_can_be_set_alone(wired):
    socks = wired("ACK\n\n" + ECHO)
    set_network("192.0.2.21", gateway="192.0.2.1", dns=["8.8.8.8", "1.1.1.1"])
    assert b"Static Gateway: 192.0.2.1\n" in socks[0].sent
    assert b"Static DNS Servers: 8.8.8.8 1.1.1.1\n" in socks[0].sent
    assert b"Static Addresses" not in socks[0].sent


def test_dns_empty_list_clears_and_none_leaves_alone(wired):
    socks = wired("ACK\n\n" + ECHO)
    set_network("192.0.2.21", dns=[])
    assert b"Static DNS Servers: \n" in socks[0].sent
    socks2 = wired("ACK\n\n" + ECHO)
    set_network("192.0.2.21", gateway="192.0.2.1")
    assert b"Static DNS Servers" not in socks2[0].sent


def test_switching_to_dhcp_is_expressible(wired):
    socks = wired("ACK\n\n" + ECHO)
    set_network("192.0.2.21", dynamic=True)
    assert b"Dynamic IP: true\n" in socks[0].sent


def test_a_refused_set_raises_rather_than_reporting_success(wired):
    wired("NAK\n\n")
    with pytest.raises(UltimatteNetworkError):
        set_network("192.0.2.21", address="192.0.2.98", netmask="255.255.248.0")
