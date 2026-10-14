# bmdwire: notes for future sessions

bmdwire is the Python wire layer for Blackmagic devices. bmdctl
(`../bmdctl`, `github.com/lucas-romanenko/bmdctl`) is its Go port, a CLI.
`CONTRIBUTING.md` says how to change the library; this file says how to
keep the port in step with it.

## bmdwire first

bmdctl follows bmdwire; it never leads it.

- Protocol knowledge lands here first. A packet layout, a field, a quirk
  or a fix, wherever it was found (on hardware, in a capture from ATEM
  Software Control, while porting), is a bmdwire PR with its tests
  before bmdctl has it.
- Each package's `docs/SURFACE.md` is the source of truth for what it
  does. A PR that changes behaviour updates it in the same PR, marking a
  fix in §5.4 with its commit as the existing ones are.
  `atemwire/docs/SURFACE.md` exists; hyperdeckwire has none yet. When it
  gets one, put it at `hyperdeckwire/docs/SURFACE.md`, where bmdctl's
  drift check looks.
- bmdctl ports from SURFACE.md and cites the bmdwire PR. It never gets a
  command or packet that bmdwire doesn't have, so a new one starts here.
- On a release, once it is on PyPI, `publish.yml`'s `port-issue` job
  opens "Port bmdwire v<version>" in bmdctl with the release notes; it
  needs the repository secret `BMDCTL_ISSUE_TOKEN`. Make the GitHub
  release within 30 minutes of the publish finishing, or re-run that job
  once it exists. bmdctl also compares its SURFACE copies with main here
  every night and keeps an issue open while they differ.
