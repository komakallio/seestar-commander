# seestar-commander

Remote control of a **ZWO SeeStar S30 Pro on an equatorial wedge** over its built-in **ASCOM Alpaca** server, in plain Python. No NINA, no SeeStar app in the loop.

> **Status: early and experimental (Nov 2026).** It works on one observatory pier and is being turned into a proper service. Expect rough edges. Read [Safety](#safety) before moving anything.

## What it does today

- **Cable-safe mount motion.** The RA axis moves only by closed-loop `MoveAxis`, inside a configured window around the home hour angle. Its unwrapped angle is persisted, so it survives restarts. **The firmware's own gotos (`SlewToCoordinates`) can take the long way round and wind the USB cable**, so they're avoided.
- **Imaging:** wide-angle and telephoto cameras, filter wheel (including the `Dark` position for dark frames), focuser, dew heater.
- **Pointing:** plate solving, then sync, then centring.
- **Calibration and stacking:** frame QA (star size, elongation), drift measurement between frames, darks, a simple dark-subtracted stack.
- **Roof:** optional close through an ASCOM Alpaca **Dome** device (roll-off roof), with an optional IP camera watching the scope.
- **Horizon feature:** capture a 360° panorama with the wide camera, plate-solve it, stitch it, extract the horizon line, edit it in the browser, and export a **Stellarium landscape** (zip) and a **NINA `.hrz`** horizon file. See [docs/horizon.md](docs/horizon.md).

## Planned

A long-running service with a control API, a mobile web UI, plan-file sequencing with horizon and twilight constraints, autofocus, twilight flats, safety handling (rain → stop, park, close), a cable-safe **Alpaca Telescope proxy** (so any ASCOM client gets cable safety), a **polar-alignment tool** that guides you to move a star onto a reticle, and running the horizon workflow from the browser. See [docs/design.md](docs/design.md).

## Requirements

- SeeStar S30 Pro with Alpaca enabled in the SeeStar app (firmware ≥ 9.75 needs it switched on). Other SeeStar models are untested; the plain S30 has no wide camera over Alpaca.
- An equatorial wedge, with the app's EQ mode and polar alignment done. Alt-az use isn't covered yet.
- Python ≥ 3.11 on a host on the same LAN as the scope. Image downloads are about 16 MB per frame, so avoid VPN and WiFi hops for imaging.
- Optional:
  - an RTSP camera that sees the scope;
  - an Alpaca Dome for the roof;
  - a wide-field plate solver for the wide camera (currently the author's `projections` solver, private; an open replacement is on the to-do list);
  - the `astrometry` Python package plus astrometry.net index series 4100 (about 170 MB) for telephoto solving.

## Quick start

```bash
git clone https://github.com/komakallio/seestar-commander && cd seestar-commander
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml   # then edit: scope address, site, cable limits
.venv/bin/python tools/probe.py      # read-only: lists Alpaca devices and capabilities
```

Then read [docs/seestar-alpaca-notes.md](docs/seestar-alpaca-notes.md) **before** your first move.

## Safety

- **Measure your cable window first.** Note the RA-axis hour angle at home (`tools/probe.py`; the mount reads Dec −90 there) and how far the RA axis may turn each way. Put those numbers in `[mount]`. The defaults are one wedge's values, not yours.
- **Never use firmware gotos for big RA moves.** Use `tools/goto.py` / `scope.move_hadec`, which enforce the window.
- **Parking:** first bring RA to the home angle with axis moves, then call the firmware `Park` under the RA guard (`tools/park_guarded.py`).
- **Roof:** closing a shared roll-off roof with no position interlock can hit telescopes. Confirm **every** scope under the roof is parked before closing.

## Layout

| Path | What |
|---|---|
| `tools/` | scripts and modules: `scope.py` (devices, cable-safe motion), `config.py`, panorama/horizon pipeline, `poc_m31.py` (first imaging run) |
| `web/` | `monitor.html` (allsky capture monitor), `horizon_editor.html` |
| `docs/` | design, SeeStar Alpaca notes, horizon workflow, grid optimisation |
| `config.toml` | **your** site config (git-ignored) |
| `data/` | frames, state and products (git-ignored) |

For agents deploying this: see [AGENTS.md](AGENTS.md).

## License

MIT, see [LICENSE](LICENSE).
