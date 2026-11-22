# Contributing

Thanks for looking. A few rules keep this small and dependable.

- **Standard library only.** Every script runs on a stock Python 3.11+ on macOS and Windows (and the tests on Linux). No packages.
- **Flat scripts.** Everything in `switchboard/` is a script you can read top to bottom and run by itself. They load
  each other by file name, so keep new files in that folder.
- **Tests first for behaviour changes.** `switchboard/discord-bridge-test.py` runs the bridge and the runner against a
  fake Discord and a temporary root: no token, no network, nothing live. Run one test by name substring while you work
  (`python3 switchboard/discord-bridge-test.py archived`) and the whole file before a pull request. Also run
  `python3 switchboard/concurrency-test.py`, `python3 switchboard/route-test.py` and `python -m pytest tests -q`.
- **Nothing machine-specific.** No names, hostnames, home paths, ids or tokens, in code, comments or fixtures.
  `python3 tools/check-clean.py` must pass; CI runs it.
- **Say why.** A comment explains the reason a rule exists, not the history of who asked for it.
- **Safer defaults.** A new capability that runs things unattended, spends money or deletes anything is off until the
  owner turns it on.
