# Satellite tracking: design

Status: **design, Phase 1 (measurements) in progress.** Phase 2 (the tracker) is not started.

Goal: follow satellites with the SeeStar on an equatorial wedge using only Alpaca `MoveAxis`, keep the target in the 4.0° × 2.2° telephoto field, and record it. Everything here is generic; site settings (Alpaca host, cable window, elements source and key, controller host) live in `config.toml` or the operator's private repo.

## 1. Constraints that shape the design

| Constraint | Consequence |
|---|---|
| Alpaca has no trajectory command; `MoveAxis(axis, rate)` holds a rate until the next call | The tracker is a rate staircase: each step commands the **average** planned rate over the coming interval. Step error ≈ α·Δt²/8, a few arcseconds at Δt = 0.5 s |
| MoveAxis 0–6 °/s (claimed) | Passes needing more on either axis are rejected. The limit used is the **measured** one (T2) minus a margin |
| Equatorial mount: RA rate ∝ 1/cos(Dec) | Passes near the celestial pole are rejected by rate, not by a separate rule |
| Cable window on the unwrapped HA; firmware gotos ignore it | The whole pass path is placed in the window **before** it starts, and every rate command is checked against it (§4). No firmware gotos, ever |
| Dec MoveAxis stops near −84.6° (arm fold) | Path must stay inside a configured Dec range with margin |
| One process owns the mount and `ra_unwrap.json` | Bench tools refuse to run while the sequencer is active; later the tracker runs as a sequencer job |
| **The link to the mount is Wi-Fi and the operator's link to the site drops now and then** | §3: everything runs on the controller host on the scope's LAN, detached from the operator's session, with a runaway guard that holds even if commands stop arriving |
| Elements are mostly wrong along the track | Optical outer loop corrects timing (if a video stream exists, T5); otherwise rely on the 4° field. Prefer operator ephemerides (SupGP) when available |
| Image download is seconds per frame | Not usable for feedback. Feedback is mount position (inner) and video (outer, optional) |

## 2. Components

New package `sattrack/` (pure Python, testable against `sequencer/sim.py`) and one bench CLI.

```
sattrack/
  elements.py   fetch + cache elements (TLE/3LE or OMM JSON); offline from cache
  geometry.py   SGP4 (Skyfield) -> topocentric apparent RA/Dec of date (JNow) -> HA/Dec,
                unwrap, branch choice inside the cable window, rates, accelerations
  passes.py     find passes, sample at 10 Hz, apply constraints, verdict per pass
  guard.py      cable / Dec / rate / runaway checks shared by planner, bench and tracker
  runlog.py     append-only JSONL run log + status.json
  follow.py     (Phase 2) controller step: plan + measurement -> rate commands
tools/
  sattrack_plan.py   T0 CLI: list passes and verdicts, write plan JSON
  axis_bench.py      T1-T6 hardware tests (subcommands), all through guard.py
```

Dependencies: add `skyfield` (SGP4 via `sgp4`). Astropy stays for LST elsewhere; geometry uses Skyfield throughout so the frame is consistent.

### 2.1 Elements

- Source configured in `[sattrack]`: base URL, the environment variable that holds the key, user agent. A plain local TLE/OMM file is also accepted.
- Cache in `data/sattrack/elements/<norad>.json` with the epoch and fetch time. Bench and tracker runs **only read the cache**; fetching happens before a run. A run refuses elements older than a configured age (default 24 h for LEO, 7 d for GEO).
- Fetched elements are not committed (licence of the source).

### 2.2 Geometry and the mount frame

1. Skyfield topocentric position → apparent RA/Dec of date (true equator and equinox of date, the mount's JNow). Refraction **off** by default, switchable: the mount model is fitted by plate-solve syncs, which already absorb mean refraction near where they were taken; T6 and the first sky runs decide.
2. HA = LAST − RA. Sampled every 0.1 s over the pass.
3. Unwrap HA along the pass (`numpy.unwrap`), then choose the 360° branch k that puts the **whole** path, plus margins, inside the window `[ha_u_min, ha_u_max]`. The current unwrapped HA sets which branch is cheapest to reach (pre-position cost); ties go to the branch with more margin.
4. Pointing correction: apply the current sync offset (the sequencer's latest plate-solve sync) so the plan is in mount coordinates. A small constant offset; no full pointing model in Phase 1.
5. Rates and accelerations per axis by central differences; primary axis in HA degrees (positive = increasing HA, the verified direction), secondary in Dec.

### 2.3 Pass verdict

Each pass gets: rise / culmination / set (above the horizon file, not just 0°), peak altitude, peak |rate| and |accel| per axis, min distance to the pole, unwrapped HA span and margin to both window edges, Dec range and margin, illumination (sunlit satellite, dark site) and a verdict: `ok`, `trim` (an inner segment is trackable: start late / stop early), or `reject` with the reason. Output: a table and `plan-<norad>-<start>.json` with the 10 Hz samples.

## 3. Running on a flaky network

The operator's VPN link to the site drops now and then, and the controller-to-mount link is Wi-Fi with tens of milliseconds of jitter. Rules:

1. **Run on the controller host on the scope's LAN.** The operator's machine only starts runs and reads results. Latency numbers measured over the VPN are meaningless.
2. **Detached.** Every hardware run is started as a transient systemd unit (`systemd-run --unit=sattrack-<test> --collect`), never in the SSH session. A dropped SSH session does not stop the run. A hard `RuntimeMaxSec` caps each test.
3. **Cleanup is not left to the script.** The unit's `ExecStopPost` runs the guarded park and restarts the sequencer, so a crash, a timeout or a kill still leaves the scope parked and the sequencer running. (The unit file is site-specific; a template goes in `docs/`.)
4. **Self-contained.** Elements are cached before the run; the run never needs the internet. The controller's clock must be NTP-synced (checked at start: 100 ms of clock error at 1 °/s is 6′ along the track).
5. **Logs survive interruptions.** `runlog.py` writes one JSON line per Alpaca call (monotonic and UTC time at send and return, call, value, error) and flushes each line. `status.json` says what is running. Results are fetched afterwards with rsync.
6. **Stop on a bad link.** If a position read fails or takes longer than a threshold (default 1 s), the controller commands both axes to 0 and retries the stop until it is acknowledged, then aborts the run.
7. **Measure the link.** A 1 Hz reachability logger (mount, gateway, internet) runs on the controller host during Phase 1 so the outage lengths are known; they set the runaway horizon below.

## 4. Safety: the runaway guard

Alpaca HTTP has no keepalive. If commands stop arriving (Wi-Fi drop, controller crash between a rate command and the next), the mount presumably keeps the last commanded rate. Whether the firmware times out a MoveAxis by itself is unknown (measured in T2). Until it is known:

- **Every** non-zero `MoveAxis` passes `guard.check_rate(axis, position, rate, T_run)`: continuing at that rate for `T_run` seconds must stay inside the cable window (with margin) and the Dec range. `T_run` defaults to 30 s and is raised if the link log shows longer outages.
- At 1 °/s that is a 30° margin on HA. This trims the end of long passes near a window edge; the planner applies the same check to every sample so the verdict already reflects it.
- The pre-position move and the post-pass return use the existing `Scope` closed-loop moves.
- Ctrl-C / SIGTERM handler: both axes to 0, then exit (the unit then parks).
- Every bench test starts and ends parked at home, with the planned excursion computed and logged first.

## 5. Phase 1 tests (bench)

All through `tools/axis_bench.py`, roof closed, real pass geometry from the planner where relevant.

| # | Subcommand | Measures | Notes |
|---|---|---|---|
| T0 | `sattrack_plan.py` | Trackable passes, peak rate/accel | Offline |
| T1 | `latency` | Call round-trip; delay from MoveAxis to motion start/stop; position read rate, delay and jitter | 20 repeats, small moves around home |
| T2 | `ramp` | Rate change while moving (ramp or stop?), acceleration, real top speed, **whether a MoveAxis times out on its own** (hold a rate 30 s with no other calls, then read) | The no-traffic hold is near home with a large margin, at a low rate |
| T3 | `dual` | Both axes moving at once | |
| T4 | `fine` | Rates 0.001–0.05 °/s with tracking on/off; tracking state after `MoveAxis(axis, 0)` | Absolute vs added to sidereal |
| T5 | `video` | RTSP probe of the reported preview ports; resolution, fps, delay | No motion except for the delay check |
| T6 | `replay` | A real pass followed with feedforward, then feedforward + position feedback, at 1, 2, 4 Hz | Main result |

Results and plots go in the operator's private repo, not here; this doc gets the generic findings (as with the Alpaca notes).

## 6. Phase 2 sketch: the follower

Inputs: the 10 Hz plan, the measured command delay `d` and position-read delay (T1), the chosen step Δt (T6).

Each step at time t:

1. Read the axis position; timestamp it at the midpoint of the read calls (two HTTP calls; their gap is logged).
2. Error e = plan(t_read) − measured, per axis.
3. Rate = mean planned rate over [t + d, t + d + Δt] + e / τ (τ a few Δt), clamped to the rate limit.
4. `guard.check_rate`; send.
5. Optical outer loop (if T5 finds video): detect the satellite, estimate the along-track time offset and the cross-track offset, fold them into plan(t) as a time shift and a small offset.

Jitter budget: with tens of milliseconds of Wi-Fi jitter, wander is about rate × jitter: 50 ms at 1 °/s is 3′, well inside the field. A 2 s outage mid-pass leaves the last rate running: error about α·t²/2 ≈ 0.1°, also inside the field. Longer outages are what the runaway guard is for.

Integration: a `sattrack` job type in the sequencer (it owns the mount and the unwrap file), pass list and live error on the status page. First sky targets: a geostationary satellite, then the ISS on a high pass away from the pole.

## 7. Open questions

- Does a MoveAxis rate persist forever without traffic (T2)?
- Does MoveAxis replace or add to sidereal tracking (T4)?
- Is there a usable video stream (T5)? It decides whether the optical loop exists.
- Refraction on or off in the plan (first sky runs).
