# SPDX-License-Identifier: MIT
"""hyperdeckwire.setup — the deck's configuration API, the one HyperDeck Setup drives.

Everything HyperDeck Setup shows for a networked deck (name, IP address,
netmask, gateway, DNS, the FTP and Web Media Manager switches, the
HyperDeck Ethernet Protocol switch, the certificate, users, date and time,
reboot) lives on a REST API on the deck's HTTP port, at
``http://<deck>/admin/api/v1/``. HyperDeck Setup is a browser onto
``http://<deck>/admin/``; over USB it reaches the same API through the
cable. So anything the app can change, this module can change from the
network, which is what you want for a deck in a rack across the building.

None of these settings exist in the 9993 Ethernet Protocol: its
``configuration:`` verb covers codec, inputs and timecode and nothing about
the network. That is why this is a separate module from :mod:`.client`,
with a separate connection (one HTTP request per call, nothing held open).

Measured on two HyperDeck Studio HD Mini units on software 9.0.2 (September
2026). The API is unauthenticated on the LAN, the same trust model as 9993
and FTP, and it is the same API family the ATEM and Videohub setup apps use,
so the network endpoints carry the same shapes.

Two facts to know before writing anything:

* **"Configure via USB and Ethernet" is read-only from here.** While that
  switch is off the deck answers every GET and refuses every PUT with HTTP
  401, and the PUT that would turn it on is one of the refused ones. It can
  only be turned on over USB. :meth:`HyperdeckSetup.remote_admin` reads it so
  a caller can explain a 401 before trying; there is no setter because one
  could never succeed, and turning it OFF over Ethernet would cut off the
  channel doing the turning.
* **Nothing here is believed on the strength of a 200.** Every setter reads
  the setting back and raises :class:`HyperdeckSetupError` if the deck
  holds something else. A network change is read back until the interface
  is live on the new values (the deck reapplies for about a second).

No policy here: :meth:`HyperdeckSetup.set_network` writes what it is told.
Whether re-addressing a deck in a rack is safe belongs to the caller.

Public surface::

    from hyperdeckwire import HyperdeckSetup

    deck = HyperdeckSetup('192.0.2.11')
    info = deck.setup_basic()          # SetupInfo: product, name, software ...
    net = deck.network()               # NetworkInterface: address, netmask ...
    deck.network_access()              # {'FTP': 'Enabled', 'HTTP': 'Enabled'}
    deck.ethernet_protocol()           # 'Enabled'  (TCP 9993 on)

    deck.set_network_access(ftp='Enabled')
    deck.set_ethernet_protocol('Enabled')
    deck.set_network(netmask='255.255.248.0')   # widens; address/gateway kept
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 4.0        # one HTTP call; the deck answers in milliseconds
READBACK_TIMEOUT = 20.0      # a network change reapplies the interface
READBACK_POLL = 0.5
API_BASE = '/admin/api/v1'

# The three states a network-access protocol can be in (``/networkAccess``
# and ``/hyperDeckEthernetProtocol`` share the enum). ``SecureOnly`` is
# only meaningful where the deck offers a secure variant: HTTP (HTTPS with
# the deck's certificate) does, FTP does not — see ``network_access_options``.
DISABLED = 'Disabled'
ENABLED = 'Enabled'
SECURE_ONLY = 'SecureOnly'
ACCESS_STATES = (DISABLED, ENABLED, SECURE_ONLY)

FTP = 'FTP'
HTTP = 'HTTP'          # the Web Media Manager switch


class HyperdeckSetupError(Exception):
    """The deck refused a request, or a setting did not read back as written.

    ``status`` is the HTTP status when the deck refused (401 = "Configure via
    USB and Ethernet" is off), ``None`` for a read-back mismatch or a
    ``success: false`` body.
    """

    def __init__(self, message: str, *, status: Optional[int] = None, path: str = ''):
        self.status = status
        self.path = path
        super().__init__(message)


@dataclass(frozen=True)
class SetupInfo:
    """``GET /setupBasic``: what the deck says about itself."""

    product_name: str
    device_name: str
    hostname: str
    software: str
    build: str = ''
    hardware: str = ''
    language: str = ''
    product_id: str = ''
    short_product_name: str = ''


@dataclass(frozen=True)
class RemoteAdmin:
    """``GET /remoteAdmin``: the "Configure via USB and Ethernet" switch.

    ``enabled`` False means every PUT from the network is refused (401).
    ``usb_request_source`` says whether THIS request arrived over USB.
    """

    enabled: bool
    usb_request_source: bool


@dataclass
class NetworkInterface:
    """One network interface as the deck reports it.

    ``address`` / ``netmask`` / ``gateway`` / ``dns`` are the CONFIGURED
    (manual) values; the ``active_*`` fields are what the interface is
    RUNNING. They differ on DHCP, and for about a second after a static
    change while the interface reapplies. ``dhcp`` is None when the deck
    reports a type this module does not know.
    """

    id: str = '0'
    name: str = ''
    mac: str = ''
    hostname: str = ''
    dhcp: Optional[bool] = None
    address: str = ''
    netmask: str = ''
    gateway: str = ''
    dns: List[str] = field(default_factory=list)
    active_address: str = ''
    active_netmask: str = ''
    active_gateway: str = ''
    active_dns: List[str] = field(default_factory=list)
    linked: Optional[bool] = None
    speed_bps: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def settled(self) -> bool:
        """Is the interface live on what it is configured with?

        For DHCP that is "has an address". For static it is the running
        address, netmask and gateway equalling the configured ones; while the
        deck reapplies, the running side lags.
        """
        if self.dhcp:
            return bool(self.active_address)
        return bool(self.active_address) and (
            self.active_address == self.address
            and self.active_netmask == self.netmask
            and self.active_gateway == self.gateway)


@dataclass(frozen=True)
class User:
    """One entry of ``GET /auth/users``. A fresh deck has only ``Guest``."""

    auth_user_id: str
    username: str
    remote_admin_utility_access: bool = False
    shared_folder_permissions: List[Dict[str, Any]] = field(default_factory=list)


def parse_interface(payload: Dict[str, Any]) -> NetworkInterface:
    """Parse a ``/network/interface/{id}`` response body (the object holding
    ``interface`` and ``hostname``) into a :class:`NetworkInterface`."""
    iface = payload.get('interface') or {}
    manual = iface.get('manualConfig') or {}
    active = iface.get('activeConfig') or {}
    kind = (iface.get('type') or '').lower()
    dhcp = True if kind == 'dhcp' else False if kind == 'static' else None
    linked = iface.get('physicallyLinked')
    speed = iface.get('currentSpeedBps')
    return NetworkInterface(
        id=str(iface.get('id', '0')),
        name=iface.get('name', ''),
        mac=iface.get('macAddress', ''),
        hostname=payload.get('hostname', ''),
        dhcp=dhcp,
        address=manual.get('address', ''),
        netmask=manual.get('netmask', ''),
        gateway=manual.get('gateway', ''),
        dns=list(manual.get('dnsServers') or []),
        active_address=active.get('address', ''),
        active_netmask=active.get('netmask', ''),
        active_gateway=active.get('gateway', ''),
        active_dns=list(active.get('dnsServers') or []),
        linked=None if linked is None else bool(linked),
        speed_bps=None if speed is None else int(speed),
        raw=payload,
    )


def _check_state(state: str) -> str:
    if state not in ACCESS_STATES:
        raise ValueError(f'{state!r} is not one of {ACCESS_STATES}')
    return state


class HyperdeckSetup:
    """Client for one deck's configuration API.

    Stateless: each method is one or a few HTTP requests. ``timeout`` is
    per request. ``opener`` is a test hook, an object with
    ``open(request, timeout=...)`` returning a response with ``read()`` and
    ``status``; the default is :func:`urllib.request.build_opener`.
    """

    def __init__(self, host: str, *, timeout: float = DEFAULT_TIMEOUT,
                 scheme: str = 'http', opener=None):
        self.host = host
        self.timeout = timeout
        self.scheme = scheme
        self._opener = opener or urllib.request.build_opener()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f'{self.scheme}://{self.host}{API_BASE}{path}'

    def _raw(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
             *, timeout: Optional[float] = None) -> bytes:
        """One request. Raises :class:`HyperdeckSetupError` on a non-2xx
        status; connection-level failures propagate as ``OSError``
        (``urllib.error.URLError`` is one)."""
        data = None
        headers = {'Accept': 'application/json'}
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self._url(path), data=data, method=method,
                                     headers=headers)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8', errors='replace').strip()
            except Exception:                                  # noqa: BLE001
                pass
            if e.code == 401:
                raise HyperdeckSetupError(
                    f'{self.host}: {method} {path} refused (HTTP 401): "Configure via '
                    f'USB and Ethernet" is off on this deck; turn it on in HyperDeck '
                    f'Setup over USB first', status=401, path=path) from None
            raise HyperdeckSetupError(
                f'{self.host}: {method} {path} failed (HTTP {e.code}) {detail[:200]}'.rstrip(),
                status=e.code, path=path) from None

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
              *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """One request, JSON in and out. A body with ``success: false`` is
        the deck saying no — raised with its ``errorMessage``."""
        raw = self._raw(method, path, body, timeout=timeout)
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise HyperdeckSetupError(
                f'{self.host}: {method} {path} answered something that is not JSON: '
                f'{raw[:120]!r}', path=path) from None
        if isinstance(parsed, dict) and parsed.get('success') is False:
            raise HyperdeckSetupError(
                f'{self.host}: {method} {path} refused: '
                f'{parsed.get("errorMessage") or parsed}', path=path)
        return parsed if isinstance(parsed, dict) else {'response': parsed}

    def _get(self, path: str) -> Dict[str, Any]:
        return self._call('GET', path).get('response', {}) or {}

    def _verify(self, what: str, wanted: Any, got: Any) -> None:
        if wanted != got:
            raise HyperdeckSetupError(
                f'{self.host}: wrote {what} = {wanted!r} but the deck reads {got!r} '
                f'— CHECK THIS DECK')

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def heartbeat(self) -> bool:
        """Is the configuration API answering? True, or raises."""
        return bool(self._call('GET', '/heartBeat').get('success', True))

    def capabilities(self) -> Dict[str, Any]:
        """``GET /capabilities``: which API sections this firmware has
        (``networkAccess``, ``hyperDeckEthernetProtocol``, ``certificate``…),
        each with its version. The map is the response object itself; its
        ``capabilities`` entry is this section's own version, not a nesting."""
        return self._get('/capabilities')

    def setup_basic(self) -> SetupInfo:
        r = self._get('/setupBasic')
        return SetupInfo(
            product_name=r.get('productName', ''),
            device_name=r.get('deviceName', ''),
            hostname=r.get('hostname', ''),
            software=r.get('software', ''),
            build=r.get('build', ''),
            hardware=r.get('hardware', ''),
            language=r.get('language', ''),
            product_id=r.get('productId', ''),
            short_product_name=r.get('shortProductName', ''),
        )

    def set_name(self, name: str) -> SetupInfo:
        """Set the deck's name (the one 9993 ``device info`` also reports).
        The deck derives its mDNS hostname from it, which invalidates a
        certificate issued for the old hostname."""
        self._call('PUT', '/setupBasic/name', {'name': name})
        info = self.setup_basic()
        self._verify('name', name, info.device_name)
        return info

    def languages(self) -> List[Dict[str, str]]:
        """``[{'code': 'en_US.UTF-8', 'name': 'English'}, …]``"""
        return list(self._get('/languages').get('languages') or [])

    def set_language(self, code: str) -> str:
        self._call('PUT', '/setupBasic/language', {'language': code})
        got = self.setup_basic().language
        self._verify('language', code, got)
        return got

    def identify(self, on: bool = True) -> None:
        """Flash the deck's front panel so someone can find it in a rack
        (``on=False`` stops it). The same button HyperDeck Setup has."""
        self._call('PUT' if on else 'DELETE', '/setupBasic/identify')

    def remote_admin(self) -> RemoteAdmin:
        """The "Configure via USB and Ethernet" switch. READ ONLY from the
        network by construction (see the module docstring)."""
        r = self._get('/remoteAdmin')
        return RemoteAdmin(enabled=bool(r.get('enabled', False)),
                           usb_request_source=bool(r.get('isUSBRequestSource', False)))

    # ------------------------------------------------------------------
    # Network interface
    # ------------------------------------------------------------------

    def network(self, interface_id: str = '0') -> NetworkInterface:
        return parse_interface(self._call('GET', f'/network/interface/{interface_id}')
                               .get('response', {}) or {})

    def set_network(self, *, address: Optional[str] = None, netmask: Optional[str] = None,
                    gateway: Optional[str] = None, dns: Optional[List[str]] = None,
                    dhcp: Optional[bool] = None, interface_id: str = '0',
                    readback_timeout: float = READBACK_TIMEOUT) -> NetworkInterface:
        """Set the interface and return it READ BACK once it is live.

        Fields left ``None`` keep their current values: the deck is read
        first and the whole config is written back with only the given
        fields changed (the API takes the full object). ``dns=[]`` clears the
        DNS list. ``dhcp=True`` switches the interface to DHCP, keeping the
        manual values on the deck for when it is switched back.

        If ``address`` changes, the read-back goes to the NEW address (the
        old one stops answering). The read-back is retried until the running
        config equals the configured one or ``readback_timeout`` passes, in
        which case this raises rather than report a success it cannot stand
        behind. Writes what it is told; the caller owns whether the change
        is safe — a wrong address or gateway strands the deck until someone
        walks to it.
        """
        if address is None and netmask is None and gateway is None and dns is None and dhcp is None:
            raise ValueError('nothing to set')
        before = self.network(interface_id)
        want_dhcp = before.dhcp if dhcp is None else dhcp
        body = {
            'id': before.id,
            'type': 'DHCP' if want_dhcp else 'Static',
            'address': before.address if address is None else address,
            'netmask': before.netmask if netmask is None else netmask,
            'gateway': before.gateway if gateway is None else gateway,
            'dnsServers': list(before.dns) if dns is None else list(dns),
        }
        self._call('PUT', f'/network/interface/{interface_id}', body)

        readback = self
        if not want_dhcp and address is not None and address != self.host:
            readback = HyperdeckSetup(address, timeout=self.timeout,
                                      scheme=self.scheme, opener=self._opener)
        deadline = time.monotonic() + readback_timeout
        last: Any = None
        while True:
            try:
                after = readback.network(interface_id)
                configured_ok = (
                    (after.dhcp is True) if want_dhcp else (
                        after.dhcp is False
                        and after.address == body['address']
                        and after.netmask == body['netmask']
                        and after.gateway == body['gateway']
                        and after.dns == body['dnsServers']))
                if configured_ok and after.settled:
                    return after
                last = (f'deck holds {after.address}/{after.netmask} gw {after.gateway} '
                        f'dns {after.dns} type {"DHCP" if after.dhcp else "Static"}, '
                        f'running {after.active_address}/{after.active_netmask}')
            except (HyperdeckSetupError, OSError) as exc:
                last = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(READBACK_POLL)
        raise HyperdeckSetupError(
            f'{readback.host}: the deck accepted the network change but did not '
            f'settle on it within {readback_timeout:g}s — CHECK THIS DECK ({last})')

    # ------------------------------------------------------------------
    # Network access: FTP, HTTP (Web Media Manager), the 9993 protocol
    # ------------------------------------------------------------------

    def network_access(self) -> Dict[str, str]:
        """``{'FTP': 'Enabled', 'HTTP': 'Enabled'}`` — protocol id to state."""
        protocols = self._get('/networkAccess').get('protocols') or []
        return {p.get('id', ''): p.get('state', '') for p in protocols if p.get('id')}

    def network_access_urls(self) -> Dict[str, str]:
        """Protocol id to the URL the deck advertises for it (``ftp://…``)."""
        protocols = self._get('/networkAccess').get('protocols') or []
        return {p['id']: p.get('url', '') for p in protocols if p.get('id')}

    def network_access_options(self) -> Dict[str, bool]:
        """Protocol id to whether it offers ``SecureOnly``. On the HD Mini:
        FTP False, HTTP True."""
        options = self._get('/networkAccess/options').get('options') or []
        return {o['protocol']: bool(o.get('secureOption', False))
                for o in options if o.get('protocol')}

    def set_network_access(self, **states: str) -> bool:
        """Set one or more protocols' access state; keyword per protocol,
        case-insensitive: ``set_network_access(ftp='Enabled', http='SecureOnly')``.

        Protocols not named keep their state (the API takes the full list, so
        the current one is read and merged). Returns whether the deck says a
        reboot is needed for the change to apply; when it does, the read-back
        is skipped because the old state is what it will read until then.
        Otherwise every named protocol is read back and a mismatch raises.
        """
        if not states:
            raise ValueError('nothing to set')
        current = self.network_access()
        wanted: Dict[str, str] = {}
        for key, state in states.items():
            match = [pid for pid in current if pid.lower() == key.lower()]
            if not match:
                raise ValueError(
                    f'{key!r} is not a protocol this deck offers ({sorted(current)})')
            wanted[match[0]] = _check_state(state)
        merged = {**current, **wanted}
        result = self._call('PUT', '/networkAccess', {
            'protocols': [{'id': pid, 'state': state} for pid, state in merged.items()]})
        reboot = bool((result.get('response') or result).get('rebootRequired', False))
        if reboot:
            logger.info('%s: network access change needs a reboot to apply', self.host)
            return True
        after = self.network_access()
        for pid, state in wanted.items():
            self._verify(f'networkAccess {pid}', state, after.get(pid))
        return False

    def ethernet_protocol(self) -> str:
        """State of the HyperDeck Ethernet Protocol (TCP 9993): ``'Enabled'``
        or ``'Disabled'``. Everything :class:`hyperdeckwire.Hyperdeck` does,
        and the ATEM's own HyperDeck control, needs it Enabled."""
        return self._get('/hyperDeckEthernetProtocol').get('state', '')

    def set_ethernet_protocol(self, state: str = ENABLED) -> str:
        _check_state(state)
        self._call('PUT', '/hyperDeckEthernetProtocol', {'state': state})
        got = self.ethernet_protocol()
        self._verify('hyperDeckEthernetProtocol', state, got)
        return got

    # ------------------------------------------------------------------
    # Certificate (for HTTPS / HTTP = SecureOnly)
    # ------------------------------------------------------------------

    def certificate_summary(self) -> Dict[str, Any]:
        """``hostname`` always; ``domain``, ``issuer``, validity fields when
        a certificate is installed (an empty summary means none is)."""
        return self._get('/certificate/summary')

    def create_self_signed_certificate(self) -> Dict[str, Any]:
        """Have the deck issue itself a certificate for its hostname."""
        self._call('PUT', '/certificate/selfSigned')
        return self.certificate_summary()

    def upload_certificate(self, certificate: str) -> Dict[str, Any]:
        """Install a certificate (the PEM text HyperDeck Setup's file picker
        would send). Returns the new summary."""
        self._call('POST', '/certificate', {'certificate': certificate})
        return self.certificate_summary()

    def delete_certificate(self) -> Dict[str, Any]:
        self._call('DELETE', '/certificate')
        return self.certificate_summary()

    def create_signing_request(self, *, common_name: str, country: str, state_name: str,
                               locality: str, organization: str,
                               subject_alternative_name: Optional[str] = None
                               ) -> Dict[str, Any]:
        """Ask the deck to generate a CSR for its key. Returns whatever the
        deck answers (the API documents the call, not the body); fetch the
        file with :meth:`download_signing_request`. Not exercised on
        hardware."""
        body = {'commonName': common_name, 'country': country, 'stateName': state_name,
                'locality': locality, 'organization': organization}
        if subject_alternative_name is not None:
            body['subjectAlternativeName'] = subject_alternative_name
        return self._call('POST', '/certificate/signingRequest', body)

    def download_signing_request(self, csr_id: str) -> bytes:
        """The ``.csr`` file for an id from :meth:`create_signing_request`."""
        return self._raw('GET', f'/certificate/signingRequest/{csr_id}')

    # ------------------------------------------------------------------
    # Users (Web Media Manager logins)
    # ------------------------------------------------------------------

    def admin_required(self) -> bool:
        """Does the deck demand admin credentials for protected endpoints?
        False on a fresh deck, which is also what anonymous FTP relies on."""
        return bool(self._get('/auth/adminStatus').get('adminRequired', False))

    def users(self) -> List[User]:
        return [User(auth_user_id=str(u.get('authUserId', '')),
                     username=u.get('username', ''),
                     remote_admin_utility_access=bool(u.get('remoteAdminUtilityAccess', False)),
                     shared_folder_permissions=list(u.get('sharedFolderPermissions') or []))
                for u in (self._get('/auth/users').get('users') or [])]

    def create_user(self, username: str, password: str, *,
                    remote_admin_utility_access: bool = False,
                    shared_folder_permissions: Optional[List[Dict[str, Any]]] = None) -> User:
        body: Dict[str, Any] = {'username': username, 'password': password,
                                'remoteAdminUtilityAccess': remote_admin_utility_access}
        if shared_folder_permissions is not None:
            body['sharedFolderPermissions'] = shared_folder_permissions
        self._call('POST', '/auth/user', body)
        for user in self.users():
            if user.username == username:
                return user
        raise HyperdeckSetupError(
            f'{self.host}: created user {username!r} but it is not in the user list — CHECK THIS DECK')

    def update_user(self, auth_user_id: str, *, username: Optional[str] = None,
                    password: Optional[str] = None,
                    remote_admin_utility_access: Optional[bool] = None,
                    shared_folder_permissions: Optional[List[Dict[str, Any]]] = None) -> User:
        body: Dict[str, Any] = {'authUserId': str(auth_user_id)}
        if username is not None:
            body['username'] = username
        if password is not None:
            body['password'] = password
        if remote_admin_utility_access is not None:
            body['remoteAdminUtilityAccess'] = remote_admin_utility_access
        if shared_folder_permissions is not None:
            body['sharedFolderPermissions'] = shared_folder_permissions
        if len(body) == 1:
            raise ValueError('nothing to update')
        self._call('PUT', '/auth/user', body)
        for user in self.users():
            if user.auth_user_id == str(auth_user_id):
                if username is not None:
                    self._verify('username', username, user.username)
                if remote_admin_utility_access is not None:
                    self._verify('remoteAdminUtilityAccess', remote_admin_utility_access,
                                 user.remote_admin_utility_access)
                return user
        raise HyperdeckSetupError(
            f'{self.host}: user {auth_user_id!r} is not in the user list after the update — CHECK THIS DECK')

    def delete_user(self, auth_user_id: str) -> None:
        self._call('DELETE', '/auth/user', {'authUserId': str(auth_user_id)})
        if any(u.auth_user_id == str(auth_user_id) for u in self.users()):
            raise HyperdeckSetupError(
                f'{self.host}: deleted user {auth_user_id!r} but it is still listed — CHECK THIS DECK')

    # ------------------------------------------------------------------
    # Date and time
    # ------------------------------------------------------------------

    def date_and_time(self) -> Dict[str, Any]:
        """``{'time': <unix seconds, int>, 'timezone_offset': <minutes>, 'time_friendly': 'YYYYMMDD-HHMMSS'}``"""
        r = self._get('/dateAndTime')
        return {'time': int(r.get('time', 0) or 0),
                'timezone_offset': int(r.get('timezoneOffset', 0) or 0),
                'time_friendly': r.get('timeFriendly', '')}

    def set_date_and_time(self, unix_seconds: int, timezone_offset_minutes: int = 0,
                          tolerance_seconds: int = 5) -> Dict[str, Any]:
        """Set the clock. Read back within ``tolerance_seconds`` (the deck
        keeps ticking between the write and the read)."""
        self._call('PUT', '/dateAndTime', {'time': str(int(unix_seconds)),
                                           'timezoneOffset': int(timezone_offset_minutes)})
        after = self.date_and_time()
        if abs(after['time'] - int(unix_seconds)) > tolerance_seconds:
            raise HyperdeckSetupError(
                f'{self.host}: set the time to {int(unix_seconds)} but the deck reads '
                f'{after["time"]} — CHECK THIS DECK')
        self._verify('timezoneOffset', int(timezone_offset_minutes), after['timezone_offset'])
        return after

    def ntp(self) -> Dict[str, Any]:
        """``{'enabled': bool, 'server_url': str, 'state': str}``"""
        r = self._get('/dateAndTime/ntp')
        return {'enabled': bool(r.get('enabled', False)),
                'server_url': r.get('serverUrl', ''),
                'state': r.get('state', '')}

    def set_ntp(self, server_url: str, enabled: bool = True) -> Dict[str, Any]:
        self._call('PUT', '/dateAndTime/ntp', {'serverUrl': server_url, 'enabled': bool(enabled)})
        after = self.ntp()
        self._verify('ntp serverUrl', server_url, after['server_url'])
        self._verify('ntp enabled', bool(enabled), after['enabled'])
        return after

    def timezone_offset(self) -> int:
        """Offset from GMT in minutes."""
        return int(self._get('/dateAndTime/timezone').get('timezoneOffset', 0) or 0)

    def set_timezone_offset(self, minutes: int) -> int:
        self._call('PUT', '/dateAndTime/timezone', {'timezoneOffset': int(minutes)})
        got = self.timezone_offset()
        self._verify('timezoneOffset', int(minutes), got)
        return got

    # ------------------------------------------------------------------
    # Reboot
    # ------------------------------------------------------------------

    def reboot(self) -> None:
        """Reboot the deck. The API hands out a one-time key on GET that the
        PUT must return, so a stray PUT cannot reboot a deck. No read-back:
        the deck goes away. Stops any playback."""
        key = self._get('/reboot').get('key')
        if not key:
            raise HyperdeckSetupError(f'{self.host}: the deck gave no reboot key')
        self._call('PUT', '/reboot', {'key': key})
