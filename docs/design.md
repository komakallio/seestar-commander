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

