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
    "MAC Address: 7c:2e:0d:19:03:4a\n"
    "Dynamic IP: false\n"
    "Current Addresses: 192.168.81.98/255.255.252.0\n"
    "Current Gateway: 192.168.80.1\n"
    "Current DNS Servers: \n"
    "Static Addresses: 192.168.81.98/255.255.252.0\n"
    "Static Gateway: 192.168.80.1\n"
    "Static DNS Servers:\n"
    "\n"
)


@pytest.fixture
def wired(monkeypatch, fake_socket_cls):
    """Script a unit: prelude, then one reply. Returns the socket so a test
    can read back exactly what got sent."""
    def _wire(reply):
        sock = fake_socket_cls([PRELUDE, reply.encode(), socket.timeout()])
        monkeypatch.setattr(network, "_connect_with_retry",
                            lambda host, port, timeout=None: sock)
        return sock
    return _wire


# ---- block rendering -------------------------------------------------------

def test_a_query_block_is_the_header_and_the_submitting_blank_line():
    assert build_block(0) == "NETWORK INTERFACE 0:\n\n"


def test_a_set_block_ends_with_the_blank_line_that_submits_it():
    block = build_block(0, **{"Static Gateway": "192.168.80.1"})
    assert block == "NETWORK INTERFACE 0:\nStatic Gateway: 192.168.80.1\n\n"
    assert block.endswith("\n\n"), "without the blank line the unit never processes it"


def test_the_index_selects_the_section():
    assert section_name(2) == "NETWORK INTERFACE 2"
    assert build_block(2).startswith("NETWORK INTERFACE 2:")


# ---- parsing ---------------------------------------------------------------

def test_parse_splits_the_combined_address_field():
    iface = parse_interface(ECHO)
    assert (iface.address, iface.netmask) == ("192.168.81.98", "255.255.252.0")
    assert (iface.static_address, iface.static_netmask) == ("192.168.81.98", "255.255.252.0")


def test_parse_reads_the_rest_of_the_section():
    iface = parse_interface(ECHO)
    assert iface.mac == "7c:2e:0d:19:03:4a"
    assert iface.name == "BMD 10G Ethernet MAC"
    assert iface.gateway == "192.168.80.1"
    assert iface.dynamic is False
    assert iface.priority == 0
    assert iface.dns == [] and iface.static_dns == []


def test_dynamic_ip_true_is_read_as_dhcp():
    assert parse_interface(ECHO.replace("Dynamic IP: false", "Dynamic IP: true")).dynamic is True


def test_an_absent_dynamic_flag_is_unknown_not_false():
    """A unit that does not report the flag must not be read as 'static'."""
    assert parse_interface(ECHO.replace("Dynamic IP: false\n", "")).dynamic is None


def test_an_address_without_a_mask_does_not_raise():
    iface = parse_interface(ECHO.replace("192.168.81.98/255.255.252.0", "192.168.81.98"))
    assert (iface.address, iface.netmask) == ("192.168.81.98", "")


def test_multiple_addresses_take_the_first():
    iface = parse_interface(ECHO.replace(
        "Current Addresses: 192.168.81.98/255.255.252.0",
        "Current Addresses: 192.168.81.98/255.255.252.0, 10.0.0.5/255.0.0.0"))
    assert (iface.address, iface.netmask) == ("192.168.81.98", "255.255.252.0")


def test_dns_servers_parse_as_a_list():
    iface = parse_interface(ECHO.replace(
        "Static DNS Servers:", "Static DNS Servers: 8.8.8.8 8.8.4.4"))
    assert iface.static_dns == ["8.8.8.8", "8.8.4.4"]


# ---- read ------------------------------------------------------------------

def test_read_network_queries_and_parses(wired):
    sock = wired("ACK\n\n" + ECHO)
    iface = read_network("192.0.2.21")
    assert isinstance(iface, NetworkInterface)
    assert iface.netmask == "255.255.252.0"
    assert sock.sent == b"NETWORK INTERFACE 0:\n\n", "read must set nothing"
    assert sock.closed


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
    sock = wired("ACK\n\n" + ECHO.replace("255.255.252.0", "255.255.248.0"))
    iface = set_network("192.0.2.21", address="192.168.81.98", netmask="255.255.248.0")
    assert b"Static Addresses: 192.168.81.98/255.255.248.0\n" in sock.sent
    assert sock.sent.endswith(b"\n\n")
    assert iface.static_netmask == "255.255.248.0", "the echo is the verification"


def test_set_returns_what_the_unit_says_not_what_was_asked(wired):
    """If the unit keeps the old value, the caller must be able to see that."""
    wired("ACK\n\n" + ECHO)
    iface = set_network("192.0.2.21", address="192.168.81.98", netmask="255.255.248.0")
    assert iface.static_netmask == "255.255.252.0"


def test_an_address_without_its_mask_is_refused_before_the_wire(wired):
    sock = wired("ACK\n\n" + ECHO)
    with pytest.raises(ValueError, match="one field"):
        set_network("192.0.2.21", address="192.168.81.98")
    assert sock.sent == b"", "nothing may reach the unit"


def test_a_mask_without_its_address_is_refused_too(wired):
    with pytest.raises(ValueError, match="one field"):
        set_network("192.0.2.21", netmask="255.255.248.0")


def test_setting_nothing_is_refused(wired):
    with pytest.raises(ValueError, match="nothing to set"):
        set_network("192.0.2.21")


def test_gateway_and_dns_can_be_set_alone(wired):
    sock = wired("ACK\n\n" + ECHO)
    set_network("192.0.2.21", gateway="192.168.80.1", dns=["8.8.8.8", "1.1.1.1"])
    assert b"Static Gateway: 192.168.80.1\n" in sock.sent
    assert b"Static DNS Servers: 8.8.8.8 1.1.1.1\n" in sock.sent
    assert b"Static Addresses" not in sock.sent


def test_dns_empty_list_clears_and_none_leaves_alone(wired):
    sock = wired("ACK\n\n" + ECHO)
    set_network("192.0.2.21", dns=[])
    assert b"Static DNS Servers: \n" in sock.sent
    sock2 = wired("ACK\n\n" + ECHO)
    set_network("192.0.2.21", gateway="192.168.80.1")
    assert b"Static DNS Servers" not in sock2.sent


def test_switching_to_dhcp_is_expressible(wired):
    sock = wired("ACK\n\n" + ECHO)
    set_network("192.0.2.21", dynamic=True)
    assert b"Dynamic IP: true\n" in sock.sent


def test_a_refused_set_raises_rather_than_reporting_success(wired):
    wired("NAK\n\n")
    with pytest.raises(UltimatteNetworkError):
        set_network("192.0.2.21", address="192.168.81.98", netmask="255.255.248.0")
