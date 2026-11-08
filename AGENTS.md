# Instructions for coding agents

You may be asked to deploy or operate seestar-commander for someone. Ground rules:

1. **Nothing site-specific goes into this repo.** Hostnames, IPs, coordinates, credentials and cable limits live in `config.toml` (git-ignored) or the operator's private repo.
2. **Start read-only.** Copy `config.example.toml` to `config.toml`, run `tools/probe.py`, confirm the device list, and **ask the operator for the cable window** (home hour angle, CCW/CW limits) before any motion.
3. **Never move the RA axis with firmware gotos.** Use `scope.move_hadec` / `tools/goto.py`, which keep the persisted unwrapped hour angle (`data/ra_unwrap.json`) inside the window. **Only one host and one process may move the mount**, because it owns `ra_unwrap.json`: while the sequencer service (`python -m sequencer serve`) runs, go through its API, not the tools/ scripts.
4. **The first motion is a small one** while a human or a camera watches. Confirm which direction positive `MoveAxis` turns.
5. **Roof/enclosure moves are hazardous.** Close it only with the SeeStar parked and the operator's go. Report roof state from the device, not from assumptions.
6. **Long runs go in the background** (nohup or a service) with progress in `data/…/status.json`. Don't block on them.
7. Read [docs/seestar-alpaca-notes.md](docs/seestar-alpaca-notes.md) for device quirks before writing new motion or camera code.
8. **Develop against the simulator** (`python -m sequencer serve --sim`, `pytest tests`). It keeps its own state in `data/sim/`.
