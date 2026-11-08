# seestar-commander

Remote control of a **ZWO SeeStar S30 Pro on an equatorial wedge** over its built-in **ASCOM Alpaca** server, in plain Python. No NINA, no SeeStar app in the loop.

> **Status: early and experimental (Nov 2026).** It works on one setup and is being turned into a proper service. Expect rough edges. Read [Safety](#safety) before moving anything.

## What it does today

- **Cable-safe mount motion.** The RA axis moves only by closed-loop `MoveAxis`, inside a configured window around the home hour angle. Its unwrapped angle is persisted, so it survives restarts. **The firmware's own gotos (`SlewToCoordinates`) can take the long way round and wind the USB cable**, so they're avoided.
- **Imaging:** wide-angle and telephoto cameras, filter wheel (including the `Dark` position for dark frames), focuser, dew heater.
- **Pointing:** plate solving, then sync, then centring.
- **Sequencer v1 (supervised):** one service that owns the devices and runs plan files (targets, exposures, altitude/horizon/Sun/end-of-night constraints), with a JSON control API and a phone-friendly status page. See [Sequencer](#sequencer-v1-supervised).
- **Calibration and stacking:** frame QA (star count, HFR, eccentricity), drift between frames, a **dark library** (master darks matched by exposure, gain, offset, binning and sensor temperature, with expiry), and a running dark-subtracted, registered, sigma-clipped stack.
- **Roof and safety hooks (optional):** close through an ASCOM Alpaca **Dome** or simple HTTP open/close/state URLs; poll an HTTP JSON safety endpoint (`tools/safety.py`, unreachable for too long = unsafe); call a URL after a verified park. An optional IP camera can watch the scope.
- **Horizon feature:** capture a 360° panorama with the wide camera, plate-solve it, stitch it, extract the horizon line, edit it in the browser, and export a **Stellarium landscape** (zip) and a **NINA `.hrz`** horizon file. See [docs/horizon.md](docs/horizon.md).

## Planned

Unattended operation (automatic roof close, scheduling, waiting for targets to rise), dithering, autofocus, twilight flats, a cable-safe **Alpaca Telescope proxy** (so any ASCOM client gets cable safety), a **polar-alignment tool** that guides you to move a star onto a reticle, and running the horizon workflow from the browser. See [docs/design.md](docs/design.md).

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

## Sequencer v1 (supervised)

One long-running process owns the devices. Everything else (the CLI, the status page, an agent) talks to its HTTP API. **Only one process may move the mount**: don't run `tools/goto.py` and friends while the service runs.

**Try it on the simulator first.** Nothing real moves; the simulator has its own state under `data/sim/`:

```bash
.venv/bin/pip install pytest pyyaml          # tests; PyYAML only for YAML plans
.venv/bin/python -m pytest -q tests          # everything runs against the simulator
.venv/bin/python -m sequencer serve --sim --speed 20     # open http://127.0.0.1:8640/
.venv/bin/python -m sequencer check plan.example.toml    # offline: constraints right now
.venv/bin/python -m sequencer darks build --plan plan.example.toml --count 5
.venv/bin/python -m sequencer run plan.example.toml
.venv/bin/python -m sequencer status
.venv/bin/python -m sequencer sim-safety unsafe RAIN      # watch it abort, park and request the roof
.venv/bin/python -m sequencer roof close                  # operator confirms
.venv/bin/python -m sequencer sim-safety safe && .venv/bin/python -m sequencer resume
```

`--sim` without a `config.toml` uses `config.example.toml`. It starts at the current time if it's dark, otherwise at tonight's astronomical dark (`--sim-start 2026-11-08T20:00:00Z` to choose). `--sim-no-solver` simulates a missing plate solver.

**Against the real scope** (after [Safety](#safety) and the cable-window measurement):

1. Fill in `config.toml`: `[mount]` cable window, `[sequencer]` (bind `host = "0.0.0.0"` and set `api_token` if the phone is on the LAN), `[darks]`, optional `[roof]`, `[safety]` and `[hooks]`.
2. Optional telephoto solving: `pip install astrometry`; the 4100-series index files (about 170 MB) download into `data/astrometry_cache` on the first solve. Without it, centring falls back to mount pointing (or `fallback = "blob"` for bright extended targets).
3. `python -m sequencer serve` on the host next to the scope (nohup or a systemd unit), and the CLI from anywhere with `--url http://host:8640 --token …`.
4. Start with a short plan while you watch (a camera on the scope helps), and keep `park` within reach.

**What one target does:** cable-safe move (`scope.move_hadec`, never a firmware goto) → tracking on → centre (telephoto plate solve, sync in JNow, re-centre; fallback when no solver) → lights with per-frame QA and drift → FITS with headers (`DARKUSED` names the master dark). Before every frame it checks the constraints and the cable window against the coming tracking time, and stops the target before the limit. Frames go to `data/sessions/<night>/<target>/lights/`, the running stack to `…/<target>/stack.fits`.

**Safety:** the service polls `tools/safety.py` (`[safety] url`). On unsafe it aborts the exposure, does the guarded park (RA to home by axis moves, then firmware Park under the RA guard), posts `on_parked`, and if a roof is configured raises **roof close requested**. v1 never closes the roof by itself: the operator runs `roof close`, which is refused unless the scope reads AtPark. Nothing restarts when it's safe again until `resume`.

**Dark library:** masters live in `data/darklib/`, keyed by camera, exposure, gain, offset, binning and a 2 °C sensor-temperature bin (the S30 Pro is uncooled; the telephoto reports `CCDTemperature`). Entries expire after 60 days. The status page lists entries (fresh / expiring / expired) and what the active plan (and any `[darks] plans`) is **missing** at the current sensor temperature; `darks build --missing` fills it with the wheel on `Dark` (fine with the roof closed). Lights without a matching master are stacked uncalibrated and flagged.

| Command | API | |
|---|---|---|
| `status` | `GET /api/status` | everything the page shows |
| `run PLAN` | `POST /api/plan` | start a plan |
| `stop` / `resume` | `POST /api/stop`, `/api/resume` | stop aborts the exposure, doesn't park |
| `park` | `POST /api/park` | guarded park + `on_parked` |
| `roof close` | `POST /api/roof/close` | only when parked |
| `safety` | `GET /api/safety` | last poll |
| `darks list` / `darks build` | `GET /api/darks`, `POST /api/darks/build` | dark library |

## Safety

- **Measure your cable window first.** Note the RA-axis hour angle at home (`tools/probe.py`; the mount reads Dec −90 there) and how far the RA axis may turn each way. Put those numbers in `[mount]`. The defaults are one wedge's values, not yours.
- **Never use firmware gotos for big RA moves.** Use `tools/goto.py` / `scope.move_hadec`, which enforce the window.
- **Parking:** first bring RA to the home angle with axis moves, then call the firmware `Park` under the RA guard (`tools/park_guarded.py`).
- **Roof:** park the SeeStar before closing a roof or enclosure; most have no telescope-position interlock.

## Layout

| Path | What |
|---|---|
| `sequencer/` | the v1 service: plan files and constraints, simulator, dark library, QA/stacking, HTTP API, CLI (`python -m sequencer`) |
| `tools/` | scripts and modules: `scope.py` (devices, cable-safe motion, guarded park), `config.py`, `safety.py`, panorama/horizon pipeline, `poc_m31.py` (first imaging run) |
| `tests/` | pytest suite, all against the simulator |
| `web/` | `sequencer.html` (status page, served by the service), `monitor.html` (allsky capture monitor), `horizon_editor.html` |
| `plan.example.toml` | example plan file |
| `docs/` | design, SeeStar Alpaca notes, horizon workflow, grid optimisation |
| `config.toml` | **your** site config (git-ignored) |
| `data/` | frames, state and products (git-ignored) |

For agents deploying this: see [AGENTS.md](AGENTS.md).

## License

MIT, see [LICENSE](LICENSE).
