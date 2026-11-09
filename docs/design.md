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
| **Service** | `service.Service`. A *worker* thread runs one job at a time (plan, park, roof close, darks build) and is the only thread that moves the mount. A *safety* thread polls the safety source. A *monitor* thread reads position and the cable angle for the page, and is the cable-watchdog backstop (tracking off at the limit); it leaves the SeeStar alone while the scope is locked. |
| **Motion** | Only through `tools/scope.Scope`: `move_hadec` (closed-loop MoveAxis, persisted unwrapped HA) and the new `park_guarded` (RA to home by axis moves, then the firmware Park in a thread, aborted if RA turns more than 10°). `Scope` now accepts injected devices, a clock and an `interrupt` callable, so stop/unsafe can break off a move. Firmware gotos are never called. |
| **Plans** | TOML (or YAML) `[[step]]` targets: name or RA/Dec, exposure, gain, count, filter, dithering (`dither_every`, `dither_px`, settle, re-centring), centring mode, QA limits. Constraints: minimum altitude, horizon file, Sun altitude, end-of-night time, plus the service-wide night limit (below). `[plan] order`: `sequence` (default, steps in order) or `best` (the scheduler, with per-step `block_frames` / `block_minutes` / `priority`; see [Scheduler](#scheduler)). |
| **Scheduler** | `scheduler.py`, only for `order = "best"`. `_job_run` iterates `scheduler.blocks()` instead of the step list: it scores the candidates, sets `progress[i]["block_to"]` (the lights loop stops there and leaves the step *pending*), parks and waits when nothing is up, and returns when all is done or nothing more fits tonight. `project()` is the same rules run forward in time for the page. See [Scheduler](#scheduler) below. |
| **Per target** | Constraints and cable room checked → cable-safe move → tracking on → centre → lights. Before every frame: Sun / end time at the end of the frame (stops the plan), altitude/horizon (stops the target), and the remaining cable window against the frame's tracking time plus a reserve: unwind and re-acquire if it's worth it (see [below](#unwind-and-re-acquire)), else stop the target and turn tracking off. |
| **Dithering** | `dither.py`. Before every `dither_every`-th light: a random offset inside a `dither_px` disc around the centred position, made by timed MoveAxis pulses with tracking on (`Scope.pulse_axis`, cable-checked), settle, then the next frame's registration measures what it did. See [Dithering](#dithering) below. |
| **Centring** | Telephoto plate solve (ASTAP CLI + D05, a subprocess on the raw frame; the frame-centre position is evaluated from the WCS), 2 s solve frames → `SyncToCoordinates` in JNow → re-centre, up to 4 iterations; syncs more than 10° off are refused. Fallbacks: mount pointing, or the PoC's probe-move blob centring for bright extended targets. |
| **Pointing memory** | `pointing.py`. Every centring solve is logged with the raw mount position (reported minus the tracked sync shift); a constant-offset or IH/ID/MA/ME model offsets each target's first slew. See [Pointing memory](#pointing-memory) below. |
| **Scope lock** | `cmd_lock` / `cmd_unlock` (`/api/lock`, CLI `lock`/`unlock`) with a wall-clock TTL, renewed by the same owner; refused while a job runs or while unsafe. While held: plans, resume, park, roof open, darks and polar are refused (also a job queued just before the lock), the monitor doesn't touch the SeeStar at all (no position, cable or camera reads, no cable watchdog: the holder owns `ra_unwrap.json` and its own guards; polling also slowed the holder's MoveAxis calls to 0.3–0.9 s); mount and cable fields are marked stale, unsafe revokes it, waits `lock_unsafe_grace_s` and parks. The service holds `<data_dir>/scope.lock` (`fcntl.flock`) while a job runs or a lock is granted; a job can't start while someone else holds it. Client: `tools/scopelock.ScopeLock` (API, else the file lock; renewal thread; `renew: true` so a forced unlock or revocation isn't undone; if the service stops mid-lock the client takes over the file lock at its next renewal, or loses the lock if someone else got it first), opt-in in `tools/scope.Scope`. |
| **Pipeline** | Per frame: star count, HFR, eccentricity, background, drift (phase correlation) and drift rate. FITS with mount, target, cable angle, QA and `DARKUSED` headers. Running stack: per-frame dark subtraction, debayer, translation registration, then a Welford mean with 3σ rejection once 3 frames are in; the first 3 frames are rebuilt with the odd one out dropped per pixel, so a hot pixel or satellite in one of them doesn't stay in the mean. |
| **Dark library** | `darklib.py`. Masters are keyed by camera, exposure, gain, offset, binning and a sensor-temperature bin (default 2 °C; a CCDTemperature of 0 means no reading). Each stores the frame count, mean/min/max temperature, creation time and camera identity. They expire after `max_age_days` (60) and show as "expiring" in the last 7 days. Lookup: same settings, not expired, nearest temperature within bin/2 + tolerance. "Missing" means what the active plan and `[darks] plans` need at the current temperature. The `darks build` job fills it (wheel to `Dark`, works with the roof closed, filter restored) and then re-stacks tonight's lights. `cloudy_hook` is reserved for building darks automatically on cloudy nights later. |
| **Safety** | `tools/safety.safety_state()`. On unsafe: abort the exposure, guarded park, `on_parked` hook, then **roof close requested** if a roof is configured. No automatic resume, and by default no automatic roof close: `roof close` (refused unless AtPark) and `resume` are operator commands (see Roof for the opt-in options). A darks build with the scope parked carries on. |
| **Roof (optional)** | One Alpaca Dome shutter or HTTP open/close/state URLs (`rig.AlpacaRoof`, `rig.HttpRoof`; an HTTP `{"message", "error": true}` answer is a refusal with its reason). Plan options `open_roof_at_start` (only while safety reports safe; refusal, unsafe or `[roof] timeout_s` stops the plan before any motion) and `close_roof_at_end` (after the end-of-plan guarded park, only when it's verified and the mount reads AtPark; else roof close requested). `[sequencer] auto_close_on_unsafe` (default off) closes after a verified unsafe park. `roof open` command/API, refused while unsafe. These are for supervised or simple setups: a separate roof/safety controller, where a site has one, is the better owner of unattended closing. |
| **Night limit** | `[sequencer] sun_limit_deg` (−8) and optional `latest_end` (`plan.NightLimit`). One code path with the plan's own constraint: `plan.night_failures(cons, t, …, end_t, limit)` takes the stricter Sun altitude (`sun_max_alt`) and the earlier end (`end_time`); reasons from the service limit end in "(night limit)". The worker checks it before each target and against the end of each frame; the monitor checks it while the plan is in any other phase (sets the abort, the plan loop ends it the same way). Reached by the service limit: plan stopped, guarded park even with `park_at_end = false`, `on_parked`, then `close_roof_at_end` / `auto_close_on_unsafe` close after a verified park, else roof close requested. Reached by the plan's own constraint: as before (park per `park_at_end`, `close_roof_at_end`). On the moment the service limit is reached with the service idle (or in polar) and the scope out, the monitor queues a guarded park (not while a script holds the scope lock). `cmd_start` / `cmd_resume` refuse once reached and say when imaging is allowed again. `latest_end` is per night: the first HH:MM after the local noon before t. `status()["night_limit"]`: settings, `reached`, `at_unix` (when it is reached), `opens_unix`; the plan's `sun_limit_unix` / `end_unix` and the timeline's `sun_max_alt` are the effective (stricter) values. |
| **Wide camera** | `wide.py`: one wide frame per `wide_every` telephoto lights, exposed inside the mount's steady time, downloaded inside a later telephoto exposure, saved with the telephoto headers and solved in the background. See [Wide camera alongside the telephoto](#wide-camera-alongside-the-telephoto). |
| **Heartbeat** | `heartbeat.py`: optional `[hooks] heartbeat_url`, own thread, 5 s HTTP timeout, failures counted and logged at most every 10 min. See [Heartbeat](#heartbeat). |
| **Interfaces** | HTTP JSON API and a status page (`web/sequencer.html`: state, step, mount and cable bar, latest frame, stack, QA/drift table, dark library, safety, log); optional bearer token for commands. CLI: `serve / run / status / stop / resume / park / roof open|close / safety / darks / check`. |
| **Status page v2** | The worker publishes its current *action* (`st["action"]`: kind `slew / centre / dither / expose / download / dark / park / roof_close`, target, frame i/n, solve iteration and last offset, exposure start and duration). `statusview.py` turns status + action into the headline, the estimated finish (frames left × (exposure + overhead) + 3 min per target not started, capped at the Sun limit / plan end) and the night timeline (5-min samples of each target's altitude and its limit, max(min alt, horizon at its azimuth), plus the Sun). Every state change, log line, frame, image and monitor tick notifies a `threading.Condition`; `GET /api/events` waits on it and sends a `status` event (coalesced to ~10/s; without the log, dark-library summary refreshed every 10 s), `log` events with only the new lines, and a `ping` every 5 s when nothing changes, so a silent stream is detectable. The page animates the exposure bar from the start time, the duration and the service clock (`time_unix` × `speed`, so the simulator's fast clock works), with no per-second pushes. If the stream errors or goes silent for 12 s it polls `/api/status` every 3 s and reconnects. Stdlib only. |
| **Simulator** | `sim.py`. Mount with a fixed index error (HA, Dec) that a sync hides and the firmware Park forgets again, optional polar error, tracking-rate error, refusal to track below the horizon, a firmware Park that goes the wrong way when far from home, gotos refused and counted, optional MoveAxis start/stop delays per axis (`axis_delays`) and a switch for whether an RA MoveAxis adds to or replaces tracking. Telephoto star field from the true pointing, with temperature-dependent dark current and hot pixels. Wide camera (camera 1, own focuser): a sparse star field at the true pointing and wide scale, optional vignetting, its own exposure state and an ImageArray transfer time; a wide solver that reads the truth and can be told to fail. Filter wheel, focuser, dew switch, dome (opens and closes with a delay; refusal and stuck modes for tests), safety source and a solver that reads the truth. A scaled clock runs it faster than real time. `tests/` cover the cable guard, safety → park, constraints, plans, the dark library and the API. |

**Not in v1:** an all-sky mosaic from the wide frames (they are saved and solved for it, see [Wide camera](#wide-camera-alongside-the-telephoto)); a scheduler that looks beyond tonight or at the Moon and the weather (the v1 scheduler is per night, see [Scheduler](#scheduler)); autofocus; flats; wide-camera centring; re-acquire after an unwind; the SQLite state store; notifications; the Alpaca Telescope proxy.

## Scheduler

`sequencer/scheduler.py`, used when a plan says `[plan] order = "best"`. `order = "sequence"` (the default) is unchanged: steps in order, each to its count, skipped if not observable when their turn comes.

**When it decides:** at the start, after every block, and after a wait. The worker runs a block exactly like a sequence step (`_run_light`: constraints and cable room, slew, centre, lights with the per-frame checks); the only difference is `progress[i]["block_to"]`, where the lights loop stops and sets the step back to *pending* ("block done, N frames left"). A step is *done* when its count is reached. A block that the per-frame checks end early (target set, cable window) but that made progress also goes back to *pending*; one that made no frames keeps *skipped* / *stopped*, so a disagreement between the projection and the worker can't loop.

**Candidates and block length.** A step with frames left is a candidate at t if it passes `target_failures` now (altitude ≥ max(min alt, horizon at its azimuth)) and at least one frame fits before the first of: it drops below its limit (`sets_at`: 5-min samples refined to 10 s), the night ends (`night_window`: the stricter of the plan's Sun / `end_time` and the service night limit), and the RA cable window runs out (`cable_frames`: the step's HA mapped into the window like `Scope.map_ha`, tracking minutes to `ha_u_max` less `cable_reserve_min` and 0.5° for J2000/JNow and dithers). Block = min(frames left, `block_frames`, `block_minutes` worth, frames that fit); a frame costs exposure + `frame_overhead_s` + its share of dither moves, and a block adds 180 s for slew and centring.

**Score** (points, higher wins, ties to the earlier step):

| Term | Points | Why |
|---|---|---|
| priority | 25 × `priority` | operator's preference (default 0) |
| urgency | 40 × (1 − spare / 3 h), clipped to 0…1; spare = time until it sets or the night ends − time its remaining frames need | targets that would otherwise be lost go first |
| soon | 10 / (1 + hours until it sets or the night ends) | tie-break towards setting targets |
| altitude | 20 × (alt − limit) / (culmination − limit), clipped | high / near transit: less air, better frames |
| left | 10 × frames left / count | spreads time; equal targets alternate block by block |
| cable | −30 × the part of the block the cable window cuts off | prefer targets whose block fits in the cable room |

There is deliberately no bonus for staying on the same target: with blocks the point is to interleave. The log has a line per decision with the terms (`scheduler: M42 x10 (score 63: urgency +34, soon +4, altitude +15, left +10); next best M31 41`).

**Waiting.** When no step is a candidate, `next_start()` scans the rest of the night (5-min steps, refined to 30 s) for the first time one is. If there is one, the scope does the guarded park (safest: tracking off, the arm folded, `on_parked` posted so a roof controller knows) and the worker waits in `clock.wait(abort)` slices, checking the night every slice; the action `wait` gives the headline "Waiting for M42 (rises above 25° at 23:40)" (local time in `[site] timezone`; "becomes observable" when the obstacle wasn't altitude). The next block unparks (`on_unparked` first). Stop, unsafe and the night limit (also through the monitor's night guard) end the wait like any other phase. If nothing becomes observable before the night ends, the remaining steps are *skipped* ("not observable again before the night ends", or "night ends before another block fits") and the plan ends normally.

**Projection.** `project(plan, progress, t, ctx)` runs the same choice forward: blocks advance time by their duration, waits jump to `next_start`, until everything is done or the night ends; it returns the blocks/waits, the frames that won't fit and why it stopped. `statusview.schedule()` starts it after the running block (from `block_to`), caches it per progress change and 5 minutes, and puts it in `status()["plan"]["schedule"]` and `/api/timeline` (`schedule`); `eta_unix` is the end of the last block and `eta_cut` says frames won't fit. The page shows the list, a strip of blocks along the timeline and "next ~HH:MM" per step. `check PLAN` prints it offline (cable window from `config.toml`). The projection uses nominal frame times; the real night drifts from it (centring, QA, slower downloads), which is why it is recomputed after each block rather than followed.

## Heartbeat

For a roof controller (or healthchecks.io, Uptime Kuma push monitors, ...) to see that the sequencer is alive and imaging. Config `[hooks]`: `heartbeat_url`, `heartbeat_interval_s` (60), `heartbeat_method` (`POST` default, or `GET`), `heartbeat_while` (`plan` default: only while a plan job runs; `job`: any job; `always`), `heartbeat_id` (the `service` field).

- **When:** at once when an active job starts, then every `heartbeat_interval_s` while active (service clock; the simulator's fast clock speeds it up). After every plan or park job ends: one beat with `event: "final"` and the resulting state, also when `heartbeat_while` would otherwise be quiet. Nothing is sent while idle with `plan`/`job`, so a receiver that wants a dead-man's switch should arm it on the first beat and disarm it on `final`.
- **GET:** the URL as configured, no body and no query (healthchecks.io: `https://hc-ping.com/<uuid>`). Receivers get only "alive".
- **POST:** `Content-Type: application/json`, the body below. Any 2xx is success; the answer body is ignored. Network errors, timeouts (5 s) and non-2xx are failures: counted in `status()["heartbeat"]` (`sent`, `failed`, `last_ok`, `last_error`; the URL is never shown), logged at most every 10 minutes, and never stop or delay imaging.

Payload (version 1; fields may be added, none removed or renamed without a new `version`):

```jsonc
{
  "service": "seestar-commander",     // [hooks] heartbeat_id
  "kind": "seestar-commander", "version": 1,
  "event": "beat",                    // "beat" while active, "final" once after a plan/park job ends
  "seq": 42,                          // per service process, increasing
  "time": "2026-11-09T03:12:00Z", "time_unix": 1794193920.0,   // service clock
  "state": "running",                 // idle | running | stopping | parking | parked | done | stopped | darks | error
  "busy": "run",                      // job in progress: run | park | roof_open | roof_close | darks_build | polar | null
  "headline": "M45 exposure 3/20 (12 / 60 s)",
  "plan": "Example night",            // loaded plan name or null
  "target": "M45",                    // current target while a plan runs, else null
  "mount": {"at_park": false, "tracking": true},
  "roof_wish": "open",                // "open": imaging now, needs the sky; "closed": doesn't need it
  "safe": true,                       // the sequencer's own safety reading (null = not known yet)
  "night_limit": {"sun_limit_deg": -8.0, "latest_end": null, "reached": false, "at_unix": 1794200000.0},
  "plan_eta_unix": 1794199000.0,      // estimated finish of the plan (capped at the night's end) or null
  "expected_end_unix": 1794199000.0,  // while imaging: min(night limit time, plan ETA); else null
  "expected_end": "2026-11-09T04:56:40Z",
  "interval_s": 60.0,                 // next beat due within this (plus a few seconds)
  "sim": false                        // true when sent by the simulator
}
```

Receiving side (suggested): treat the sequencer as imaging while beats with `roof_wish: "open"` arrive within ~2–3 × `interval_s`; a `final` beat, `roof_wish: "closed"` or `mount.at_park: true` means it's done with the sky. `expected_end_unix` says when it plans to be parked at the latest. Missing beats mean the sequencer is gone (crashed, host down): the roof controller's own rules apply; the sequencer never relies on the receiver.

## Wide camera alongside the telephoto

**Why.** The wide camera (6 mm, 32°×54°, 55″/px) sees most of the sky the telephoto is working in. Frames taken during the night, with WCS, give a record of the sky (clouds, satellites, a later all-sky mosaic) at no cost in telephoto time.

**Measured on the SeeStar** (camera-only, Alpaca on the LAN): telephoto and wide exposures overlap without interference in either order (no early ImageReady, identical frame statistics); ImageArray downloads take about 2.5 s per camera and run one after the other.

**Timing.** The constraints are: the mount must be tracking and settled for the whole wide exposure (no dither pulse, re-centre, slew, park), the two downloads must not overlap (one bridge; a shared lock between the two `Imager`s), and the telephoto cadence must not get dead time. So:
- The worker calls `WideRunner.tele_started` right after the telephoto's `StartExposure`. A frame is due once `wide_every` lights have passed since the last wide start on this target. It starts at once if `wide_exposure` ≤ the steady time: the telephoto exposures from this light up to the next mount move (next dither, re-centre or the end of the target's lights), counted as m × exposure (the downloads between those lights are extra margin). Otherwise it waits for a later light where it fits (e.g. 50 s wide with 30 s lights and `dither_every = 2` starts on the light after each dither); if it can't fit between dithers at all, the target gets no wide frames and the log says so once. The mount must read `Tracking`.
- The wide thread sleeps until the exposure ends, polls `ImageReady`, then waits for a download slot: a telephoto exposure is running with at least `download_s + margin_s` (3 + 2 s) left, so the wide download ends before the telephoto's begins. If the telephoto's download still finds the bridge busy it waits (counted as `bus_waits`; the tests require 0).
- Before a dither or re-centre, `quiet()` aborts a wide exposure still running (only if the assumption above failed) and keeps the wide thread off the bridge until the move and settle are over; a finished frame waiting for its download survives the dither.
- At every end of a target's lights a running wide exposure is aborted (tracking may go off or the mount slews next). On a normal end a finished frame is downloaded right away (the worker waits a few seconds, once per target); on stop, unsafe, the night limit, an error, a park or a scope lock it is dropped.
- Header reads (temperature, focuser) happen right after the download, before the worker may move on; saving, the thumbnail and solving are file work only.

**Solving.** `ProjectionsSolver` runs the projections blind solver (`node scripts/solve-blind.ts` on the green-superpixel 16-bit PNG, as `tools/solve_frames.py` does) in its own thread, then samples the solution on a grid (`tools/solution_grid.ts`). The header gets a local TAN fitted to the grid within 30 % of the short side of the centre (`WCSAPPRX = T`); a 50° field is far from TAN, so the sidecar `<frame>.wcs.json` keeps the centre, the TAN, the pixel → sky grid (green px, J2000) and the name of the solver's own solution (`<frame>.solution.json`, quaternion + radial lens model). A backlog of more than `solve_backlog` frames skips solving the newest; errors and timeouts are logged, counted on the status page and never touch imaging.

**Not in this step: the all-sky mosaic.** Planned as a separate job over a night's `wide/` folders: per frame, the solution's lens model (or the grid) maps pixels to J2000; frames are flat-fielded for the vignetting (corners ~46 %, a master from the frames themselves or twilight), background-matched, and reprojected onto an all-sky grid (equirectangular/HEALPix, or alt/az at the frame's `DATE-AVG` for a horizon-referenced view like the horizon pipeline's `stitch.py`). Everything it needs is in the FITS (raw data, times, site, mount) and the sidecars.

**Still to check on the sky:** that the wide download inside a telephoto exposure doesn't change telephoto frames (the camera-only test said no); real download times while the mount tracks; the solver's success rate on 10 s frames at gain 100 through the vignetting; the wide gain/exposure that keeps the sky background reasonable in moonlight.

## Pointing memory

**Problem.** On the sky the first solve after every unpark was about 100′ off: a constant +1.5–1.6° in Dec plus −0.5…−1.3° in RA. The firmware `Park()` resets the mount's model, so the previous `SyncToCoordinates` is lost. Part of the RA error depends on HA: the polar axis is 1–1.5° off.

**Data.** Each successful centring solve goes to `data/pointing/solves.jsonl`: time, target, the mount's reported HA/Dec (JNow, read right after the frame; with tracking on it's what the mount said during it), the solved frame centre (ICRS → FK5 of date, as HA at the same instant), and the offset solved − raw. **Raw** is the reported position minus the **sync shift**: what the firmware's syncs currently add. The service tracks that shift (`data/pointing/state.json`): it is zero whenever the mount is parked (seen at unpark, after a guarded park, at service start), and every `SyncToCoordinates` adds (synced-to − reported) in HA/Dec. That is what makes solves taken *after* a centring sync usable, and the stored offsets all refer to the firmware's unsynced frame. If the shift isn't known (the service started with the scope unparked and no saved state), nothing is logged or corrected until the next park.

**Applying it: an offset on the commanded position, not a sync.** For each target's first slew the service evaluates the model at the target and commands the mount to `target − (model − shift)` (RA + dH, Dec − dD) with a 0.05° axis tolerance, then centres as before (solve → sync → re-centre). Why not sync right after the unpark:
- after `Unpark()` the scope sits folded at Dec −85° below the pole, where a fitted model's tan δ terms are meaningless and the firmware's behaviour on a sync there is unknown; a sync at the target instead would need an extra slew and gives nothing the offset doesn't;
- the offset keeps the firmware frame raw until the first real solve, so the logged data stays clean, and the sync that follows is a few arcmin instead of 1.5–2°;
- it works the same for every target, not only after a park: after a centring sync the shift equals the error at that target, so the next target's correction is the *change* in error between the two (the HA-dependent part, once the model is fitted);
- centring's fine moves and the polar tool's moves are never offset (their own sync / solves define where they are).
Corrections over `max_correction_deg` (10°, the same limit as for syncs) aren't applied.

**Unwrapped-HA bookkeeping.** `data/ra_unwrap.json` follows the *reported* HA. A sync moves the reported HA by its HA delta without moving the axis; a park that drops the sync moves it back. `ha_unwrapped()` takes either jump in on its next read (they're < 10°, far below the 180° it can resolve), so the bookkeeping stays continuous; its relation to the mechanical axis angle is off by the sync shift (1–2° here), which `[mount] limit_margin_deg` (5°) covers. The pointing correction itself never touches the bookkeeping: it only changes the commanded position, and `move_radec` maps it into the cable window like any target.

**Model.** offset = true − raw, h = HA, d = Dec:
- dH = IH + CH sec d + NP tan d − MA cos h tan d − ME sin h tan d
- dD = ID + MA sin h − ME cos h

IH/ID are index offsets, MA/ME the polar axis azimuth (+ = east of the pole) and elevation (+ = too high) errors with the same signs as the polar tool's az/alt, CH/NP collimation and non-perpendicularity (optional; they need solves spread in Dec). The HA rows are weighted as sky arc. The fit is used once there are `min_solves_for_fit` solves over `min_ha_span_deg` of HA and it's full-rank; otherwise the median of the latest `constant_window` solves. Solves older than `max_age_days`, or from before the last polar-alignment **Done**, are ignored. Refraction isn't modelled (it shows in the rms, about 0.3′ in the simulator). Turning the az knob also turns the HA zero point, so IH absorbs part of a polar az error; that's expected. `/api/status` has `pointing` (model, terms in arcmin, rms, residuals of the latest solves, sync shift, last correction), and the status page a *Pointing model* card.

**Relation to the polar tool.** `polar.py` measures the RA axis directly (three solves while only the RA axis turns) to adjust the wedge; the pointing model's MA/ME estimate the same error indirectly from centring offsets over a night, without a dedicated measurement. They're kept separate on purpose: the polar tool is the instrument for adjusting, the model is for pointing. MA/ME from a fitted model are a cheap cross-check of the last polar measurement (and of whether alignment has drifted), and a Done in the polar tool restarts the model because the old MA/ME no longer apply.

**Still to check on the sky:** that a SeeStar sync behaves as a constant HA/Dec offset (the next target's solve should match model − shift), and that `Park()` really drops it every time (the first solve after an unpark with the memory switched off should show the full ~100′).

## Dithering

**Why.** The lights are unguided, uncalibrated (when no matching dark exists) 60 s subs with hot pixels; drift (7–20″/min from a 1–1.5° polar error) moves the field slowly and in one direction, so hot pixels leave trails in the stack instead of being clipped. A dither moves the field by a random 10–20 px between frames, so each hot pixel lands on a different stack pixel and the sigma clipping drops it.

**How small moves are made.** A dither is tens of arcseconds (15 px × 3.74″ ≈ 56″). The closed-loop `_axis_to` can't do that reliably: its fine stage is 0.05 °/s without compensating the MoveAxis delays (measured on one S30 Pro, from the call to the motion: RA start 0.28 s / stop 0.26 s, Dec 0.23 s / 0.12 s, spread 2–3 ms, rates exact to 0.15 %), each position read takes about 0.25 s, and it turns tracking off. In the simulator with those delays it left 10–20″ errors at an 18–36″ tolerance and hunted without converging at 11″ (40 iterations, ~50 s). So a dither is one **timed pulse per axis with tracking on** (`Scope.pulse_axis`): `[dither] rate_deg_s` (0.01 °/s = 36″/s) relative to the sky, held for move / rate + the axis lead (start minus stop delay), timed from sending the command. At that rate a few ms of timing error is well under 1″. Details:
- **RA never reverses** (as in ST-4 guiding): a move against the tracking direction runs at most `ra_no_reverse` (0.9) × sidereal, so the axis only slows down and gear backlash doesn't come in. A move with it runs at the full rate.
- **Tracking interaction.** `ra_moveaxis = "adds"` assumes a MoveAxis rate on RA adds to sidereal tracking (the ASCOM convention); `"replaces"` adds sidereal to the commanded rate. Tracking is switched back on if it reads off after the pulse.
- **Dec** reverses; `dec_backlash_as` is added to a pulse that changes direction (0 until measured).
- **Safety.** Before each pulse the axis held at its rate (plus tracking) for `max(pulse, runaway_s)` (60 s) must stay inside the cable window (RA) or the Dec range, otherwise the pulse is refused and the dither skipped (the frame's own cable check then decides about the target). The pulse polls the interrupt (stop, unsafe) and the scope lock and stops the axis on either; settling is interruptible. Dithering runs inside the plan job, so the scope lock and the file lock already exclude other users. Firmware gotos are never used.

**Pattern.** Positions are uniform inside a disc of radius `dither_px` around the **centred position** (the commanded offset is tracked, so it's not a random walk), each at least half the radius from the current position and the last six (best of 64 candidates), seeded by the target name. A disc is used instead of a spiral because it needs no state beyond the offset, every dither is a decent move in both axes, and repeats are avoided by the separation rule. Offsets are chosen on the sky (east, north) and converted to axis angles with 1/cos δ for HA.

**Measuring it.** Each frame's registration offset against the stack reference (phase correlation, the same one the stack uses) is booked against the commanded offset (`dither.Response`). Between consecutive frames, measured change = A · commanded change + drift · Δt is fitted by least squares once there are 3 dithers in 2-D; with the centring solve's orientation (CD matrix) as A₀, K = A₀⁻¹A gives the achieved/commanded scale for east and north (1.0 = as commanded; the status page shows it). Before the fit, A₀ predicts each move and the log says how far the measurement was from it. The drift rate shown for a dithered target is computed with the dithers taken out. Frame log: `dither_n`, `dither_e_px`/`dither_n_px` (commanded), `dither_x_px`/`dither_y_px` (measured). FITS: `DITHN`, `DITHRA`/`DITHDEC` (commanded offset, ″ east/north of centre), `DITHERX`/`DITHERY` (measured image shift vs the reference frame, full-resolution px, including drift).

**Re-centring.** Drift still carries the field away from the target. `dither_recentre_every = N` replaces every Nth dither with a plate-solve re-centre (sync + closed-loop fine move, tolerance min(centre tolerance, 1′)); `dither_recentre_px` re-centres once the drift measured since the last centring exceeds that many px. Both need `centre = auto/solve` and a solver; after a re-centre that moved, the offset is zero again and the response starts a new segment.

**Stack.** Registration handles offsets of tens of px (it's full-frame phase correlation; the simulator checks it to ~0.3 px). Later frames' hot pixels are clipped by the 3σ rejection; hot pixels in the first three frames are dropped when the statistics are rebuilt from them (see Pipeline). In the simulator, without darks and without drift, the excess left in the stack at the brightest hot pixels drops from about 9 800 counts (no dithering) to under 1 count (7 frames, dithered every frame); before the first three frames were rebuilt it was about 1 400 (one in seven of the first frames' hot pixels stayed in the mean). With the T1 delays the simulated dithers land within 0.1–0.7 px of the prediction, and the fitted east/north scale is 0.99/1.00.

**Still to check on the sky:** that `0.01 °/s` is honoured at that low rate and whether an RA MoveAxis adds to tracking (sattrack T4); the delays at that rate (`ra_lead_s`, `dec_lead_s`); Dec backlash (the east/north scale and the per-dither "off the prediction" in the log show it); that tracking is still on after a pulse; settle time (HFR/ecc of the frame after a dither vs the others).

## Unwind and re-acquire

`sequencer/unwind.py` (window maths, pure), `Service._unwind` (the moves), step options `unwind`, `unwind_min_frames`, `unwind_min_minutes`, `unwind_max_wait_min`.

**When.** The per-frame cable check in the lights loop finds less than one frame plus `cable_reserve_min` of tracking left before the CW end (`ha_u_max`). Instead of stopping the target it asks whether a turn back pays.

**Window maths.** u is the target's unwrapped HA (tracking: the axis follows it at r = 0.2507 °/min). The same sky position is at u − 360k for k = 1, 2, … (same axis angle mod 360, so the same field orientation; no meridian flip on a wedge). For each k:

- gap = max(0, `ha_u_min` + 2° − (u − 360k)): how far the target still is below the CCW end. With a window of W degrees, gap ≈ 360 + 2 − W + (frame + reserve)·r, so it's zero only if W > ~363°. The default −175…+175 (W = 350°) gives gap ≈ 13°, i.e. a wait of gap / r ≈ 50 min for 60 s frames. A k whose wait exceeds `unwind_max_wait_min` is out (and so is every larger k). **`unwind_max_wait_min` defaults to 0**: on a window narrower than ~363° nothing changes until the operator opts in with a wait; only a wider window unwinds by default.
- move time = |u − (u − 360k + gap)| / 5 °/s + 20 s (the fast stage of `_axis_to`; ~90 s for a full turn), arrival = max(move, wait) + 120 s of centring, the target then at u_a = u − 360k + r·(arrival − now).
- room = (`ha_u_max` − u_a)/r − reserve; frames that fit = min(left to do, room // (exposure + overhead)), then cut to those whose end the sky allows (altitude/horizon of the target, the plan's Sun/end time, the service night limit).
- Choice: the fewest turns (then shortest wait) that fits all remaining frames, else the option with the most frames. Not worth it (fewer than `unwind_min_frames` frames or `unwind_min_minutes` of imaging), no option, or `unwind = false`: the target stops as before, tracking off, with the reason in the step.

**Moves.** All through `Scope` with the persisted unwrapped HA. `map_ha`/`move_hadec`/`move_radec` take `near=`: the unwrapped angle of the HA nearest that value, `None`/refused if it's outside the window, which is checked before anything moves (both ends inside the window means the monotonic path between is too; `_ha_guard` is the backstop during the move). If the target isn't inside yet, RA goes to `ha_u_min` + 2° with tracking off and the worker waits there (interruptible, the night guard and unsafe abort it like any job). Then `_goto(correct=True, near=…)` to the target (pointing memory offset, tracking on), and `_centre` (plate solve + sync + fine moves). Centring fine moves and blob nudges now pass `near` = the current angle, so on a window wider than 360° they can't jump to the other turn. The wide camera is kept quiet during the whole unwind, like a dither. Stop, unsafe, the night limit and a lost scope lock stop the axis (`Scope._check_interrupt`) and end the plan as for any move.

**With `order = "best"`.** The unwind never waits inline (the wait is treated as 0 whatever the step says): a long idle wait would block the other targets. If only a waiting unwind would do, the block ends at the limit with tracking off and the step goes back to `pending`; the scheduler picks something else and returns to it once its HA has crossed the gap (then it's inside the window and the ordinary slew takes the long way round; no unwind needed). A zero-wait unwind (window > ~363°) is done inline, and `scheduler.cable_frames` counts one extra turn of tracking for such a step so that blocks aren't cut at the limit.

**After.** The step's `centre` is the new one, the dither state starts a new segment with its offset at zero (dithering continues around the new centre), the stack keeps its reference (same field, translation only). The step counts `unwinds`; the log says `UNWINDING CABLE: RA axis u -> u' …` and `unwound to unwrapped HA …`.

**To check on the sky:** that a 360° counter-clockwise MoveAxis turn at 5 °/s really leaves the cable relaxed (watch it with the camera the first time), the actual move and re-centre time against the 90 s + 120 s estimate, and that the field orientation is unchanged after the turn (the stack registers by translation only).

## Components

1. **Devices.** Thin Alpaca clients (alpyca) wrapped with timeouts, retries and reconnects. Each device knows its quirks:
   - every SeeStar exposure stops any running view;
   - `frame_calib` stays off;
   - turning tracking on is refused while the scope points below the horizon;
   - the ImageArray download is slow.
2. **Motion: the only path to the mount.**
   - **Never** uses a firmware goto for RA. It moves with closed-loop MoveAxis and tracks the unwrapped HA in persistent state.
   - The cable window is configured per scope; for the SeeStar it's 90° counter-clockwise / 270° clockwise from home.
   - Plans the shortest legal path and refuses anything outside the window. Tracking time counts against the window, with an automatic unwind and re-acquire at the limit (implemented, see [Unwind and re-acquire](#unwind-and-re-acquire)).
   - **Planned :** expose it as **our own Alpaca Telescope device**, a cable-safe proxy in front of the SeeStar. Other clients (Stellarium's telescope control, scripts, any ASCOM software) then get cable safety for free; gotos become legal axis moves.
3. **Sequencer.**
   - **Plan files** (YAML) list targets with exposures, filters, dither, priority and constraints (altitude, horizon file, moon, twilight).
   - **Scheduler:** what to image now, given tonight's horizon and the time.
   - **Calibration jobs:**
     - darks, with the wheel on Dark, matched in exposure, gain and temperature, before and after each block;
     - twilight sky flats, with exposure servo;
     - bias / dark-flats.
   - **Centring:** wide-camera solve with the projections solver, then sync, then telephoto solve to refine (ASTAP), then re-centre.
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


## Implemented: polar alignment (v1)

`sequencer/polar.py`, job `polar` on the worker (exclusive with plans; stop, park and unsafe end it), CLI `polar start|stop|status|done`, API `/api/polar…`, phone page `web/polar.html` at `/polar`. Measure: telephoto solves at three RA-axis angles (current HA ±30°, Dec 65, cable-checked `move_hadec`, RA only), apparent directions → plane fit → error against the refracted pole in alt/az with words, plus a fit residual from the encoder angles. Adjust: tracking on at the last point; each solve gives the knob rotation (az about the vertical, alt about the hinge), the remaining error and a star-to-reticle target with a tolerance circle; Done stores the result in `data/polar/results.jsonl`. `[polar]` config, including `alt_knob_sign` / `az_knob_sign` once the knob directions are known. The simulator has a polar-misalignment model (`polar_error`, refraction, camera roll) and a knob hook (`/api/sim/polar`). Differences from the plan below: the telephoto (ASTAP) is the solver, not the wide camera; there's no SQLite state store yet; the ongoing drift check (4.) is not done. See [polar-alignment.md](polar-alignment.md).

## Planned: polar alignment tool with a mobile web UI 

Goal: you stand at the wedge with a phone and turn the alt/az knobs while the page tells you which way and how far, ideally by moving a star onto a reticle.

1. **Measure: three-point polar alignment.**
   - Plate-solve at three RA positions, moving only the RA axis. That's cable-safe, within the window, and spans for example ±30°.
   - The plane fitted through the three solved directions gives the true RA axis. Its offset from the refracted celestial pole is the polar error, split into altitude and azimuth.
   - Solver: wide camera with the projections solver (fast, large field, robust), with the telephoto (ASTAP) for a fine check.
2. **Adjust: live loop.**
   - Stay at the last position and solve continuously, a few seconds per frame, while the knobs are turned.
   - Update the residual error after each solve. The RA axis itself doesn't move during adjustment, so each new solve says where the axis now points.
3. **Phone UI** (served by the on-site daemon, works over Tailscale or the site WiFi):
   - **Big numbers:** altitude error and azimuth error in arcminutes, with arrows such as "raise", "lower", "turn left" and "turn right" (screw directions calibrated once per wedge), colour-coded to the target tolerance.
   - **Reticle view:** a crop of the live frame around a bright star, with a reticle where that star must go when the error is zero. Turning the knobs moves the star onto the reticle, and the size of the error circle shows the tolerance.
   - **History:** a small trace of error against time, so overshoot is easy to see.
   - **Finish:** a "Done" button stores the final error and timestamp in the state store. The drift monitor later reports whether alignment still holds.
4. **Ongoing check:** the frame-to-frame drift measured during imaging feeds a running polar-error / pointing-model estimate, which flags when alignment has changed (for example after a cold night or a bump).

