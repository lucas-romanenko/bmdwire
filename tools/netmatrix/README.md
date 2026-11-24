# The network matrix

Every network path atemwire uses, through every kind of network, against a
fake switcher, with the data checked. A passing test on a fast LAN proves
nothing about a VPN, a slow site or a lossy Wi-Fi link; this is where those
get tried before a release, offline.

```bash
pip install -e ".[test]"
python tools/netmatrix/netmatrix.py --list
python tools/netmatrix/netmatrix.py                           # all of it, 3 runs each (about 2 h)
python tools/netmatrix/netmatrix.py --profiles vpn,mtu576 --paths still_up,macro_up
python tools/netmatrix/netmatrix.py --quick --runs 1          # the CI subset
python -m pytest tools/test_netmatrix.py                      # the same, as tests
```

One line per run, then a table of seconds and pass/fail. `--out x.jsonl`
keeps the runs, `--log x.log` the client's debug log.

## The pieces

- `fakeswitcher.py`: a fake ATEM modelled on captures of Constellation HD and
  4K Plus units: in-order-only reception, its own resends every 80 ms, ACKs
  every 16 packets or 10 ms, a Time packet every 40 ms, a state dump in
  1,394-byte packets, five sessions, store locks, upload budgets of 768
  chunks, downloads with a 150-chunk window, macros, reboots. Its docstring
  says what each behaviour is based on. Its state is `initial-dump.bin`, a
  1 M/E Constellation HD's (bmdctl's synthetic dump, every command checked
  against atemwire's parsers), and its macro `macro-sample.bin` (bmdctl's
  fixtures, from a Constellation HD).
- `netlink.py`: the network, a UDP relay in the fake's process. Each way:
  delay, jitter (in order, or reordering), loss, a rate with a short queue,
  a largest datagram (bigger ones dropped silently, as on a VPN), and a
  cable to pull.
- `netmatrix.py`: the paths and the profiles.

## Paths

| path | what it does | checked |
|---|---|---|
| connect | hello to complete state dump | dump complete |
| commands | 20 program cuts, one after another | each confirmed by the switcher |
| labels | 20 input labels | read back |
| multiview | 8 multiview windows | read back |
| still_up | a noisy 1080p still (8.3 MB on the wire) | MD5 at the switcher |
| still_down | the same, downloaded | identical to what went up |
| macro_up | a 4 KB macro | read back identical, named |
| macro_fail_cleanup | a macro upload into an empty slot that can't finish | slot empty again |
| profile_save | `ATEM(ip)` and `Profile.from_atem`, as WebATEM's save | XML has the labels and the macro's ops |
| profile_restore | a saved profile, labels changed, macro moved to an empty slot, applied | the switcher has both |
| cable_pull | cable out 3 s mid-upload | finishes, MD5 at the switcher |
| idle_cut | cable out 8 s on an idle connection | state back |
| reboot | switcher reboots mid-upload | reconnects, next upload OK |
| two_clients | two clients upload at once | both stills arrive |
| disconnect | 5 connects and goodbyes | no session left behind |

## Profiles

Round trip = twice the one-way delay.

| profile | each way |
|---|---|
| direct | no relay (the fastest the machine goes) |
| lan | 0.2 ms |
| rtt80, rtt200 | 40, 100 ms |
| loss0.5, loss2, loss5 | 20 ms, that loss |
| jitter20 | 40 ms ± 20 ms, in order |
| reorder | 40 ms ± 10 ms, packets overtake |
| bw1, bw5, bw10 | 20 ms, 1/5/10 Mbit/s, a 30-60 packet queue |
| mtu1400 ... mtu576 | 20 ms; towards the switcher, datagrams over that path MTU dropped |
| mtu1280, mtu1280v6 | a ZeroTier-like VPN (interface MTU 1,280) carrying IPv4 or IPv6 |
| vpn | 40 ms, 1 % loss, path MTU 1,200 towards the switcher |
| slowsite | 30 ms, 1 % loss, 3 Mbit/s, a 40-packet queue |
| busyhost | 0.25 ms ± 0.35, a busy thread in the client's process |

The relay is Python: on the fast profiles its own speed is part of the
seconds, so compare a change with what it replaces on the same machine, not
with a real LAN. Real hardware is the bench's job.
