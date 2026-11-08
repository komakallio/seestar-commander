# SeeStar S30 Pro over Alpaca: notes

Verified on one S30 Pro (Alpaca server 1.3.1-1, Nov 2026) on an EQ wedge, unless marked otherwise.

## Devices

| Device | Notes |
|---|---|
| Telescope 0 | InterfaceVersion 3 |
| Camera 0 (telephoto) | 2160×3840 portrait, 2.9 µm pixels, 160 mm, about 3.74″/px, field about 2.2°×4.0°. Bayer **GRBG** (BayerOffset 1,0). Exposure up to 2000 s, gain 0–600 |
| Camera 1 (wide) | 2160×3840, 1.6 µm pixels, 6 mm lens, field about 32°×54°. GRBG. Exposure 30 µs–60 s. Strong vignetting (corners about 46 % of centre) |
| Focuser 0/1 | Absolute. Wide focuser range 0–1023; the factory position was already at star focus |
| FilterWheel 0 | `Dark`, `IR` (IR-cut / broadband), `LP` (duo-band). **`Dark` is the shutter, used for dark frames** |
| Switch 0 | Dew heater, on/off |

**Telescope 0 details:**
- **AlignmentMode 1** (polar) on the wedge, **EquatorialSystem = topocentric** (JNow).
- **CanSlewAltAz = false.** CanSlew, CanSync and CanPark are true.
- MoveAxis works on both axes, at 0–6 °/s.
- **Axis direction** (verified with a camera): positive primary rate means increasing hour angle; positive secondary rate means increasing Dec.

**Downloads:** about 16 MB per frame through ImageArray. That took about 10 s over a VPN and WiFi, so run on the LAN.

## Quirks and gotchas

- **Firmware gotos ignore cable wrap.** A goto from the home position spun the RA axis a full turn the long way round. Use closed-loop MoveAxis with your own unwrapped hour-angle bookkeeping.
- **Tracking:**
  - turning it on is refused while the scope points below the horizon (`below horizon`), for example while folded at home;
  - `SlewToCoordinates` still runs with tracking off.
- **The pointing model is rough.** Pointing was off by about 1.5–2° after a fresh day. Plate-solve, then `SyncToCoordinates` (in **JNow**), then re-centre.
- **Folding the arm:** MoveAxis on Dec stops at about −84.6°. The firmware's `Park()` folds it fully (Dec −90, AtPark = true). From the home RA angle it only moved RA by under 2°. Still run it under a guard that aborts if RA turns more than a few degrees (`tools/park_guarded.py`).
- **Calibration:** every Alpaca exposure stops the app's running view and sets `frame_calib:false`, so you get raw, uncalibrated frames.
- **Unguided tracking:** at 60 s and gain 80 on the telephoto, stars were about 3.9 px HFR with about 0.4 elongation. Drift was about 13–15″/min, almost all in RA, suggesting a tracking-rate error rather than polar misalignment.
- **Wide-camera stars are sparse** for small fields. Use a wide-field solver for the wide camera, and astrometry.net (index 4100 series) for the telephoto.
- **Image direction depends on where the scope points.** The camera is fixed to the Dec axis, so on an EQ mount the long side follows Dec. Probe moves of +0.5° in Dec and HA give the pixel direction of each axis, which is used for centring.
