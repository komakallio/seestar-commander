# seestar-commander

Remote control of a **ZWO SeeStar S30 Pro on an equatorial wedge** over its built-in **ASCOM Alpaca** server, in plain Python. No NINA, no SeeStar app in the loop.

> **Status: early and experimental (Nov 2026).** It works on one setup and is being turned into a proper service. Expect rough edges. Read [Safety](#safety) before moving anything.

## What it does today

- **Cable-safe mount motion.** The RA axis moves only by closed-loop `MoveAxis`, inside a configured window around the home hour angle. Its unwrapped angle is persisted, so it survives restarts. **The firmware's own gotos (`SlewToCoordinates`) can take the long way round and wind the USB cable**, so they're avoided.
- **Imaging:** wide-angle and telephoto cameras, filter wheel (including the `Dark` position for dark frames), focuser, dew heater.
- **Pointing:** plate solving, then sync, then centring. A **pointing memory** logs every solve and offsets each target's first slew by the remembered error (constant offset, then a fitted index + polar-axis model), because the firmware forgets its sync when it parks. See [Pointing memory](#pointing-memory).
- **Exclusive use:** a lock with a TTL hands the scope to a script (`python -m sequencer lock`, `tools/scopelock.py`); the service refuses its own jobs meanwhile. See [Scope lock](#scope-lock).
- **Sequencer v1 (supervised):** one service that owns the devices and runs plan files (targets, exposures, altitude/horizon/Sun/end-of-night constraints), with a JSON control API and a phone-friendly status page. See [Sequencer](#sequencer-v1-supervised).
- **Target scheduler (`order = "best"`):** instead of running the steps in order, pick the next target block by block from what's observable now and for the whole block (altitude above the min alt and horizon, Sun / night limit, RA cable room), how urgent it is (would it set or the night end before its frames are done), its altitude and an optional `priority`; targets come back for more blocks (`block_frames` / `block_minutes`), and when nothing is up yet the scope parks and waits ("Waiting for M42 (rises above 25° at 23:40)"). The status page shows the projected order for the rest of the night. See [Scheduler](#scheduler-order--best).
- **Dithering:** small random offsets (default up to 15 telephoto px) around the centred position between lights, made with timed, cable-checked MoveAxis pulses with tracking on; the achieved offsets are measured from the frames' registration and logged in the frame log and FITS headers (`DITHERX`/`DITHERY`), with an optional plate-solve re-centre every N dithers. See [Dithering](docs/design.md#dithering).
- **Wide camera alongside the telephoto:** with `wide_every = N` a wide-angle frame is taken with every Nth telephoto light, exposed while the telephoto exposes and the mount tracks (never across a dither or slew), downloaded while a later telephoto light runs, so the telephoto cadence doesn't change. Frames go to `…/<target>/wide/` with the same mount/time/target headers, and are plate-solved in the background with the wide-field solver (WCS in the header and a `.wcs.json` sidecar). See [Wide camera](#wide-camera-alongside-the-telephoto).
- **Calibration and stacking:** frame QA (star count, HFR, eccentricity), drift between frames, a **dark library** (master darks matched by exposure, gain, offset, binning and sensor temperature, with expiry), a **flat library** (auto-exposed panel flats per filter, matched by filter, binning and focuser position at any gain, with expiry), and a running (light − dark) / flat, registered, sigma-clipped stack.
- **Roof and safety hooks (optional):** open and close through an ASCOM Alpaca **Dome** or simple HTTP open/close/state URLs (plans can open at start and close at end, see [Roof](#roof-optional)); poll an HTTP JSON safety endpoint (`tools/safety.py`, unreachable for too long = unsafe); call a URL after a verified park. An optional IP camera can watch the scope.
- **Polar alignment** (three-point, cable-safe RA moves only) with a phone page at `/polar`: big alt/az numbers with "raise/lower/turn east/west", a star-to-reticle view and an error trace while you turn the wedge knobs. See [docs/polar-alignment.md](docs/polar-alignment.md).
- **Horizon feature:** capture a 360° panorama with the wide camera, plate-solve it, stitch it, extract the horizon line, edit it in the browser, and export a **Stellarium landscape** (zip) and a **NINA `.hrz`** horizon file. See [docs/horizon.md](docs/horizon.md).

## Planned

Unattended operation (weather-aware restarts, multi-night plans), autofocus, twilight sky flats (panel flats are done), a cable-safe **Alpaca Telescope proxy** (so any ASCOM client gets cable safety), and running the horizon workflow from the browser. See [docs/design.md](docs/design.md).

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
.venv/bin/python -m sequencer flats build --plan plan.example.toml --count 5   # the simulated panel
.venv/bin/python -m sequencer run plan.example.toml
.venv/bin/python -m sequencer status
.venv/bin/python -m sequencer sim-safety unsafe RAIN      # watch it abort, park and request the roof
.venv/bin/python -m sequencer roof close                  # operator confirms
.venv/bin/python -m sequencer sim-safety safe && .venv/bin/python -m sequencer roof open   # refused while unsafe
.venv/bin/python -m sequencer resume
```

`--sim` without a `config.toml` uses `config.example.toml`. It starts at the current time if it's dark, otherwise at tonight's astronomical dark (`--sim-start 2026-11-08T20:00:00Z` to choose). `--sim-before-limit 20` starts it 20 minutes before tonight's [night limit](#night-limit-and-heartbeat), to watch a plan stop and park there. `--sim-no-solver` simulates a missing plate solver. `--sim-flats manual` makes flats wait for `sim-panel on` and `flats confirm`, like a hand-held panel.

**Against the real scope** (after [Safety](#safety) and the cable-window measurement):

1. Fill in `config.toml`: `[mount]` cable window, `[sequencer]` (bind `host = "0.0.0.0"` and set `api_token` if the phone is on the LAN), `[darks]`, optional `[roof]`, `[safety]` and `[hooks]`.
2. Optional telephoto solving: install the ASTAP CLI and the D05 database ([docs/astap.md](docs/astap.md)) and set `[solver] astap_path` / `db_dir`. A solve takes about 0.2 s on a 2 s frame (`solve_exposure`, default 2 s). Without it, centring falls back to mount pointing (or `fallback = "blob"` for bright extended targets).
3. `python -m sequencer serve` on the host next to the scope (nohup or a systemd unit), and the CLI from anywhere with `--url http://host:8640 --token …`.
4. Start with a short plan while you watch (a camera on the scope helps), and keep `park` within reach.

**What one target does:** cable-safe move (`scope.move_hadec`, never a firmware goto) → tracking on → centre (telephoto plate solve, sync in JNow, re-centre; fallback when no solver) → lights with per-frame QA and drift, and a dither before every `dither_every`-th frame → FITS with headers (`OBJCTRA`/`OBJCTDEC` sexagesimal, `RA`/`DEC` in degrees, both the J2000 target; `MNT_*` the mount; `DARKUSED` names the master dark). Before every frame it checks the constraints and the cable window against the coming tracking time. At the cable limit it **unwinds and re-acquires** (below) when that's worth it, otherwise it stops the target before the limit.

**Unwind and re-acquire.** Tracking turns RA clockwise, so a long target uses up the cable window. When the window can't fit the next frame plus `cable_reserve_min`, the RA axis turns a full turn back (counter-clockwise, through home) to the same sky position: closed-loop `MoveAxis` with the unwrapped bookkeeping, never a goto, checked against the window before it moves and stopped by stop/unsafe/night limit. A 360° turn takes about 1.5 min. Then it re-centres with a plate solve (pointing memory applies) and the remaining frames go on; dithering continues around the new centre. The headline reads "Unwinding cable (M45)". **If the window is narrower than 360°** (e.g. −175…+175 around home −90), the turn back lands in the gap behind the CCW end: the scope can wait there with tracking off until the sky has carried the target inside (about 4 min per degree of gap, ~50 min for that window), at most `unwind_max_wait_min`. **That wait is opt-in: `unwind_max_wait_min` defaults to 0, so on a window narrower than about 363° the default behaviour is unchanged (the target stops at the cable limit).** Only a window wider than that unwinds by default. With `order = "best"` the unwind never waits inline: the block ends at the limit (the step goes back to pending) and the scheduler uses the time on other targets, returning once the target is inside the window again. Per-step options: `unwind` (default true), `unwind_min_frames` (3) and `unwind_min_minutes` (10): don't unwind unless at least that many frames / minutes of imaging then fit (cable room, altitude, horizon, Sun, night limit); `unwind_max_wait_min` (0 = never wait). Otherwise the target stops as before. See [docs/design.md](docs/design.md#unwind-and-re-acquire). Frames go to `data/sessions/<night>/<target>/lights/`, the running stack to `…/<target>/stack.fits`.

**Gain:** the telephoto's default gain is **200** (plans, `flats build`, `darks build`, polar alignment). The gain scale is 0.1 dB; on the S30 Pro telephoto the low-noise (HCG) readout switches on at exactly 200, with about 4× lower read noise in electrons than gain 80 and the same dynamic range per sub. Stars saturate about 4× sooner than at 80, so 15–60 s subs suit most targets. A step's `gain` still overrides it. The wide camera's default stays 100.

**Safety:** the service polls `tools/safety.py` (`[safety] url`). On unsafe it aborts the exposure, does the guarded park (RA to home by axis moves, then firmware Park under the RA guard), posts `on_parked`, and if a roof is configured raises **roof close requested**. By default it never closes the roof by itself: the operator runs `roof close`, which is refused unless the scope reads AtPark. With `[sequencer] auto_close_on_unsafe = true` it closes the roof itself, but only after a verified park. Nothing restarts when it's safe again until `resume`.

### Scheduler (order = "best")

By default (`[plan] order = "sequence"`) the steps run in order, each to its count, and a step that isn't observable when its turn comes is skipped. With `order = "best"` the scheduler (`sequencer/scheduler.py`) chooses at the start and after every block:

- **Candidates:** steps with frames still needed that are observable now (altitude above max(min alt, horizon at their azimuth), Sun and night limit) and where at least one frame fits before the target sets, the night ends or the RA cable window runs out. A block is `block_frames` frames, or what fits in `block_minutes` (the smaller of both; neither = the whole rest), shortened to what fits.
- **Score:** urgency (how little time the target has to spare before it sets or the night ends, so a target that would otherwise be lost goes first), altitude (high and transiting first), frames left, `priority` (+25 per unit), minus a penalty when the cable window would cut the block short. The weights are in [docs/design.md](docs/design.md#scheduler).
- **Revisits:** a step with frames left after its block goes back to *pending* and competes again; it is done when its count is reached. Equal targets alternate block by block.
- **Waiting:** if nothing is observable now but something will be before the night ends, the scope does the **guarded park** (tracking off, RA home, firmware Park, `on_parked` hook; it unparks with `on_unparked` before the next block) and waits. The headline says "Waiting for M42 (rises above 25° at 23:40)" (time in `[site] timezone`). `stop`, unsafe and the night limit end the wait like any other phase.
- **End:** when everything is done, or nothing more can be observed before the night ends (remaining steps *skipped* with the reason), the plan ends as usual (`park_at_end`, `close_roof_at_end`); the night limit ends it as for any plan.
- **Status page:** the plan card shows the projected schedule for the rest of the night (blocks, waits, what won't fit), a strip of blocks along the bottom of the timeline, "next ~HH:MM" per step, and the finish from the projection. It is recomputed after every block and every 5 minutes. `python -m sequencer check PLAN [--at ISO]` prints the same projection offline.

```toml
[plan]
order = "best"
[defaults]
block_minutes = 30
[[step]]
target = "M31"
count = 60
[[step]]
target = "M42"
count = 40
priority = 1
```

### Wide camera alongside the telephoto

Camera 1 (6 mm, about 32°×54°) can take frames while the telephoto images, for an all-sky record of the night (a mosaic step is planned). Plan options per step or in `[defaults]`: `wide_every` (one wide frame per N telephoto lights, 0 = off), `wide_exposure` (s, up to 60), `wide_gain`; without them the `[wide]` table in `config.toml` gives the defaults (`every = 0`: off).

- **Timing.** The wide exposure starts right after the telephoto's `StartExposure`, and only if it ends before the mount next moves (the telephoto lights up to the next dither, re-centre or the end of the target); otherwise the frame waits for a light where it fits, or is skipped (logged) if it can never fit between dithers. Its download (~2.5 s) runs while a telephoto exposure has enough time left, never at the same time as the telephoto's own download (one Alpaca bridge), and the service doesn't touch camera 1 while the mount moves. A finished frame still waiting at the end of a target is downloaded before the slew (a few seconds once per target). Stop, unsafe, the night limit, a park or a scope lock abort the wide exposure and drop a frame not yet downloaded. The telephoto and the wide exposures were measured not to interfere on the SeeStar in either order.
- **Files.** `data/sessions/<night>/<target>/wide/<target>_wide_<exp>s_g<gain>_NNN.fits`: raw GRBG frame with the telephoto light's headers (`OBJCTRA`/`OBJCTDEC`, `RA`/`DEC`, `EQUINOX`, `MNT_*`, `CABLE_U`, dither offset) and its own `DATE-OBS`/`DATE-AVG`/`DATE-END`, `EXPTIME`, `GAIN`, `CAMERA = 'wide'`, `CAMNUM`, `FOCUSPOS` (focuser 1, never moved), `TELEFRM` (the telephoto light it rode along).
- **Solving.** With the projections solver (`[paths] projections_dir`, needs `node`) each saved frame is solved in a background thread: `WCSSOLVD`, a local TAN WCS at the frame centre in the header, and `<frame>.wcs.json` (centre, the TAN, a pixel → sky grid and the solver's own solution with its lens model in `<frame>.solution.json`). Failures are logged and never affect imaging; without a solver frames are just saved.
- **Status page:** a *Wide* card with the last frame, solved or not, and counts.

### Night limit and heartbeat

**Night limit** (`[sequencer] sun_limit_deg`, default −8, and optional `latest_end = "HH:MM"` in `[site] timezone`): a service-wide end of the night that applies to every plan on top of the plan's own `sun_max_alt` / `end_time`; the stricter one wins. It's checked before every frame against the end of that frame, and by the monitor while the plan does anything else (slewing, centring, waiting for the roof). When the limit is reached the plan stops, the scope does the guarded park **whatever `park_at_end` says**, `on_parked` is posted, and then the roof: closed after a verified park if the plan has `close_roof_at_end` or `auto_close_on_unsafe` is on, otherwise **roof close requested**. If the scope is out and idle when the limit passes (a plan with `park_at_end = false`, a stopped plan, polar alignment), it is parked too, unless a script holds the scope lock. Plans and `resume` are refused once the limit (or the plan's own night end) is reached, with the time imaging is allowed again. `latest_end` belongs to the night: `"05:30"` means the next morning in the evening and stays "reached" from 05:30 until local noon. `status`, `check` and the page summary show the limit and when it will be reached; the timeline shades daylight above the effective (stricter) Sun limit. Use it to finish well before an outside roof controller closes for dawn.

**Heartbeat** (`[hooks] heartbeat_url`): while a plan runs (`heartbeat_while = "plan"`, or `"job"` for any job, `"always"`), the service sends a beat every `heartbeat_interval_s` (60): a JSON POST (`heartbeat_method = "POST"`) with service id, state, busy job, plan, target, mount `at_park`, `roof_wish` (open / closed), time and the expected end, or a plain GET of the URL (`"GET"`, healthchecks.io style). When a plan or park ends it sends one `final` beat with the resulting state. Failures are logged at most every 10 minutes and never stop imaging; `status` shows sent/failed counts and the last error (not the URL). The payload is specified in [docs/design.md](docs/design.md#heartbeat).

### Roof (optional)

One roof: an Alpaca Dome (`[roof] alpaca`, shutter only) or HTTP URLs (`open_url`, `close_url`, `state_url` returning `{"open": bool}`). An HTTP answer `{"message": REASON, "error": true}` is a refusal and the reason shows in the log and status. Open and close wait up to `[roof] timeout_s` for the roof to report the new state.

- **Plan options** (`[plan]`): `open_roof_at_start` opens before the first target, only while safety reports safe; a refusal, unsafe or a timeout stops the plan before anything moves. `close_roof_at_end` closes after the end-of-plan guarded park, only when the park is verified and the mount reads AtPark; otherwise it raises roof close requested. A stopped or interrupted plan never closes the roof at the end.
- **Config** (`[sequencer]`): `auto_close_on_unsafe` (default false), see Safety above.
- The status page shows, for the running plan, whether auto-open / auto-close are on, the last roof action and its result, and the roof steps in the phase and log.

These options are for supervised or simple setups. If the site has a separate roof/safety controller, that controller is the better owner of unattended closing; leave these off and let the sequencer park.

**Status page** (`http://host:8640/`, built for a phone in portrait, dark by default, with a red-light toggle): a big headline of what it's doing now ("Slewing to M45", "Centring M45 (solve 2/4, 8.4′ off)", "M45 exposure 1/3 (37 / 60 s)", "Downloading M45 1/3", "Dithering M45 (#3, 14 px)", "Parking", "Waiting: unsafe (RAIN)", …), also in the tab title, with a live exposure bar; the plan with frames done/total, status and reasons, and a night timeline (target altitudes, min-alt limit, Sun limit (the stricter of the plan's and the service night limit), plan end, now, estimated finish); then mount and cable, latest frame, stack, QA/drift, dark library, safety and log. With no plan or job, the headline shows what the dark harvest is doing ("Harvesting darks · 60 s g200 at +14.9 °C (bin +14, 2/15)" with a frame bar, "Darks: holding off (8 min)", …; *Idle* only when nothing happens), a *harvest* chip and a compact *Dark harvest* card link to the Darks page, the timeline shows tonight anyway (Sun shading at the night limit, dusk, dawn, the Moon and its phase), and the image cards say "No frame yet tonight" instead of an empty box. It updates over Server-Sent Events (`/api/events`), falls back to polling `/api/status` while the stream is down, reconnects by itself, and shows "updated N s ago" in red when updates stall.

**Dark library:** masters live in `data/darklib/`, keyed by camera, exposure, gain, offset, binning and a 2 °C sensor-temperature bin (the S30 Pro is uncooled; the telephoto reports `CCDTemperature`). Entries expire after 60 days. The status page lists entries (fresh / expiring / expired) and what the active plan (and any `[darks] plans`) is **missing** at the current sensor temperature; `darks build --missing` fills it with the wheel on `Dark` (fine with the roof closed). Lights without a matching master are stacked uncalibrated and flagged.

**Dark archive and frame pool:** every dark frame `darks build` takes is kept with its own metadata (sensor temperature before and after, times, gain, exposure, offset, binning, median/mean level, camera identity) in `data/darkarchive/<camera>/<YYYYMMDD>/` plus `index.jsonl` (`[darks] archive = true`, the default). The sensor is uncooled and warms while it exposes, so **each frame is binned by its own temperature** (the mean of the readings before and after it), not by the series mean: the frames wait in a pool per (exposure, gain, offset, binning, 2 °C bin), a view over the archive. A frame whose reading changed by more than `max_temp_change_c` (0.5 °C) during the exposure stays in the archive, flagged `steady: false`, and is never pooled; frames older than `pool_max_age_days` (14) drop out of the pools. After each key's series, a master is built for every bin whose pool has at least `count` eligible frames (older pooled frames of that bin count too, from any day); the others stay as partial pools for later. The job logs which masters it built and which pools remain partial (also `darks_result` in the status). A pool master's `meta.json` lists its frame files, date span (`date_first`, `date_last`) and temperature min/max/mean, with `source: "pool"`; the frames stay archived and are marked used (`used.jsonl`). On start the service reads the existing archive (idempotent: a frame listed twice counts once, deleted files are skipped). `GET /api/darks` has the pools under `pool`.

**Dark harvest** (`[darks.harvest]`, off by default; `darks harvest on|off|status`): a thread in the service takes darks whenever the observatory is idle, at whatever temperature the uncooled sensor happens to be, so the library fills across temperature bins as the weather moves the sensor up and down. It runs only while no plan or job runs or is queued, nobody holds the scope lock (API or file lock), the roof is closed (if one is configured; a roof without a state reading counts as closed, then only parked is required), the scope is at park, the telephoto camera is idle (`CameraState`) and the optional `busy_url` check says idle, and `holdoff_min` (10) after the last of these. It uses the camera and the filter wheel only (wheel on `Dark` for a series and back after), never the mount, never takes the lock and never shows as busy: **any plan, job or lock request pre-empts it at once** (the exposure is aborted and discarded, finished frames stay pooled, the camera and wheel are free within about a second, nothing is refused or queued behind it). `stop` stops a harvest frame too. On shutdown (Ctrl-C or SIGTERM, e.g. `systemctl restart`) the frame in flight is aborted and the wheel put back; if a killed process left the camera exposing, the next start recognises its own frame from `data/darkharvest/inflight.json` (started within exposure + 60 s) and aborts it instead of backing off as if the camera were someone else's. One frame at a time, after reading the temperature: what the plans need and is missing in this bin, then their expired/expiring masters, then the **sentinel** key (60 s g200 in the most common bin, every 3 days; its masters are kept in `darkarchive/<camera>/masters/` as an ageing time series), then the standard set (gain 200 × 10–120 s), then 1 ms **bias**; partly filled pools first; short exposures when the sensor is in the top half of its bin, long ones in the bottom half. When the bin is complete it lets the sensor cool and fills the lower bins on the way down. If it's enabled, the service is idle and parked, the current bin has work and no frame came for `stall_hours`, the status says **stalled** with the reason. Logs: `data/darkharvest/log.jsonl` (every decision with its reason, frame, master, pre-emption), one main-log line per master and a daily summary. `GET /api/darks/harvest` (open) returns everything a page needs: state and headline, sensor temperature/bin/trend, the current key and its pool, today's and this week's counters, the coverage grid (keys × bins: fresh/expiring/pool n/missing/never reached, ages), the sentinel, the pools, the stall flag and the last log entries; `POST /api/darks/harvest {"enabled": bool}` needs the API token when one is set.

**Darks page** (`http://host:8640/darks`, linked from the dark library card; same live updates, phone layout and red-light mode as the status page; view-only without the API token): a headline of what darks work is happening ("Dark 7/30 · 120 s g200 · sensor 12.6 °C"), the temperatures now with their trend over 30 min, a **coverage grid** (exposure × gain rows, 2 °C bin columns: fresh (fading with age), expiring, expired, missing, not reached in the graph's range; the current sensor bin marked; tap a cell for its master), a temperature timeline (24 h / 7 d / 30 d from `/api/templog`, shaded while exposing or busy, bin stripes coloured by coverage; zoom with the wheel, a drag-selected range or a pinch, pan by dragging when zoomed, the y axis follows the visible window, and zooming past the loaded resolution fetches that window in more detail; hover or tap shows every series, the activity and the filter at that time), the library with expiry, and frames per day and per key in the dark archive (`GET /api/darks/archive?days=30`, counts only). With the harvester: its headline (red when stalled), state and reason, blockers and hold-off, today's and this week's counters, the sentinel, a live bar for the frame being exposed, pool n/15 cells in the grid, the on/off toggle (POST, needs the token) and its log, filterable by kind. An *Analysis* tab is reserved for the dark-current model.

**Temperature log:** every `[templog] interval_s` (60 s) the service appends a row to `data/templog/YYYYMMDD.csv` (UTC date): telephoto and wide sensor temperature (empty when there is no reading), any ambient sources from `[templog.sources.<name>]` (JSON URL + dot path + optional regex, see `config.example.toml`), Sun altitude, activity (`idle`, `plan`, the job name, `locked:<owner>`, `exposing`), whether the telephoto is exposing, and the filter. While a script holds the scope lock the SeeStar isn't read at all (sensor and filter cells stay empty). `GET /api/templog?hours=24` returns the rows (open, like the other GETs); `&from=…&to=…` (ISO or unix seconds, at most 31 days) gives a window instead, and `&step=N` combines every N rows.

**Flat library:** masters live in `data/flatlib/`, stored by camera, filter, gain, binning and the **focuser position** they were taken at (dust shadows move with focus: a master matches lights within `[flats] focus_tolerance` steps). Matching ignores the gain, since a normalised flat records the optics and pixel response and is valid at any gain: a master at the light's gain is preferred, then the newest. Entries expire after 30 days. `flats build --missing` (or `--filter IR --filter LP`, `--wide` for the wide camera) switches the light source on, auto-exposes each filter to a raw mean of 40–50 % of full scale, takes `count` flats, switches the light off, subtracts a dark-flat (a matching dark-library master, else dark-flats taken with the wheel on `Dark` and stored in the dark library) and stores the median, normalised per Bayer channel. Then it re-stacks tonight's lights. Light source (`[flats] source`): `manual` waits for `flats confirm` (or the page's *Panel is on* button) after you put the panel on; `http` POSTs `panel_on_url` (with `{brightness}`, per filter if set) and `panel_off_url`; twilight sky flats are planned (`sky` is refused for now). The job never moves the mount. Lights get (light − dark) / flat in the running stack and the re-stack, with `FLATUSED` in the header; a light without a matching flat, or without a dark (dividing the bias pedestal by the flat would imprint it), is stacked without one and flagged. The wide camera's flats are stored for the planned mosaic; its frames aren't stacked.

**Nothing is deleted automatically.** How fast darks and flats really go stale isn't known yet (the dark-current analysis will measure it), so nothing driven by age, expiry or a rebuild is irreversible. Rebuilding a master for the same key moves the old one (master.fits + meta.json) to `<library>/<camera>/_history/<entry>/<created stamp>/`, keeping every version. Expiry is soft: an expired master stays in the library; lights prefer a fresh match, and when there is none they use the newest expired one (`use_expired_fallback = true` in `[darks]` and `[flats]`) rather than staying uncalibrated. That is logged once per master and plan, flagged in the light's header (`DARKAGE`/`DARKEXP`, `FLATAGE`/`FLATEXP`), in the frame list (`dark_expired`, `flat_expired`), in the library summary ("expired, used as fallback") and in the harvest coverage grid (state `expired`, `fallback: true`); the key still counts as missing, so a fresh master gets built. Frames older than `pool_max_age_days` only stop being pooled. Every dark frame stays in `data/darkarchive/`, and every flat, auto-exposure and dark-flat frame in `data/flatarchive/<camera>/<YYYYMMDD>/` plus `index.jsonl` (kind, exposure, level, temperatures, focus, filter, gain, binning, times; `[flats] archive = true`), so any master can be rebuilt or re-analysed later. Thinning the archives is a manual decision.

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
| `darks harvest status` / `on` / `off` | `GET /api/darks/harvest`, `POST /api/darks/harvest` | dark harvest |
| `flats list` / `flats build` / `flats confirm` | `GET /api/flats`, `POST /api/flats/build`, `/api/flats/confirm` | flat library; confirm = the manual panel is on |
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

or `Scope(lock_owner="sattrack")`, or `SEESTAR_LOCK_OWNER=sattrack` in the environment for any script built on `tools/scope.Scope` (its axis moves stop once the lock is lost); from a shell, `python -m sequencer lock --owner me --reason "…"` and `unlock`. It uses the service's API (`$SEQ_URL` / `$SEQ_TOKEN`, else `config.toml`); while it's held the service refuses plans, park, roof open, darks and polar jobs and the page shows "Scope in use by …". The lock expires `ttl_s` after the last renewal (the helper renews every ttl/3, at most every 5 s). If safety turns unsafe the service revokes it, waits `lock_unsafe_grace_s` and parks. If the service isn't running, the helper falls back to an exclusive file lock on `<data_dir>/scope.lock`; the service holds the same file lock while it runs a job or has granted the API lock, so a script that couldn't reach the API is refused while the service moves the scope. A script holding the file lock (it fell back, e.g. while the service restarted) is mirrored as the scope lock: `status` shows it (`lock.via = "file"`, owner, reason, pid), the page shows the "Scope in use by …" banner, plans, park, roof open, darks, flats, polar and other API locks are refused at once, the monitor and the dark harvest leave the SeeStar alone, and it ends when that process releases the lock or exits (the service can't force it). The cable watchdog still turns tracking off at the window limit while the scope is locked; nothing else in the service touches the mount.

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
| `sequencer/wide.py` | the wide camera alongside the telephoto: timing, saving, background solving |
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
