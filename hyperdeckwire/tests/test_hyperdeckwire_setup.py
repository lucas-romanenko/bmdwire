# SPDX-License-Identifier: MIT
"""Unit tests for hyperdeckwire.setup — the port-80 configuration API.

Nothing here touches a network. ``_FakeDeck`` stands in for the deck's
HTTP server: it holds a settings dict shaped like the real responses
(taken off a HyperDeck Studio HD Mini on 9.0.2), answers GETs from it,
applies PUTs to it, and records every request so a test can assert on
the exact method, path and JSON body the client sent.

What is pinned:
  - URL and body shapes for every endpoint the module writes.
  - A 401 names the "Configure via USB and Ethernet" switch; a
    ``success: false`` body raises with the deck's errorMessage.
  - Every setter reads back and raises when the deck holds something
    else (a deck that ignores the write — ``stubborn`` mode).
  - ``set_network`` merges over the current config, writes the full
    object, reads back until the interface is live, and follows an
    address change to the new address.
  - ``set_network_access`` merges over the current list, rejects unknown
    protocols and states, and skips the read-back when the deck says a
    reboot is needed.
  - ``reboot`` returns the key the GET handed out.
"""

from __future__ import annotations

import copy
import io
import json
import urllib.error

import pytest

from hyperdeckwire import setup as setup_mod
from hyperdeckwire.setup import (
    DISABLED, ENABLED, SECURE_ONLY, HyperdeckSetup, HyperdeckSetupError,
    NetworkInterface, parse_interface,
)

HOST = '192.0.2.11'

IFACE = {
    'hostname': 'HyperDeck-Studio-HD-Mini-3.local',
    'interface': {
        'activeConfig': {'address': '192.0.2.11', 'dnsServers': ['8.8.8.8', '8.8.4.4'],
                         'gateway': '192.0.2.1', 'netmask': '255.255.252.0'},
        'currentMtu': 1500, 'currentSpeedBps': 1000000000, 'currentState': 'GloballyBound',
        'defaultRoute': True, 'id': '0', 'macAddress': '7C-2E-0D-18-DC-78',
        'manualConfig': {'address': '192.0.2.11', 'dnsServers': ['8.8.8.8', '8.8.4.4'],
                         'gateway': '192.0.2.1', 'netmask': '255.255.252.0'},
        'name': '1GbE', 'physicallyLinked': 1, 'physicallyPresent': 1, 'priority': 1,
        'type': 'Static',
    },
}

STATE = {
    '/setupBasic': {'build': '586FB398', 'deviceName': 'Deck A', 'hardware': '0300',
                    'hostname': 'Deck-A', 'language': 'en_US.UTF-8', 'productId': 'BE76',
                    'productName': 'HyperDeck Studio HD Mini',
                    'shortProductName': 'HyperDeck Studio HD Mini', 'software': '9.0.2'},
    '/remoteAdmin': {'enabled': True, 'isUSBRequestSource': False},
    '/network/interface/0': IFACE,
    '/networkAccess': {'protocols': [
        {'id': 'FTP', 'state': 'Enabled', 'url': 'ftp://Deck-A.local'},
        {'id': 'HTTP', 'state': 'Enabled', 'url': 'http://Deck-A.local'}]},
    '/networkAccess/options': {'options': [
        {'protocol': 'FTP', 'secureOption': False, 'showURL': True},
        {'protocol': 'HTTP', 'secureOption': True, 'showURL': True}]},
    '/hyperDeckEthernetProtocol': {'state': 'Enabled'},
    '/certificate/summary': {'hostname': 'Deck-A.local'},
    '/auth/adminStatus': {'adminRequired': False},
    '/auth/users': {'users': [{'authUserId': '1', 'remoteAdminUtilityAccess': False,
                               'sharedFolderPermissions': [], 'username': 'Guest'}]},
    '/dateAndTime': {'time': '1789584763', 'timeFriendly': '20260916-185243', 'timezoneOffset': 0},
    '/dateAndTime/ntp': {'enabled': True, 'serverUrl': 'time.cloudflare.com', 'state': 'OffsetCorrection'},
    '/dateAndTime/timezone': {'timezoneOffset': 0},
    '/reboot': {'key': 'ABC123'},
    '/languages': {'languages': [{'code': 'en_US.UTF-8', 'name': 'English'}]},
    '/capabilities': {'capabilities': {'version': 1}, 'networkAccess': {'version': 1},
                      'hyperDeckEthernetProtocol': {'version': 1}},
}


class _Resp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDeck:
    """The opener hook: ``open(request, timeout)``.

    ``stubborn`` = acknowledge every write with 200 but change nothing.
    ``refuse`` = answer every write with HTTP 401 (remote admin off).
    ``reboot_required`` = /networkAccess PUT answers rebootRequired true.
    """

    def __init__(self, state=None, *, stubborn=False, refuse=False, reboot_required=False,
                 settle_after=0):
        self.state = copy.deepcopy(state if state is not None else STATE)
        self.stubborn = stubborn
        self.refuse = refuse
        self.reboot_required = reboot_required
        self.settle_after = settle_after     # network reads AFTER a write before activeConfig catches up
        self._network_written = False
        self.requests = []                    # (method, path, body-or-None)
        self.hosts = []

    def open(self, req, timeout=None):
        assert req.full_url.startswith('http://')
        host, _, rest = req.full_url[len('http://'):].partition('/admin/api/v1')
        path = rest
        self.hosts.append(host)
        body = json.loads(req.data) if req.data else None
        self.requests.append((req.get_method(), path, body))
        method = req.get_method()
        if method == 'GET':
            return self._get(path)
        if self.refuse:
            raise urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', {}, io.BytesIO(b''))
        return self._write(method, path, body)

    def _ok(self, extra=None):
        payload = {'success': True}
        if extra:
            payload.update(extra)
        return _Resp(json.dumps(payload).encode())

    def _get(self, path):
        if path == '/heartBeat':
            return self._ok()
        if path.startswith('/certificate/signingRequest/'):
            return _Resp(b'-----BEGIN CERTIFICATE REQUEST-----')
        if path not in self.state:
            raise urllib.error.HTTPError('http://x' + path, 404, 'Not Found', {}, io.BytesIO(b''))
        value = copy.deepcopy(self.state[path])
        if path == '/network/interface/0' and self._network_written and self.settle_after > 0:
            self.settle_after -= 1
            value['interface']['activeConfig'] = {'address': '', 'netmask': '', 'gateway': '',
                                                  'dnsServers': []}
        return _Resp(json.dumps({'response': value, 'success': True}).encode())

    def _write(self, method, path, body):
        if path == '/network/interface/0':
            self._network_written = True
        if self.stubborn:
            return self._ok()
        if path == '/setupBasic/name':
            self.state['/setupBasic']['deviceName'] = body['name']
        elif path == '/setupBasic/language':
            self.state['/setupBasic']['language'] = body['language']
        elif path == '/setupBasic/identify':
            pass
        elif path == '/network/interface/0':
            self._network_written = True
            iface = self.state['/network/interface/0']['interface']
            cfg = {'address': body['address'], 'netmask': body['netmask'],
                   'gateway': body['gateway'], 'dnsServers': body['dnsServers']}
            iface['manualConfig'] = dict(cfg)
            iface['activeConfig'] = dict(cfg)
            iface['type'] = body['type']
        elif path == '/networkAccess':
            if self.reboot_required:
                return self._ok({'response': {'rebootRequired': True}})
            self.state['/networkAccess']['protocols'] = [
                dict(p) for p in body['protocols']]
            return self._ok({'response': {'rebootRequired': False}})
        elif path == '/hyperDeckEthernetProtocol':
            self.state['/hyperDeckEthernetProtocol']['state'] = body['state']
        elif path == '/certificate/selfSigned':
            self.state['/certificate/summary'] = {'hostname': 'Deck-A.local', 'domain': 'Deck-A.local',
                                                  'issuer': 'Deck-A.local'}
        elif path == '/certificate' and method == 'POST':
            self.state['/certificate/summary'] = {'hostname': 'Deck-A.local', 'domain': 'deck-a.example',
                                                  'issuer': 'Example CA'}
        elif path == '/certificate' and method == 'DELETE':
            self.state['/certificate/summary'] = {'hostname': 'Deck-A.local'}
        elif path == '/certificate/signingRequest':
            return self._ok({'response': {'id': 'csr-1'}})
        elif path == '/auth/user' and method == 'POST':
            users = self.state['/auth/users']['users']
            users.append({'authUserId': str(len(users) + 1), 'username': body['username'],
                          'remoteAdminUtilityAccess': body.get('remoteAdminUtilityAccess', False),
                          'sharedFolderPermissions': body.get('sharedFolderPermissions', [])})
        elif path == '/auth/user' and method == 'PUT':
            for u in self.state['/auth/users']['users']:
                if u['authUserId'] == body['authUserId']:
                    for k in ('username', 'remoteAdminUtilityAccess', 'sharedFolderPermissions'):
                        if k in body:
                            u[k] = body[k]
        elif path == '/auth/user' and method == 'DELETE':
            self.state['/auth/users']['users'] = [
                u for u in self.state['/auth/users']['users'] if u['authUserId'] != body['authUserId']]
        elif path == '/dateAndTime':
            self.state['/dateAndTime'] = {'time': body['time'], 'timezoneOffset': body['timezoneOffset'],
                                          'timeFriendly': ''}
        elif path == '/dateAndTime/ntp':
            self.state['/dateAndTime/ntp'] = {'enabled': body['enabled'], 'serverUrl': body['serverUrl'],
                                              'state': 'OffsetCorrection'}
        elif path == '/dateAndTime/timezone':
            self.state['/dateAndTime/timezone'] = {'timezoneOffset': body['timezoneOffset']}
        elif path == '/reboot':
            if body.get('key') != self.state['/reboot']['key']:
                return _Resp(json.dumps({'success': False, 'errorMessage': 'bad key'}).encode())
        else:
            raise AssertionError(f'unexpected write {method} {path}')
        return self._ok()


@pytest.fixture
def deck():
    return _FakeDeck()


@pytest.fixture
def client(deck):
    return HyperdeckSetup(HOST, opener=deck)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(setup_mod.time, 'sleep', lambda s: None)


def _writes(deck):
    return [(m, p, b) for m, p, b in deck.requests if m != 'GET']


# ---- transport ---------------------------------------------------------------

def test_urls_are_the_admin_api_on_port_80(client, deck):
    client.setup_basic()
    assert deck.requests == [('GET', '/setupBasic', None)]
    assert deck.hosts == [HOST]


def test_a_401_names_the_usb_and_ethernet_switch(deck):
    deck.refuse = True
    client = HyperdeckSetup(HOST, opener=deck)
    with pytest.raises(HyperdeckSetupError) as exc:
        client.set_ethernet_protocol(ENABLED)
    assert exc.value.status == 401
    assert 'Configure via USB and Ethernet' in str(exc.value)
    assert exc.value.path == '/hyperDeckEthernetProtocol'


def test_a_success_false_body_raises_with_the_decks_message(client, deck):
    deck.state['/reboot']['key'] = 'K1'
    original = deck._write

    def bad_key(method, path, body):
        if path == '/reboot':
            return _Resp(json.dumps({'success': False, 'errorMessage': 'bad key'}).encode())
        return original(method, path, body)
    deck._write = bad_key
    with pytest.raises(HyperdeckSetupError, match='bad key'):
        client.reboot()


def test_other_http_errors_carry_their_status(deck):
    client = HyperdeckSetup(HOST, opener=deck)
    with pytest.raises(HyperdeckSetupError) as exc:
        client._get('/nothingHere')
    assert exc.value.status == 404


def test_connection_failures_propagate_as_oserror():
    class Down:
        def open(self, req, timeout=None):
            raise urllib.error.URLError('connection refused')
    with pytest.raises(OSError):
        HyperdeckSetup(HOST, opener=Down()).heartbeat()


# ---- identity ----------------------------------------------------------------

def test_setup_basic_maps_the_response(client):
    info = client.setup_basic()
    assert info.product_name == 'HyperDeck Studio HD Mini'
    assert info.device_name == 'Deck A'
    assert info.software == '9.0.2'
    assert info.hostname == 'Deck-A'


def test_set_name_writes_and_reads_back(client, deck):
    info = client.set_name('Deck B')
    assert ('PUT', '/setupBasic/name', {'name': 'Deck B'}) in deck.requests
    assert info.device_name == 'Deck B'


def test_set_name_raises_when_the_deck_keeps_the_old_name(deck):
    deck.stubborn = True
    with pytest.raises(HyperdeckSetupError, match='CHECK THIS DECK'):
        HyperdeckSetup(HOST, opener=deck).set_name('Deck B')


def test_identify_is_put_to_start_and_delete_to_stop(client, deck):
    client.identify()
    client.identify(False)
    assert _writes(deck) == [('PUT', '/setupBasic/identify', None),
                             ('DELETE', '/setupBasic/identify', None)]


def test_remote_admin_is_read_only(client):
    ra = client.remote_admin()
    assert ra.enabled is True and ra.usb_request_source is False
    assert not hasattr(client, 'set_remote_admin')
    assert not hasattr(client, 'enable_remote_admin')


def test_capabilities_and_heartbeat(client):
    assert client.heartbeat() is True
    assert {'networkAccess', 'hyperDeckEthernetProtocol'} <= set(client.capabilities())


def test_languages(client, deck):
    assert client.languages() == [{'code': 'en_US.UTF-8', 'name': 'English'}]
    assert client.set_language('de_DE.UTF-8') == 'de_DE.UTF-8'
    assert ('PUT', '/setupBasic/language', {'language': 'de_DE.UTF-8'}) in deck.requests


# ---- network interface -----------------------------------------------------

def test_parse_interface_separates_configured_from_running():
    iface = parse_interface(IFACE)
    assert isinstance(iface, NetworkInterface)
    assert iface.dhcp is False
    assert (iface.address, iface.netmask, iface.gateway) == ('192.0.2.11', '255.255.252.0', '192.0.2.1')
    assert iface.dns == ['8.8.8.8', '8.8.4.4']
    assert iface.active_netmask == '255.255.252.0'
    assert iface.mac == '7C-2E-0D-18-DC-78'
    assert iface.hostname == 'HyperDeck-Studio-HD-Mini-3.local'
    assert iface.settled


def test_an_interface_still_reapplying_is_not_settled():
    payload = copy.deepcopy(IFACE)
    payload['interface']['activeConfig']['netmask'] = '255.255.248.0'
    assert not parse_interface(payload).settled


def test_set_network_merges_over_the_current_config_and_writes_the_full_object(client, deck):
    after = client.set_network(netmask='255.255.248.0')
    assert _writes(deck) == [('PUT', '/network/interface/0', {
        'id': '0', 'type': 'Static', 'address': '192.0.2.11', 'netmask': '255.255.248.0',
        'gateway': '192.0.2.1', 'dnsServers': ['8.8.8.8', '8.8.4.4']})]
    assert after.netmask == '255.255.248.0'
    assert after.active_netmask == '255.255.248.0'


def test_set_network_dns_empty_list_clears_and_none_keeps(client, deck):
    client.set_network(dns=[])
    assert _writes(deck)[-1][2]['dnsServers'] == []
    client.set_network(gateway='192.0.2.254')
    assert _writes(deck)[-1][2]['dnsServers'] == []      # kept from the previous write


def test_set_network_dhcp_switches_the_type(client, deck):
    after = client.set_network(dhcp=True)
    assert _writes(deck)[-1][2]['type'] == 'DHCP'
    assert after.dhcp is True


def test_set_network_waits_for_the_interface_to_settle(deck):
    deck.settle_after = 2          # two reads still show an empty activeConfig
    after = HyperdeckSetup(HOST, opener=deck).set_network(netmask='255.255.248.0')
    assert after.settled
    reads = [p for m, p, _ in deck.requests if m == 'GET' and p.startswith('/network')]
    assert len(reads) == 1 + 3      # the pre-read, two unsettled read-backs, the settled one


def test_set_network_raises_when_the_deck_never_settles(deck):
    deck.settle_after = 10 ** 6
    with pytest.raises(HyperdeckSetupError, match='did not settle'):
        HyperdeckSetup(HOST, opener=deck).set_network(netmask='255.255.248.0', readback_timeout=0.0)


def test_set_network_raises_when_the_deck_ignores_the_write(deck):
    deck.stubborn = True
    with pytest.raises(HyperdeckSetupError, match='did not settle'):
        HyperdeckSetup(HOST, opener=deck).set_network(netmask='255.255.248.0', readback_timeout=0.0)


def test_set_network_reads_back_at_the_new_address(client, deck):
    client.set_network(address='192.0.2.99')
    assert deck.hosts[:2] == [HOST, HOST]          # the pre-read and the PUT
    assert deck.hosts[2:] == ['192.0.2.99']        # the read-back
    assert deck.requests[-1][0] == 'GET'


def test_set_network_with_nothing_to_set_is_an_error(client):
    with pytest.raises(ValueError):
        client.set_network()


# ---- network access --------------------------------------------------------

def test_network_access_reads_protocol_to_state(client):
    assert client.network_access() == {'FTP': 'Enabled', 'HTTP': 'Enabled'}
    assert client.network_access_urls() == {'FTP': 'ftp://Deck-A.local', 'HTTP': 'http://Deck-A.local'}
    assert client.network_access_options() == {'FTP': False, 'HTTP': True}


def test_set_network_access_merges_and_writes_the_full_list(client, deck):
    reboot = client.set_network_access(http=SECURE_ONLY)
    assert reboot is False
    assert _writes(deck) == [('PUT', '/networkAccess', {'protocols': [
        {'id': 'FTP', 'state': 'Enabled'}, {'id': 'HTTP', 'state': 'SecureOnly'}]})]
    assert client.network_access() == {'FTP': 'Enabled', 'HTTP': 'SecureOnly'}


def test_set_network_access_keys_are_case_insensitive(client, deck):
    client.set_network_access(ftp=DISABLED, HTTP=ENABLED)
    assert _writes(deck)[-1][2]['protocols'] == [
        {'id': 'FTP', 'state': 'Disabled'}, {'id': 'HTTP', 'state': 'Enabled'}]


def test_set_network_access_rejects_unknown_protocols_and_states(client, deck):
    with pytest.raises(ValueError, match='not a protocol'):
        client.set_network_access(sftp=ENABLED)
    with pytest.raises(ValueError, match='not one of'):
        client.set_network_access(ftp='On')
    with pytest.raises(ValueError):
        client.set_network_access()
    assert _writes(deck) == []


def test_set_network_access_skips_read_back_when_a_reboot_is_required(deck):
    deck.reboot_required = True
    client = HyperdeckSetup(HOST, opener=deck)
    assert client.set_network_access(ftp=DISABLED) is True
    assert [m for m, _, _ in deck.requests] == ['GET', 'PUT']


def test_set_network_access_raises_when_the_deck_keeps_the_old_state(deck):
    deck.stubborn = True
    with pytest.raises(HyperdeckSetupError, match='networkAccess FTP'):
        HyperdeckSetup(HOST, opener=deck).set_network_access(ftp=DISABLED)


def test_ethernet_protocol_round_trip(client, deck):
    assert client.ethernet_protocol() == 'Enabled'
    assert client.set_ethernet_protocol(DISABLED) == 'Disabled'
    assert ('PUT', '/hyperDeckEthernetProtocol', {'state': 'Disabled'}) in deck.requests
    with pytest.raises(ValueError):
        client.set_ethernet_protocol('On')


def test_ethernet_protocol_default_is_enabled(client, deck):
    client.set_ethernet_protocol()
    assert _writes(deck)[-1] == ('PUT', '/hyperDeckEthernetProtocol', {'state': 'Enabled'})


# ---- certificate -----------------------------------------------------------

def test_certificate_calls(client, deck):
    assert client.certificate_summary() == {'hostname': 'Deck-A.local'}
    assert client.create_self_signed_certificate()['issuer'] == 'Deck-A.local'
    assert client.upload_certificate('PEM')['issuer'] == 'Example CA'
    assert ('POST', '/certificate', {'certificate': 'PEM'}) in deck.requests
    assert client.delete_certificate() == {'hostname': 'Deck-A.local'}
    assert ('DELETE', '/certificate', None) in deck.requests


def test_signing_request(client, deck):
    out = client.create_signing_request(common_name='deck-a.example', country='CA',
                                        state_name='Example State', locality='Example City',
                                        organization='Studio')
    assert out['response'] == {'id': 'csr-1'}
    assert _writes(deck)[-1] == ('POST', '/certificate/signingRequest', {
        'commonName': 'deck-a.example', 'country': 'CA', 'stateName': 'Example State',
        'locality': 'Example City', 'organization': 'Studio'})
    assert client.download_signing_request('csr-1').startswith(b'-----BEGIN')


# ---- users -----------------------------------------------------------------

def test_users(client, deck):
    assert client.admin_required() is False
    assert [u.username for u in client.users()] == ['Guest']
    created = client.create_user('ops', 'secret')
    assert created.auth_user_id == '2'
    assert ('POST', '/auth/user', {'username': 'ops', 'password': 'secret',
                                   'remoteAdminUtilityAccess': False}) in deck.requests
    updated = client.update_user('2', username='ops2', remote_admin_utility_access=True)
    assert (updated.username, updated.remote_admin_utility_access) == ('ops2', True)
    client.delete_user('2')
    assert [u.username for u in client.users()] == ['Guest']
    with pytest.raises(ValueError):
        client.update_user('1')


def test_user_writes_are_verified(deck):
    deck.stubborn = True
    client = HyperdeckSetup(HOST, opener=deck)
    with pytest.raises(HyperdeckSetupError):
        client.create_user('ops', 'secret')
    with pytest.raises(HyperdeckSetupError):
        client.delete_user('1')


# ---- date and time ---------------------------------------------------------

def test_date_and_time(client, deck):
    now = client.date_and_time()
    assert now == {'time': 1789584763, 'timezone_offset': 0, 'time_friendly': '20260916-185243'}
    after = client.set_date_and_time(1789600000, -240)
    assert ('PUT', '/dateAndTime', {'time': '1789600000', 'timezoneOffset': -240}) in deck.requests
    assert (after['time'], after['timezone_offset']) == (1789600000, -240)


def test_ntp_and_timezone(client, deck):
    assert client.ntp() == {'enabled': True, 'server_url': 'time.cloudflare.com',
                            'state': 'OffsetCorrection'}
    assert client.set_ntp('192.0.2.5')['server_url'] == '192.0.2.5'
    assert ('PUT', '/dateAndTime/ntp', {'serverUrl': '192.0.2.5', 'enabled': True}) in deck.requests
    assert client.timezone_offset() == 0
    assert client.set_timezone_offset(-300) == -300


def test_ntp_write_is_verified(deck):
    deck.stubborn = True
    with pytest.raises(HyperdeckSetupError, match='ntp serverUrl'):
        HyperdeckSetup(HOST, opener=deck).set_ntp('192.0.2.5')


# ---- reboot ----------------------------------------------------------------

def test_reboot_returns_the_key_the_get_handed_out(client, deck):
    client.reboot()
    assert deck.requests == [('GET', '/reboot', None), ('PUT', '/reboot', {'key': 'ABC123'})]


def test_reboot_without_a_key_raises(deck):
    deck.state['/reboot'] = {}
    with pytest.raises(HyperdeckSetupError, match='no reboot key'):
        HyperdeckSetup(HOST, opener=deck).reboot()
