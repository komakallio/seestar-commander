# Remote imaging system: design draft

Status: **draft** (Nov 2026). An own control system on ASCOM Alpaca, no NINA.

## Goals

- Run the SeeStar S30 Pro remotely and reliably from target selection to calibrated stack, without ever breaking the cable or roof rules.
- Be operable by a person and by an agent (Claude) through the same interfaces, with one source of truth for state.

## Shape

```
            CLI / agent            web dashboard (status, live frames, horizon/allsky)
                 \                      /
                  +---- control API ---+          (HTTP/JSON on the LAN / Tailscale)
                           |
                    observatory daemon (Python, asyncio, one per site host)
     +-----------+---------+-----------+-------------+--------------+
     | devices   | motion  | sequencer | pipeline    | safety       |
     | Alpaca    | cable-  | plans,    | solve, QA,  | weather, sun,|
     | clients   | safe    | scheduler,| calibrate,  | roof, park   |
     |           | axes    | calib.    | stack       | checks       |
     +-----------+---------+-----------+-------------+--------------+
                           |
          state store (SQLite: frames, unwrap angle, calibration library, events)
          + data store (FITS on disk/NAS)
```

## Implemented: sequencer v1 (supervised)

`python -m sequencer` (package `sequencer/`). One process with plain threads and no database yet: state is in memory, mirrored to `data/sequencer/status.json` and `log.txt`.

| Part | v1 |
|---|---|
| **Service** | `service.Service`. A *worker* thread runs one job at a time (plan, park, roof close, darks build) and is the only thread that moves the mount. A *safety* thread polls the safety source. A *monitor* thread reads position and the cable angle for the page, and is the cable-watchdog backstop (tracking off at the limit). |
| **Motion** | Only through `tools/scope.Scope`: `move_hadec` (closed-loop MoveAxis, persisted unwrapped HA) and the new `park_guarded` (RA to home by axis moves, then the firmware Park in a thread, aborted if RA turns more than 10°). `Scope` now accepts injected devices, a clock and an `interrupt` callable, so stop/unsafe can break off a move. Firmware gotos are never called. |
| **Plans** | TOML (or YAML) `[[step]]` targets: name or RA/Dec, exposure, gain, count, filter, dither (stub), centring mode, QA limits. Constraints: minimum altitude, horizon file, Sun altitude, end-of-night time. |
| **Per target** | Constraints and cable room checked → cable-safe move → tracking on → centre → lights. Before every frame: Sun / end time at the end of the frame (stops the plan), altitude/horizon (stops the target), and the remaining cable window against the frame's tracking time plus a reserve (stops the target and turns tracking off). |
| **Centring** | Telephoto plate solve (`astrometry` package, 4100 index series) → `SyncToCoordinates` in JNow → re-centre, up to 4 iterations; syncs more than 10° off are refused. Fallbacks: mount pointing, or the PoC's probe-move blob centring for bright extended targets. |
| **Pipeline** | Per frame: star count, HFR, eccentricity, background, drift (phase correlation) and drift rate. FITS with mount, target, cable angle, QA and `DARKUSED` headers. Running stack: per-frame dark subtraction, debayer, translation registration, then a Welford mean with 3σ rejection once 3 frames are in. |
| **Dark library** | `darklib.py`. Masters are keyed by camera, exposure, gain, offset, binning and a sensor-temperature bin (default 2 °C; a CCDTemperature of 0 means no reading). Each stores the frame count, mean/min/max temperature, creation time and camera identity. They expire after `max_age_days` (60) and show as "expiring" in the last 7 days. Lookup: same settings, not expired, nearest temperature within bin/2 + tolerance. "Missing" means what the active plan and `[darks] plans` need at the current temperature. The `darks build` job fills it (wheel to `Dark`, works with the roof closed, filter restored) and then re-stacks tonight's lights. `cloudy_hook` is reserved for building darks automatically on cloudy nights later. |
| **Safety** | `tools/safety.safety_state()`. On unsafe: abort the exposure, guarded park, `on_parked` hook, then **roof close requested** if a roof is configured. No automatic resume, and by default no automatic roof close: `roof close` (refused unless AtPark) and `resume` are operator commands (see Roof for the opt-in options). A darks build with the scope parked carries on. |
| **Roof (optional)** | One Alpaca Dome shutter or HTTP open/close/state URLs (`rig.AlpacaRoof`, `rig.HttpRoof`; an HTTP `{"message", "error": true}` answer is a refusal with its reason). Plan options `open_roof_at_start` (only while safety reports safe; refusal, unsafe or `[roof] timeout_s` stops the plan before any motion) and `close_roof_at_end` (after the end-of-plan guarded park, only when it's verified and the mount reads AtPark; else roof close requested). `[sequencer] auto_close_on_unsafe` (default off) closes after a verified unsafe park. `roof open` command/API, refused while unsafe. These are for supervised or simple setups: a separate roof/safety controller, where a site has one, is the better owner of unattended closing. |
| **Interfaces** | HTTP JSON API and a status page (`web/sequencer.html`: state, step, mount and cable bar, latest frame, stack, QA/drift table, dark library, safety, log); optional bearer token for commands. CLI: `serve / run / status / stop / resume / park / roof open|close / safety / darks / check`. |
| **Simulator** | `sim.py`. Mount with pointing error, tracking-rate error, refusal to track below the horizon, a firmware Park that goes the wrong way when far from home, and gotos refused and counted. Telephoto star field from the true pointing, with temperature-dependent dark current and hot pixels. Filter wheel, focuser, dew switch, dome (opens and closes with a delay; refusal and stuck modes for tests), safety source and a solver that reads the truth. A scaled clock runs it faster than real time. `tests/` cover the cable guard, safety → park, constraints, plans, the dark library and the API. |

**Not in v1:** a scheduler (targets run in plan order and are skipped if they aren't observable *now*); dithering; autofocus; flats; wide-camera centring; re-acquire after an unwind; the SQLite state store; notifications; the Alpaca Telescope proxy.

## Components

1. **Devices.** Thin Alpaca clients (alpyca) wrapped with timeouts, retries and reconnects. Each device knows its quirks:
   - every SeeStar exposure stops any running view;
   - `frame_calib` stays off;
   - turning tracking on is refused while the scope points below the horizon;
   - the ImageArray download is slow.
2. **Motion: the only path to the mount.**
   - **Never** uses a firmware goto for RA. It moves with closed-loop MoveAxis and tracks the unwrapped HA in persistent state.
   - The cable window is configured per scope; for the SeeStar it's 90° counter-clockwise / 270° clockwise from home.
   - Plans the shortest legal path and refuses anything outside the window. Tracking time counts against the window, with an automatic unwind and re-acquire before the limit.
   - **Planned :** expose it as **our own Alpaca Telescope device**, a cable-safe proxy in front of the SeeStar. Other clients (Stellarium's telescope control, scripts, any ASCOM software) then get cable safety for free; gotos become legal axis moves.
3. **Sequencer.**
   - **Plan files** (YAML) list targets with exposures, filters, dither, priority and constraints (altitude, horizon file, moon, twilight).
   - **Scheduler:** what to image now, given tonight's horizon and the time.
   - **Calibration jobs:**
     - darks, with the wheel on Dark, matched in exposure, gain and temperature, before and after each block;
     - twilight sky flats, with exposure servo;
     - bias / dark-flats.
   - **Centring:** wide-camera solve with the projections solver, then sync, then telephoto solve to refine (astrometry.net index 4110–4112), then re-centre.
   - **Autofocus:** HFR V-curve, triggered by temperature change or HFR drift.
4. **Pipeline.**
   - **Per-frame QA:** HFR, star count, eccentricity (trailing), background (clouds, dawn).
   - **Calibration:** dark-subtract, flat-divide, debayer (GRBG).
   - **Stacking:** register, then sigma-clip a running stack. Results go to a per-target product folder.
5. **Safety.**
   - **Inputs:** an Alpaca SafetyMonitor / ObservingConditions, the sun's altitude, and a heartbeat from the daemon itself.
   - **When unsafe:**
     1. stop exposures;
     2. park the SeeStar and verify it (position, optional IP camera);
     3. close the roof/enclosure through its Alpaca Dome, if configured;
     4. confirm closed and notify.
6. **Interfaces.**
   - A CLI: `obs status`, `obs run plan.yaml`, `obs stop`, `obs park`, `obs roof close`.
   - A JSON control API that the CLI and agent use.
   - The web dashboard.
   - Notifications.


## Planned: polar alignment tool with a mobile web UI 

Goal: you stand at the wedge with a phone and turn the alt/az knobs while the page tells you which way and how far, ideally by moving a star onto a reticle.

1. **Measure: three-point polar alignment.**
   - Plate-solve at three RA positions, moving only the RA axis. That's cable-safe, within the window, and spans for example ±30°.
   - The plane fitted through the three solved directions gives the true RA axis. Its offset from the refracted celestial pole is the polar error, split into altitude and azimuth.
   - Solver: wide camera with the projections solver (fast, large field, robust), with the telephoto (astrometry.net) for a fine check.
2. **Adjust: live loop.**
   - Stay at the last position and solve continuously, a few seconds per frame, while the knobs are turned.
   - Update the residual error after each solve. The RA axis itself doesn't move during adjustment, so each new solve says where the axis now points.
3. **Phone UI** (served by the on-site daemon, works over Tailscale or the site WiFi):
   - **Big numbers:** altitude error and azimuth error in arcminutes, with arrows such as "raise", "lower", "turn left" and "turn right" (screw directions calibrated once per wedge), colour-coded to the target tolerance.
   - **Reticle view:** a crop of the live frame around a bright star, with a reticle where that star must go when the error is zero. Turning the knobs moves the star onto the reticle, and the size of the error circle shows the tolerance.
   - **History:** a small trace of error against time, so overshoot is easy to see.
   - **Finish:** a "Done" button stores the final error and timestamp in the state store. The drift monitor later reports whether alignment still holds.
4. **Ongoing check:** the frame-to-frame drift measured during imaging feeds a running polar-error / pointing-model estimate, which flags when alignment has changed (for example after a cold night or a bump).

