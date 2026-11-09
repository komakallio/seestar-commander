# Polar alignment

`python -m sequencer polar start` (or the **Start** button on the phone page `/polar`) measures how far the
wedge's RA axis is from the celestial pole, then keeps solving while you turn the alt/az knobs and shows
what's left. Code: `sequencer/polar.py`. Tested only against the simulator so far (see
[Real hardware: still to check](#real-hardware-still-to-check)).

## Method

**1. Measure (three points, TPPA-style).**
- The scope goes to Dec 65 (`[polar] dec`, or `current`) and plate-solves the telephoto at three RA-axis angles: the current hour angle and ±30° (`span_deg`, `points`).
- Only the RA axis moves between the points, by `scope.move_hadec` inside the cable window. It never uses a firmware goto. If ±span doesn't fit the window, or a point would be lower than `min_alt`, the start is refused and the message says which centre hour angles would fit.
- The last point is the lower end of the sweep. The az knob turns the mount about the vertical, which moves a high field very little, so a lower field gives a better-conditioned adjust loop.
- Tracking is off while it measures, and the frame is kept fixed to the Earth. Each solve (ICRS) is converted to FK5 of date, then to an hour angle from LST at mid-exposure, then to alt/az. Refraction is then added. The result is the *apparent* direction the optics physically point in.
- These directions lie on a circle around the physical RA axis. A plane fit (SVD) through them gives the axis.
- The axis is compared with the **refracted** pole: altitude |latitude| plus refraction, at azimuth 0 (180 in the south).
- **Altitude error:** positive means the axis is too high (**lower** it).
- **Azimuth error:** the arc on the sky. Positive means the axis is east of the pole (**turn west**).
- **Fit residual:** with three points a plane always fits exactly. The residual is therefore the disagreement between the angles around the fitted axis and the mount's own RA-axis angles, in arcminutes on the sky. With more than three points the plane rms is reported too. A residual well above the solve accuracy points to flexure, a slipping clutch or a bad solve.

**2. Adjust (live loop).**
- The scope stays at the last point. By default tracking is on, so stars stay still while you work (`track_during_adjust`); it stays off if the cable window doesn't leave `adjust_timeout_min` plus the reserve. With tracking on, the RA axis rotation since the last point comes from the mount's unwrapped hour angle and is applied to the reference direction about the fitted axis.
- Every solve gives the current direction `v`. Turning the knobs rotates the whole mount by Q = Rz(−b)·R_hinge(a):
  - the az knob turns about the vertical;
  - the alt knob turns about the horizontal hinge perpendicular to the axis.
  - The two knob angles come from Q · v_ref = v (Gauss–Newton). The axis is then Q · axis, and the error is recomputed.
- **Star to reticle:** it picks a bright star near the frame centre and follows it by sky position from frame to frame. The **reticle** is where that star will sit in this frame once the remaining error is corrected. The correction rotation C (knob form) is applied to the camera, so the star appears at C⁻¹·star. The tolerance circle is the tolerance converted into pixels at the current star-to-reticle scale.
- **Ending the loop:** Stop, Done (stores the current error with a timestamp in `data/polar/results.jsonl`), the timeout, or unsafe. On unsafe it aborts and parks, as for a plan. Tracking is switched off at the end. The last measurement goes to `data/polar/last_measurement.json`.

## Relation to the pointing model

The sequencer's pointing memory ([design.md](design.md#pointing-memory)) fits MA/ME (polar axis azimuth and elevation error) from the centring solves of normal imaging, with the same signs as this tool's az/alt. That's an indirect estimate, good as a cross-check of the last measurement or a sign that alignment has drifted; adjusting the wedge is this tool's job. Pressing **Done** here makes the pointing model start afresh, because older solves were taken with the old axis.

## Accuracy notes

- **Simulator:** the simulator includes the polar model and refraction, with 5″ of solve noise. The measurement lands within about **0.3′** in alt and az for errors from 10′ to 1.5°, at Dec 45 and 65. The live loop follows simulated knob turns within about 0.2′, and the tests allow 2–2.5′. These figures show that the geometry is self-consistent. They don't show how accurate it is on the sky.
- **Solve accuracy:**
  - The SeeStar telephoto at about 3.7″/px should solve to 1–2″. Three points 60° apart on a 25° circle turn that into well under 1′ on the axis.
  - The live loop amplifies noise by 1/conditioning; the page's `conditioning` field shows this (about 0.6 at the default geometry, and 1 is ideal).
  - Fields near the zenith, or near the east/west horizon, make the az (or alt) knob hard to see.
- **Refraction:** the pole is about 0.5′ higher in apparent altitude at 60° latitude, and about 1′ at 40°. The points themselves are refracted by different amounts, and the fit handles that because it works in apparent directions. Set `pressure_hpa` / `temperature_c` roughly; at these altitudes 10 % in pressure changes the result by under 0.1′.
- **Frames:**
  - The solve is ICRS, and FK5 of date is used as "JNow". Aberration and nutation (≤ 20″) are left out. They shift all points almost equally, so the axis moves by well under 1′.
  - LST is mean sidereal time (the equation of the equinoxes is ≤ 1 s, about 15″).
- **Not modelled:**
  - non-perpendicularity between the RA axis and the optical axis or the Dec axis (cone error). Measuring at a fixed Dec isn't affected, but a sync or the mount's own model is;
  - flexure between the points;
  - the knob axes not being exactly vertical or horizontal. The wedge's az pivot sits on a tilted base, so the live loop's knob decomposition is only approximate. The error it reports always comes from the latest solve, though, and the error converges as you iterate.
- **Mount pointing:** pointing is 1–2° off before a sync, and that doesn't matter here: the method uses only solved positions and RA-axis angle differences.

## At the wedge

1. Start the service on the scope host (`python -m sequencer serve`), with `[sequencer] host = "0.0.0.0"` and an `api_token`.
2. On the phone, open `http://<host>:8640/polar?token=<token>`. The page remembers the token.
3. Press **Start**. The scope makes three RA moves and solves at each; this takes about a minute or two. The big numbers then show the measured error.
4. Turn the knob in the direction shown:
   - **Altitude:** ↑ "raise" or ↓ "lower".
   - **Azimuth:** "turn east" or "turn west".
   - After each solve (every few seconds) the numbers update. The star (yellow) moves towards the reticle (blue). Stop when the star is inside the green tolerance circle and both numbers are green.
   - The trace shows any overshoot.
5. Press **Done** to store the result. Stop leaves without storing it.
6. **Learn the knob directions once.**
   - Turn the alt knob clockwise a little and watch whether the altitude number rises or falls. Set `[polar] alt_knob_sign = 1` if clockwise *raises* the axis, or −1 if it lowers it. Do the same for azimuth: `az_knob_sign = 1` if clockwise moves the axis *east*.
   - From then on the page also says "clockwise" or "counter-clockwise".
   - Until you've set them, it describes only the axis motion.
7. Re-measure (Start again) after big corrections; the live loop's knob model is first-order. Then check the result with the drift readout during imaging: Dec drift should drop well below the 21″/min at HA +17° seen on the first run.

**Simulator:**
- `python -m sequencer serve --sim` starts with a 50′/−70′ error (`--sim-polar-error ALT,AZ`, or `none`).
- `python -m sequencer sim-polar -30 50` stands in for turning the knobs by −30′ in alt and +50′ in az.

## Real hardware: still to check

- That the SeeStar's `RightAscension`/hour-angle readout follows the RA axis in step while tracking. The live loop uses it to rotate the reference.
- That the ASTAP WCS orientation (`cd`, used for the reticle) has the right parity for the telephoto's ImageArray orientation. If the star moves away from the reticle when you follow the words, this is the first suspect.
- Which way each knob turns, which sets `alt_knob_sign` / `az_knob_sign`.
- How long a 5 s solve takes on the host. ASTAP takes about 0.2–0.4 s per frame on a desktop PC; expect somewhat more on a small server.
- The knob pivots: whether the az knob turns the axis about a near-vertical axis and the alt knob about a horizontal one. If not, expect a few iterations, and re-measure.
- That the measured error agrees with the drift seen on the first run: about 1–1.5°, with the Dec drift larger at HA +17° than at −27°.
