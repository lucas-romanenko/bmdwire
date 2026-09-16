# hyperdeckwire

Python library for Blackmagic **HyperDeck Studio** recorders: transport control,
clip listing and timeline editing over the HyperDeck Ethernet Protocol (TCP
9993), clip upload over the deck's built-in FTP server, and the deck's
configuration (network, the FTP / Web Media Manager / Ethernet Protocol
switches, certificate, users, time) over the API HyperDeck Setup drives.

[![CI](https://github.com/lucas-romanenko/bmdwire/actions/workflows/ci.yml/badge.svg)](https://github.com/lucas-romanenko/bmdwire/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/bmdwire.svg?label=pypi%20bmdwire)](https://pypi.org/project/bmdwire/) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)

hyperdeckwire is small and dependency-free. Playback and upload use the
`9993 + FTP` combination on purpose: the HTTP media/transport REST API that
arrived in firmware 8.x is only available on the Plus/Pro/HDR/Shuttle models,
while every networked HyperDeck, including the Studio HD Mini, offers these
two. The configuration API is a separate thing, on the Mini too, and is the
only place the network and access switches live.

## Status

- **1.0.0**, extracted from a broadcast control
  application where it drives decks in production: clip push, cue-and-loop
  as a switcher background source, and a transport modal for operators.
- Verified on a HyperDeck Studio HD Mini (firmware 8.1.1) through the full
  probe / clear / upload / cue-and-loop cycle, and on two HD Minis on 9.0.2
  for the configuration API. Other Studio HD models speak
  the same protocol but were not on the bench.

## Install

hyperdeckwire ships inside the `bmdwire` distribution together with the other three
libraries in this repository, at one version:

```sh
pip install bmdwire
```

Pin the version in a requirements file (`bmdwire==1.2.0`) so a later release cannot change your install under you. To install straight from a GitHub tag instead (git needed on the machine):

```sh
pip install "bmdwire @ git+https://github.com/lucas-romanenko/bmdwire.git@v1.2.0"
```

Python 3.10 or newer. No other dependencies.

## Features

- Blocking, single-socket `Hyperdeck` client with an explicit connect/close lifecycle and context-manager support.
- Read commands: `device_info`, `remote_info`, `slot_info`, `transport_info`, `configuration`, `disk_list`, `clips_get`, `clips_count`.
- Write commands: `play` (loop, single clip, speed, clip id), `pause`, `stop`, `goto_clip`, `clips_add`, `clips_remove`, `clips_clear`, `slot_select`, `remote_enable`, `set_configuration`, `ping`.
- Typed `Clip` and `Response` dataclasses; protocol errors raise `HyperdeckError` with the deck's code and text.
- Asynchronous 5xx notifications are filtered out of blocking requests and can be read explicitly.
- `upload_clip` FTP helper with storage-volume auto-detection, anonymous-login fallback, progress callback and throughput reporting.
- `HyperdeckSetup`: the configuration API on the deck's HTTP port. Name, network interface (address, netmask, gateway, DNS, DHCP), the FTP / Web Media Manager / HyperDeck Ethernet Protocol switches, certificate, users, date and time, NTP, identify, reboot. Every setter reads the setting back before reporting success; the "Configure via USB and Ethernet" switch is read-only from the network by construction.
- Pure standard library; a `socket_factory` hook, `ftplib` monkeypatching and an `opener` hook make the whole suite runnable without hardware.

## Usage

```python
from hyperdeckwire import Hyperdeck, HyperdeckError, upload_clip

# Push a clip onto the deck's active storage volume.
result = upload_clip('192.0.2.11', '/local/path/intro.mp4')
print(f'{result.name}: {result.throughput_mb_s:.1f} MB/s into slot {result.slot_dir}')

# Cue it and loop it.
with Hyperdeck('192.0.2.11') as hd:
    print(hd.model, hd.protocol_version)
    for clip in hd.disk_list():
        print(clip.clip_id, clip.name, clip.duration)
    hd.stop()
    hd.clips_clear()
    try:
        hd.clips_add('intro.mp4')
    except HyperdeckError as e:
        raise SystemExit(f'deck refused the clip: {e}')
    hd.play(loop=True, single_clip=True)

# Make sure a deck in a rack is set up for the above: 9993 on, FTP on.
from hyperdeckwire import HyperdeckSetup
deck = HyperdeckSetup('192.0.2.11')
if deck.ethernet_protocol() != 'Enabled':
    deck.set_ethernet_protocol('Enabled')
if deck.network_access()['FTP'] != 'Enabled':
    deck.set_network_access(ftp='Enabled')
```

The full command reference, dataclass fields and error-code table are in
[docs/API.md](docs/API.md).

## Protocol notes

The Ethernet Protocol itself is documented by Blackmagic in
`HyperDeckEthernetProtocol.pdf` (December 2024 revision). The points below
are behaviour the library relies on that the document does not spell out, or
that was established on hardware.

- **No REST API on the Studio HD Mini.** Port 80 is closed on firmware 8.1.1, so the library never depends on HTTP.
- **The greeting is a 5xx.** On connect the deck sends `500 connection info:` as a multi-line block. It shares the code range of asynchronous notifications but is synchronous and arrives once; the client reads it eagerly and caches `model` and `protocol version` from it.
- **Multi-line framing.** A response is multi-line if and only if its head line ends with a colon; the body is then read until a blank line. Async notifications can interleave with a pending response and are skipped inside `request()` by default.
- **There is no pause verb.** `pause()` sends `play: speed: 0`, which freezes on the current frame. `stop` also holds the last frame under the factory `stop mode: lastframe` setting; the transport reports `stopped` in both cases.
- **`213 deck rebooting` is a success.** A `file format` change may answer with 213 instead of `200 ok` and drop the connection. The client treats both as success and leaves reconnecting to the caller.
- **FTP volume layout.** Storage volumes are top-level directories. The Studio HD Mini names them by slot number (`/1/`, `/2/`); other models name them by medium (`sd1`, `ssd1`, `usb`, `nas`). `STOR` at the root is refused with `550`, so `upload_clip` lists the root, picks a volume (numeric first, then SD, SSD, USB, NAS) and changes into it. `System Volume Information` and `.Trashes` are never selected.
- **FTP login.** Stock firmware accepts an empty anonymous login; some servers reject the bare `USER` form, so the helper retries as `anonymous` before failing.
- **The disk index updates live.** A clip is visible to `disk list` immediately after its `STOR` completes; no rescan or slot reselect is needed.
- **Clip names contain spaces.** `disk list` and `clips get` rows are tokenised from the right (duration, format fields) and everything left over is the name, which is why a name such as `Intro Loop animation.mp4` round-trips.
- **Client limit.** Beyond a small number of simultaneous 9993 clients the deck answers `120 connection failed` and closes the socket.
- **Verified hardware.** HyperDeck Studio HD Mini, firmware 8.1.1, full probe / clear / upload / cue-and-loop cycle. Other Studio HD models speak the same protocol but were not on the bench.

## Development

```sh
git clone https://github.com/lucas-romanenko/bmdwire.git
cd bmdwire
pip install -e ".[test]"
python -m pytest hyperdeckwire/tests      # or plain `python -m pytest` for all four suites
```

The suite needs no hardware. CI runs all four suites on Python 3.10, 3.12 and 3.14 for every push and pull request.

## Related libraries

The same `pip install bmdwire` carries one library per Blackmagic device family, same shape, same author, all pure standard library except atemwire's small C extension:

- [atemwire](../atemwire/): ATEM switchers (UDP protocol, macros, profiles)
- [ultimattewire](../ultimattewire/): Ultimatte keyers (archive and restore)
- [videohubwire](../videohubwire/): Videohub routers (routing, labels)

## License

MIT. See `LICENSE`.
