# Panorama grid optimisation (offline)

2026-11-08. Design and simulation only. Nothing here talks to the SeeStar.

**Conclusion:** use **3 HA/Dec strips with 32 frames** (`tools/plan_optimal.py`, default strategy `hadec`) instead of the current 52 frames in alt rows.
- Coverage is still 100 % from alt −10° to the zenith, with at least 4.7° overlap everywhere.
- Estimated time drops from **54 → 29 min at night** (20 s exposures) and from **37 → 18 min in daytime** (0.01 s exposures), about **−47 %**.
- It uses the same cable range as the first measured run (2026-11-08): −77° (CCW) / +257° (CW) from home.

![footprints](docs/grid-optimization.png)

*Allsky, N up, E left. Colour gives capture order, from dark to light. Left: current plan. Right: the 3-strip plan, with the low strip at Dec −17, the middle strip at Dec +29 and the polar cap at Dec +67.*

## Comparison

All plans start from home (HA −90, Dec −90) and are checked on a 0.5° Fibonacci sky grid.
- **Band cells** show the share of each band covered with the required overlap, then the minimum overlap in that band.
- **min overlap** is 2 × the worst edge margin. At the bottom band it also measures how far the frames reach below alt −10°.
- **Time models:**
  - *meas.* is fitted to the 2026-11-08 `capture.log`.
  - *nominal* is the brief's model: 3.5 s per move + |dHA|/5 + |dDec|/5 + exposure + 10 s download.

| plan | frames | min overlap | −10..0 | 0..15 | 15..30 | 30..50 | 50..70 | 70..90 | night (meas.) | day (meas.) | night (nominal) | day (nominal) | largest RA move | RA extremes vs home |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| current alt rows (plan_grid) | 52 | 7.3° | 100% / 7.3° | 100% / 10.3° | 100% / 10.6° | 100% / 12.0° | 100% / 11.7° | 100% / 10.6° | 54.3 min | 37.0 min | 35.4 min | 18.1 min | 86° | −77° / +257° |
| optimal alt rows | 36 | 5.0° | 100% / 5.7° | 100% / 8.3° | 100% / 6.0° | 100% / 7.8° | 100% / 5.0° | 100% / 8.5° | 37.8 min | 25.8 min | 24.7 min | 12.7 min | 119° | −76° / +256° |
| free set cover (MILP, 60 s) | 32 | 3.3° | 100% / 4.0° | 100% / 7.0° | 100% / 3.3° | 100% / 4.0° | 100% / 4.5° | 100% / 6.1° | 41.1 min | 30.5 min | 28.8 min | 18.2 min | 295° | −85° / +260° |
| **HA/Dec strips (recommended)** | **32** | **4.7°** | 100% / 5.0° | 100% / 4.8° | 100% / 4.7° | 100% / 4.7° | 100% / 5.3° | 100% / 5.1° | **28.6 min** | **18.0 min** | 21.6 min | 10.9 min | 56° | −77° / +257° |
| HA/Dec strips, overlap 3° | 31 | 3.4° | 100% / 4.3° | 100% / 3.9° | 100% / 3.5° | 100% / 3.4° | 100% / 4.0° | 100% / 3.6° | 27.7 min | 17.4 min | 20.9 min | 10.6 min | 75° | −77° / +257° |

Sensitivity of the strip plan:
- alt_min −5° → 31 frames, 27.8 / 17.5 min.
- alt_min 0° → 28 frames, 25.6 / 16.3 min.
- A cable window of ±130° (or narrower) has **no** solution. Something must reach HA ±180 from one side, so another pier needs either ≥ ~±170° of RA travel or Dec-over-the-pole pointings (see below).

## Why strips win

- **Fewer Dec moves.** The camera is fixed to the Dec axis, so a constant-Dec strip moves **only the RA axis** between frames. The log fit shows each axis that moves costs about **9.5 s of fixed closed-loop overhead** (the 5 → 1 → 0.3 °/s approach legs, 0.8 s pauses and Alpaca round trips), plus about 0.28 s/°. In alt rows both axes move for every frame.
- **Tiling follows the camera.** Frames in a strip keep the same orientation in HA/Dec, so they tile like bricks along the RA direction. In alt/az the same frames rotate with the parallactic angle, and the rows need extra overlap to cover the wedges.
- **Three Dec strips cover the whole region.** One frame's long side is 54°, and the region alt ≥ −10° spans Dec −40 to +90:
  - Low strip, Dec −17, 10 frames across HA ±111°.
  - Middle strip, Dec +29, 14 frames all round.
  - Polar cap, Dec +67, 8 frames all round.
- **Near the pole the frames become spokes.** Their long sides point at the pole and their far ends cross over it, which fills the cap.
- **Free set cover gains nothing here.** Its LP bound is 26 frames. Every free-cover frame needs a Dec move, though, so even 26 frames would take about as long as 32 strip frames. HiGHS also doesn't converge within minutes, and its 32-frame incumbent orders badly.
- **Order and cable seam.** The low strip is swept −111 → +111 in HA, the middle strip +167 → −167, and the cap −157 → +157.
  - Each strip is one monotonic sweep in unwrapped HA inside [−175, +175], so no move crosses the seam.
  - The largest RA move is 56°, at the change from the low strip to the middle strip. The extremes are the same as the first measured run (2026-11-08).

## Measured timing (first run, 2026-11-08, `data/pano/capture.log`)

- The 52 frames took 54.5 min of wall time, about 63 s per frame. The `measured` model predicts 54.3 min.
- Fit: frame time ≈ 33.5 s (20 s exposure + ~2 s readout/FITS + 10 s download + 1.5 s settle), plus 9.5 s and 0.28 s/° for each axis that moves. Residual is 3.9 s (n = 51).
- Frame #38 moved RA only and took 45 s; its neighbours took about 60 s.
- The brief's nominal model (3.5 s per move, 5 °/s) underestimates the first measured run (2026-11-08) by about 19 min.

## Ideas to speed it up further (not implemented)

Gains are estimated on the 32-frame plan as night / day:

1. **Cut the closed-loop approach overhead** (9.5 s → ~4 s per axis), in `scope.py._axis_to`, no hardware change. Saves about 3 min (25.4 / 14.7 min).
   - Loosen the tolerance from 0.3° to ~1°: with 4° overlap, pointing error of 1° doesn't matter.
   - Drop the 0.3 °/s leg.
   - Predict the stop point from a measured deceleration.
   - Drop the 0.8 s pause before each read.
2. **Faster download** (10 s → ~3 s). Saves about 4 min (24.9 / 14.2 min). Options:
   - run the capture on a host on the scope's LAN, not over a VPN and WiFi;
   - make sure ImageBytes (binary) transfer is used rather than JSON ImageArray;
   - for daytime, use JPEG frames from the JSON-RPC image socket (4804).
   - Items 1 + 2 together: **21.7 / 11.0 min**.
3. **Overlap download with the next slew.** Fetch the ImageArray in a thread while the RA axis moves to the next frame. In daytime this hides most of the 10 s download behind the ~16 s move.
4. **Daytime RTSP wide stream (port 4555).** Take a still from the live stream as soon as the mount settles: no exposure and no download, so a frame costs only the slew. That makes the 32 frames a ~10 min job. The cost is lower quality (compressed, 8-bit); scenery and horizon tracing will likely accept that, but the night plate-solving pass won't.
5. **Simultaneous axis moves.** Within a strip only RA moves, so this only helps the 3 strip changes; it matters for alt-row plans.
6. **Binning:** not available (MaxBin 1).
7. **Dec over the pole.** HA h, Dec d is the same pointing as HA h+180, Dec 180−d.
   - This would keep RA travel within about ±90° of home and make narrow-cable piers feasible.
   - It needs checking that the SeeStar Dec axis goes past 90.
   - The frame orientation flips by 180°, which the stitcher doesn't mind.
8. **Raise alt_min** where the walls block everything below some altitude anyway: alt_min 0° gives 28 frames.

## Caveats

- **Lens model.** The lens is treated as rectilinear 32.1° × 54.2°. Mild barrel distortion usually adds sky at the corners, so the plan is conservative. A plate-solved FOV or distortion can be fed in through `pano_sim.Camera`, which takes the FOV and a `--roll` value.
- **Camera axis.** The long axis is assumed to lie along Dec, as in `CAMERA_LONG_AXIS`. The mount-model fit (`data/pano/guided/mount_model.json`) should confirm the camera roll before relying on 4.7° overlaps. A few degrees of roll would eat into them; `--roll` replans for it.
- **Low strip below the horizon.** At the E/W ends the low strip points well below the horizon (centres at alt −25). That is the cost of keeping Dec fixed. Those frames still carry the E/W horizon at their top edge.
- **Night plate solving.** The cap and middle strip are all sky. The low strip has sky only in its upper part.

## Files

- `tools/plan_optimal.py` is the planner.
  - Strategies: `hadec` (default), `altrows`, `free`.
  - Parameters: `--alt-min --min-overlap --ha-min --ha-max --home-ha --home-dec --exp --download --roll`.
  - `--out` writes a state JSON in panolib format, with `ha_u` set to the unwrapped HA that capture/scope already use.
  - `--install` writes `data/pano/state.json` and has the same guard as `plan_grid.py`.
  - `--compare` regenerates the table and the figure.
- `tools/pano_sim.py` holds the geometry, coverage and overlap checks, and the time models. `--calibrate LOG` refits the timing.
- `docs/plan-optimal-hadec.json` is the recommended 32-frame plan.
- `docs/grid-optimization.png` is the figure.
