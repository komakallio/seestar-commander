# seestar-commander

Remote control of a **ZWO SeeStar S30 Pro on an equatorial wedge** over its built-in **ASCOM Alpaca** server, in plain Python. No NINA, no SeeStar app in the loop.

> **Status: early and experimental (Nov 2026).** It works on one setup and is being turned into a proper service. Expect rough edges. Read [Safety](#safety) before moving anything.

## What it does today

- **Cable-safe mount motion.** The RA axis moves only by closed-loop `MoveAxis`, inside a configured window around the home hour angle. Its unwrapped angle is persisted, so it survives restarts. **The firmware's own gotos (`SlewToCoordinates`) can take the long way round and wind the USB cable**, so they're avoided.
- **Imaging:** wide-angle and telephoto cameras, filter wheel (including the `Dark` position for dark frames), focuser, dew heater.
- **Pointing:** plate solving, then sync, then centring. A **pointing memory** logs every solve and offsets each target's first slew by the remembered error (constant offset, then a fitted index + polar-axis model), because the firmware forgets its sync when it parks. See [Pointing memory](#pointing-memory).
- **Exclusive use:** a lock with a TTL hands the scope to a script (`python -m sequencer lock`, `tools/scopelock.py`); the service refuses its own jobs meanwhile. See [Scope lock](#scope-lock).
- **Sequencer v1 (supervised):** one service that owns the devices and runs plan files (targets, exposures, altitude/horizon/Sun/end-of-night constraints), with a JSON control API and a phone-friendly status page. See [Sequencer](#sequencer-v1-supervised).
- **Dithering:** small random offsets (default up to 15 telephoto px) around the centred position between lights, made with timed, cable-checked MoveAxis pulses with tracking on; the achieved offsets are measured from the frames' registration and logged in the frame log and FITS headers (`DITHERX`/`DITHERY`), with an optional plate-solve re-centre every N dithers. See [Dithering](docs/design.md#dithering).
- **Calibration and stacking:** frame QA (star count, HFR, eccentricity), drift between frames, a **dark library** (master darks matched by exposure, gain, offset, binning and sensor temperature, with expiry), and a running dark-subtracted, registered, sigma-clipped stack.
- **Roof and safety hooks (optional):** open and close through an ASCOM Alpaca **Dome** or simple HTTP open/close/state URLs (plans can open at start and close at end, see [Roof](#roof-optional)); poll an HTTP JSON safety endpoint (`tools/safety.py`, unreachable for too long = unsafe); call a URL after a verified park. An optional IP camera can watch the scope.
- **Polar alignment** (three-point, cable-safe RA moves only) with a phone page at `/polar`: big alt/az numbers with "raise/lower/turn east/west", a star-to-reticle view and an error trace while you turn the wedge knobs. See [docs/polar-alignment.md](docs/polar-alignment.md).
- **Horizon feature:** capture a 360° panorama with the wide camera, plate-solve it, stitch it, extract the horizon line, edit it in the browser, and export a **Stellarium landscape** (zip) and a **NINA `.hrz`** horizon file. See [docs/horizon.md](docs/horizon.md).

## Planned

Unattended operation (scheduling, waiting for targets to rise), autofocus, twilight flats, a cable-safe **Alpaca Telescope proxy** (so any ASCOM client gets cable safety), and running the horizon workflow from the browser. See [docs/design.md](docs/design.md).

## Requirements

- SeeStar S30 Pro with Alpaca enabled in the SeeStar app (firmware ≥ 9.75 needs it switched on). Other SeeStar models are untested; the plain S30 has no wide camera over Alpaca.
- An equatorial wedge, with the app's EQ mode and polar alignment done. Alt-az use isn't covered yet.
- Python ≥ 3.11 on a host on the same LAN as the scope. Image downloads are about 16 MB per frame, so avoid VPN and WiFi hops for imaging.
- Optional:
  - an RTSP camera that sees the scope;
  - an Alpaca Dome for the roof;
  - a wide-field plate solver for the wide camera (currently the author's `projections` solver, private; an open replacement is on the to-do list);
  - the [ASTAP](https://www.hnsky.org/astap.htm) command-line solver plus its D05 star database (about 110 MB) for telephoto solving. It runs as a separate program; see [docs/astap.md](docs/astap.md).

## Quick start

```bash
git clone https://github.com/komakallio/seestar-commander && cd seestar-commander
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml   # then edit: scope address, site, cable limits
.venv/bin/python tools/probe.py      # read-only: lists Alpaca devices and capabilities
```

Then read [docs/seestar-alpaca-notes.md](docs/seestar-alpaca-notes.md) **before** your first move.

## Sequencer v1 (supervised)

One long-running process owns the devices. Everything else (the CLI, the status page, an agent) talks to its HTTP API. **Only one process may move the mount**: a script that needs the scope while the service runs takes the [scope lock](#scope-lock) first (`SEESTAR_LOCK_OWNER=me python tools/goto.py …`); never run `tools/` scripts against the scope without it.

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
.venv/bin/python -m sequencer sim-safety safe && .venv/bin/python -m sequencer roof open   # refused while unsafe
.venv/bin/python -m sequencer resume
```

`--sim` without a `config.toml` uses `config.example.toml`. It starts at the current time if it's dark, otherwise at tonight's astronomical dark (`--sim-start 2026-11-08T20:00:00Z` to choose). `--sim-before-limit 20` starts it 20 minutes before tonight's [night limit](#night-limit-and-heartbeat), to watch a plan stop and park there. `--sim-no-solver` simulates a missing plate solver.

**Against the real scope** (after [Safety](#safety) and the cable-window measurement):

1. Fill in `config.toml`: `[mount]` cable window, `[sequencer]` (bind `host = "0.0.0.0"` and set `api_token` if the phone is on the LAN), `[darks]`, optional `[roof]`, `[safety]` and `[hooks]`.
2. Optional telephoto solving: install the ASTAP CLI and the D05 database ([docs/astap.md](docs/astap.md)) and set `[solver] astap_path` / `db_dir`. A solve takes about 0.2 s on a 2 s frame (`solve_exposure`, default 2 s). Without it, centring falls back to mount pointing (or `fallback = "blob"` for bright extended targets).
3. `python -m sequencer serve` on the host next to the scope (nohup or a systemd unit), and the CLI from anywhere with `--url http://host:8640 --token …`.
4. Start with a short plan while you watch (a camera on the scope helps), and keep `park` within reach.

**What one target does:** cable-safe move (`scope.move_hadec`, never a firmware goto) → tracking on → centre (telephoto plate solve, sync in JNow, re-centre; fallback when no solver) → lights with per-frame QA and drift, and a dither before every `dither_every`-th frame → FITS with headers (`OBJCTRA`/`OBJCTDEC` sexagesimal, `RA`/`DEC` in degrees, both the J2000 target; `MNT_*` the mount; `DARKUSED` names the master dark). Before every frame it checks the constraints and the cable window against the coming tracking time, and stops the target before the limit. Frames go to `data/sessions/<night>/<target>/lights/`, the running stack to `…/<target>/stack.fits`.

**Safety:** the service polls `tools/safety.py` (`[safety] url`). On unsafe it aborts the exposure, does the guarded park (RA to home by axis moves, then firmware Park under the RA guard), posts `on_parked`, and if a roof is configured raises **roof close requested**. By default it never closes the roof by itself: the operator runs `roof close`, which is refused unless the scope reads AtPark. With `[sequencer] auto_close_on_unsafe = true` it closes the roof itself, but only after a verified park. Nothing restarts when it's safe again until `resume`.

### Night limit and heartbeat

**Night limit** (`[sequencer] sun_limit_deg`, default −8, and optional `latest_end = "HH:MM"` in `[site] timezone`): a service-wide end of the night that applies to every plan on top of the plan's own `sun_max_alt` / `end_time`; the stricter one wins. It's checked before every frame against the end of that frame, and by the monitor while the plan does anything else (slewing, centring, waiting for the roof). When the limit is reached the plan stops, the scope does the guarded park **whatever `park_at_end` says**, `on_parked` is posted, and then the roof: closed after a verified park if the plan has `close_roof_at_end` or `auto_close_on_unsafe` is on, otherwise **roof close requested**. If the scope is out and idle when the limit passes (a plan with `park_at_end = false`, a stopped plan, polar alignment), it is parked too, unless a script holds the scope lock. Plans and `resume` are refused once the limit (or the plan's own night end) is reached, with the time imaging is allowed again. `latest_end` belongs to the night: `"05:30"` means the next morning in the evening and stays "reached" from 05:30 until local noon. `status`, `check` and the page summary show the limit and when it will be reached; the timeline shades daylight above the effective (stricter) Sun limit. Use it to finish well before an outside roof controller closes for dawn.

**Heartbeat** (`[hooks] heartbeat_url`): while a plan runs (`heartbeat_while = "plan"`, or `"job"` for any job, `"always"`), the service sends a beat every `heartbeat_interval_s` (60): a JSON POST (`heartbeat_method = "POST"`) with service id, state, busy job, plan, target, mount `at_park`, `roof_wish` (open / closed), time and the expected end, or a plain GET of the URL (`"GET"`, healthchecks.io style). When a plan or park ends it sends one `final` beat with the resulting state. Failures are logged at most every 10 minutes and never stop imaging; `status` shows sent/failed counts and the last error (not the URL). The payload is specified in [docs/design.md](docs/design.md#heartbeat).

### Roof (optional)

One roof: an Alpaca Dome (`[roof] alpaca`, shutter only) or HTTP URLs (`open_url`, `close_url`, `state_url` returning `{"open": bool}`). An HTTP answer `{"message": REASON, "error": true}` is a refusal and the reason shows in the log and status. Open and close wait up to `[roof] timeout_s` for the roof to report the new state.

- **Plan options** (`[plan]`): `open_roof_at_start` opens before the first target, only while safety reports safe; a refusal, unsafe or a timeout stops the plan before anything moves. `close_roof_at_end` closes after the end-of-plan guarded park, only when the park is verified and the mount reads AtPark; otherwise it raises roof close requested. A stopped or interrupted plan never closes the roof at the end.
- **Config** (`[sequencer]`): `auto_close_on_unsafe` (default false), see Safety above.
- The status page shows, for the running plan, whether auto-open / auto-close are on, the last roof action and its result, and the roof steps in the phase and log.

These options are for supervised or simple setups. If the site has a separate roof/safety controller, that controller is the better owner of unattended closing; leave these off and let the sequencer park.

**Status page** (`http://host:8640/`, built for a phone in portrait, dark by default, with a red-light toggle): a big headline of what it's doing now ("Slewing to M45", "Centring M45 (solve 2/4, 8.4′ off)", "M45 exposure 1/3 (37 / 60 s)", "Downloading M45 1/3", "Dithering M45 (#3, 14 px)", "Parking", "Waiting: unsafe (RAIN)", …), also in the tab title, with a live exposure bar; the plan with frames done/total, status and reasons, and a night timeline (target altitudes, min-alt limit, Sun limit (the stricter of the plan's and the service night limit), plan end, now, estimated finish); then mount and cable, latest frame, stack, QA/drift, dark library, safety and log. It updates over Server-Sent Events (`/api/events`), falls back to polling `/api/status` while the stream is down, reconnects by itself, and shows "updated N s ago" in red when updates stall.

**Dark library:** masters live in `data/darklib/`, keyed by camera, exposure, gain, offset, binning and a 2 °C sensor-temperature bin (the S30 Pro is uncooled; the telephoto reports `CCDTemperature`). Entries expire after 60 days. The status page lists entries (fresh / expiring / expired) and what the active plan (and any `[darks] plans`) is **missing** at the current sensor temperature; `darks build --missing` fills it with the wheel on `Dark` (fine with the roof closed). Lights without a matching master are stacked uncalibrated and flagged.

| Command | API | |
|---|---|---|
| `status` | `GET /api/status` | everything the page shows |
| | `GET /api/events` | the same, pushed as Server-Sent Events on every change |
| | `GET /api/timeline` | plan targets' altitude curves for the rest of the night |
| `run PLAN` | `POST /api/plan` | start a plan |
| `stop` / `resume` | `POST /api/stop`, `/api/resume` | stop aborts the exposure, doesn't park |
| `park` | `POST /api/park` | guarded park + `on_parked` |
| `roof open` | `POST /api/roof/open` | refused while unsafe (or safety not known yet) |
| `roof close` | `POST /api/roof/close` | only when parked |
| `safety` | `GET /api/safety` | last poll |
| `darks list` / `darks build` | `GET /api/darks`, `POST /api/darks/build` | dark library |
| `polar start` / `stop` / `done` / `status` | `POST /api/polar/start`, `/stop`, `/done`; `GET /api/polar` | polar alignment; phone page `/polar` |
| `lock --owner X --reason … --ttl 600` / `unlock [--force]` | `POST /api/lock`, `/api/unlock`; `GET /api/lock` | exclusive use for a script (refused while a job runs) |

### Pointing memory

The firmware `Park()` drops the mount's sync, so without help the first solve after every unpark is far off (on one wedge: about 100′, a constant +1.5° in Dec plus −0.5…−1.3° in RA that depends on HA). The service logs every centring solve in `data/pointing/solves.jsonl` (time, target, the mount's reported and *raw* HA/Dec, the solved HA/Dec in JNow, and the offset) and keeps track of what the firmware's sync currently adds (`data/pointing/state.json`: zero after a park). Each target's **first slew is commanded off the target** by the remembered error, so it lands within a few arcminutes and centring needs one solve instead of two or three. The model is the median of the latest solves until there are `min_solves_for_fit` solves over `min_ha_span_deg` of HA, then a least-squares fit of IH, ID (index offsets), MA, ME (polar-axis azimuth/elevation error, same signs as the polar tool). The status page's *Pointing model* card shows the model, its rms and the latest residuals; `[pointing]` in `config.toml` configures it. A polar alignment **Done** starts the model afresh. Details: [docs/design.md](docs/design.md#pointing-memory).

### Scope lock

A script that wants the scope while the service runs (a satellite-tracking test, the polar tool, `tools/*.py`) takes the lock:

```python
from scopelock import ScopeLock            # tools/scopelock.py
with ScopeLock("sattrack", "ISS pass test", ttl_s=120) as lk:
    ...                                     # lk.lost is set if it's revoked (unsafe) or can't be renewed
```

or `Scope(lock_owner="sattrack")`, or `SEESTAR_LOCK_OWNER=sattrack` in the environment for any script built on `tools/scope.Scope` (its axis moves stop once the lock is lost); from a shell, `python -m sequencer lock --owner me --reason "…"` and `unlock`. It uses the service's API (`$SEQ_URL` / `$SEQ_TOKEN`, else `config.toml`); while it's held the service refuses plans, park, roof open, darks and polar jobs and the page shows "Scope in use by …". The lock expires `ttl_s` after the last renewal (the helper renews every ttl/3, at most every 5 s). If safety turns unsafe the service revokes it, waits `lock_unsafe_grace_s` and parks. If the service isn't running, the helper falls back to an exclusive file lock on `<data_dir>/scope.lock`; the service holds the same file lock while it runs a job or has granted the API lock, so a script that couldn't reach the API is refused while the service moves the scope, and the service refuses its jobs while such a script holds it. The cable watchdog still turns tracking off at the window limit while the scope is locked; nothing else in the service touches the mount.

**Polar alignment:** `python -m sequencer polar start [--dec 65] [--span 30]` moves only the RA axis to the current hour angle and ±30° (refused if the cable window doesn't allow it), solves the telephoto at each point and reports the error of the RA axis against the refracted pole in alt and az (arcmin, with the direction to move). It then stays at the last point and keeps solving while you turn the knobs. Open `http://host:8640/polar?token=…` on the phone at the wedge. It runs as a job on the worker (not alongside a plan), and `stop`, `park` and unsafe end it. On the simulator: `serve --sim` has a 50′/−70′ error, and `sim-polar -30 50` turns the knobs. Details and accuracy notes: [docs/polar-alignment.md](docs/polar-alignment.md).

## Safety

- **Measure your cable window first.** Note the RA-axis hour angle at home (`tools/probe.py`; the mount reads Dec −90 there) and how far the RA axis may turn each way. Put those numbers in `[mount]`. The defaults are one wedge's values, not yours.
- **Never use firmware gotos for big RA moves.** Use `tools/goto.py` / `scope.move_hadec`, which enforce the window.
- **Parking:** first bring RA to the home angle with axis moves, then call the firmware `Park` under the RA guard (`tools/park_guarded.py`).
- **Roof:** park the SeeStar before closing a roof or enclosure; most have no telescope-position interlock.

## Layout

| Path | What |
|---|---|
| `sequencer/` | the v1 service: plan files and constraints, simulator, dark library, QA/stacking, HTTP API, CLI (`python -m sequencer`) |
| `tools/` | scripts and modules: `scope.py` (devices, cable-safe motion, guarded park), `scopelock.py` (exclusive use), `config.py`, `safety.py`, panorama/horizon pipeline, `poc_m31.py` (first imaging run) |
| `tests/` | pytest suite, all against the simulator |
| `web/` | `sequencer.html` (status page, served by the service), `monitor.html` (allsky capture monitor), `horizon_editor.html` |
| `plan.example.toml` | example plan file |
| `docs/` | design, SeeStar Alpaca notes, horizon workflow, grid optimisation |
| `config.toml` | **your** site config (git-ignored) |
| `data/` | frames, state and products (git-ignored) |

For agents deploying this: see [AGENTS.md](AGENTS.md).

## License

MIT, see [LICENSE](LICENSE).
