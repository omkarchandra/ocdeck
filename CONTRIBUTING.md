# Contributing

Thanks for looking. OC Deck is maintained by one person in spare time, so a
short note on what helps most:

- **Bug reports are very welcome.** Use the issue form; the OC Deck version
  (`pip show ocdeck`), your OS and desktop, which harnesses you use, and what you
  expected versus what happened are usually enough. If a key does the wrong
  thing, say which view you were in.
- **Small, focused pull requests** are easiest to review: one fix or feature, with a
  test. Please open an issue first for anything large, so we agree on the idea
  before you spend time on it.
- **Security issues:** please do not open a public issue; see [SECURITY.md](SECURITY.md).

## Running the tests

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q tests          # about ten minutes
node --test plugins/test_permission_notify.mjs desktop/tests/*.mjs tests/*.mjs
python -m pytest -q desktop/tests agent-browser
```

The tests use fakes for tmux, systemd and the OpenCode API, so they do not touch
your real sessions or servers.

Screenshots in the README come from `scripts/make_screenshots.py`, which renders
synthetic demo data. Regenerate them with `python scripts/make_screenshots.py --png`
(needs `inkscape`); never screenshot a real session.

## Conventions

- The files behind the Super+O hotkey (`ocdeck_hotkey.py`,
  `src/ocdeck/focus_helper.py`, `src/ocdeck/ptyxis_tabs.py`) stay stdlib/`gi`-only;
  a test enforces it. That keeps the shortcut working even when the project's
  virtualenv is broken.
- OC Deck reads files and APIs it does not control. New readers should bound
  sizes, validate shape and fail closed, like the existing ones.
- Anything that can approve, start or widen an agent needs a confirmation step
  and a test for the refusal path.
- Comments and tests cite decision numbers (`C104`, `P0-4`) and detection-rule IDs
  (`S1`, `S7`). They come from the design record of the private workstation this
  project grew out of, which is not published. The code and its tests are the
  specification; the labels are only cross-references.
