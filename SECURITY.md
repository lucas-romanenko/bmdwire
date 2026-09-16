# Security policy

## Reporting

Use GitHub's private vulnerability reporting: **Security → Report a
vulnerability**, on this repository. It is enabled. Please do not open a
public issue for a vulnerability, so there is a window to fix and release
before it is exploitable against anyone running it.

Say which of the four packages is affected, since they are independent
distributions with their own versions.

## What these libraries are

Four clients that speak to broadcast hardware on a local network:

| Package | Protocol |
|---|---|
| atemwire | ATEM switchers, native UDP on 9910, plus a C extension for pixel conversion |
| hyperdeckwire | HyperDeck recorders, a line protocol on TCP 9993, and clip upload over FTP |
| ultimattewire | Ultimatte keyers, a block protocol on TCP 9998 |
| videohubwire | Videohub routers, a line protocol on TCP 9990 |

**None of these protocols authenticate.** That is the hardware, not these
libraries. A device on the network accepts commands from anything that can
reach it, with or without this code in the picture. Put the devices on a
network you trust.

## What is worth reporting

These parse bytes that arrive over a network, so the interesting reports are
about what happens when those bytes are hostile or malformed:

- A crash, hang or unbounded allocation in a parser, given a crafted packet
  or response. atemwire's `messages` DSL and `state` assembler, and the line
  and block parsers in the other three, are the surface.
- Memory safety in `atemwire.mediaconvert`, the C extension. It converts
  pixel data and does RLE encoding, so it is the one place a bad length or
  size can be more than an exception.
- Path handling in `atemwire.profile` when restoring a profile, and in
  `hyperdeckwire.upload` when naming a clip on a deck.
- Anything that lets a device's response make a caller write outside the
  path it asked for.

Profile XML parsing is already hardened against entity expansion, including
the wide-encoding bypass where a UTF-16 DTD survives a text check and expat
revives it. If you find a way past that, it is very much worth a report.

## What is not a vulnerability here

- The absence of authentication in the device protocols. See above.
- A library accepting an IP address from its caller and connecting to it.
  These are clients; connecting where they are told is the job. Validating
  caller input belongs in the application.
- A malformed response from a device causing an exception that the caller is
  expected to handle. An exception is the contract; a hang or an unbounded
  allocation is not.
