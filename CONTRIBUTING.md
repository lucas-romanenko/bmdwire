# Contributing

Pull requests are welcome. These four libraries exist so that other people
can build on the wire layer, which only works if other people can also
correct it.

## Which package

One distribution, `bmdwire`, four import packages, each in its own
directory at the root of the repository:

| You want to change | Package |
|---|---|
| An ATEM command, field or reader | `atemwire` |
| HyperDeck transport, clips, upload | `hyperdeckwire` |
| Ultimatte settings, archive, network | `ultimattewire` |
| Videohub routing or labels | `videohubwire` |

Each directory carries its own licence and its own `tests/`; the version is
one number for all of them, in `pyproject.toml` at the root. A change
spanning two device families is one pull request and one release.

## Adding an ATEM command

The whole library-side change happens in one file, the per-feature module
under `atemwire/messages/`. Wire format, operation wrapper and
mixerstate reader live together there:

1. Declare the wire format as a `Send` or `Recv` subclass using the field
   types (`u8`, `u16`, `boolean`, `string`, `mask_bit=`). `Recv` classes are
   registered automatically.
2. Add the operation wrapper below it, taking the connection as its first
   argument. Convert operator units to wire units inside the function.
3. Add a reader if incoming state carries the value.
4. Add a test in that package's `tests/`. A fake connection that captures
   the emitted bytes is the usual shape; no hardware needed.

Do not hand-roll `struct.pack`. The declarative format is what makes the
docstring offset tables checkable, and a test checks them.

## Hardware you have and I do not

These were developed against 1 M/E Constellation HD switchers, HD Mini
HyperDecks and the Ultimatte and Videohub models in one facility. Anything
else is untested.

If a command behaves differently on your hardware, that is a finding worth
an issue even without a fix. If you send a fix, say in the pull request what
you ran it against and what you saw. For a device I do not own, that report
is the only verification either of us gets.

Wire formats are easy to get self-consistently wrong: a field can round trip
through our own reader and still not be what the hardware means. Where you
can, cross-check against what the vendor's own software sends.

## Running the tests

```bash
pip install -e ".[test]"     # atemwire builds a small C extension, so it needs a compiler
pytest -q                    # all four suites, from the repository root
```

No hardware required. Continuous integration runs the suite on Python
3.10, 3.12 and 3.14 for every push and pull request.

## Licensing, which differs by package

- **atemwire is LGPL-3.0-only.** It is a fork of pyatem by Martijn Braam and
  the OpenAtem contributors, and contributions to it are released under the
  same licence. See `atemwire/NOTICE` for the fork point.
- **hyperdeckwire, ultimattewire and videohubwire are MIT**, and
  contributions to them are released under MIT.

By opening a pull request you agree your contribution goes out under the
licence of the package you touched.
