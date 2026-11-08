"""SeeStar S30 Pro control over Alpaca: cable-safe HA/Dec slews and wide-camera exposures.

Mount is on an EQ wedge (AlignmentMode polar, topocentric RA/Dec), CanSlewAltAz=false.
Cable safety: the RA axis only moves by closed-loop MoveAxis inside a configured window
around the home hour angle (config [mount]); the unwrapped HA is persisted in
data/ra_unwrap.json. Firmware gotos can take the long way round and wind the cable.
"""
import json
import math
import os
import time

import numpy as np
from astropy.io import fits
from astropy.time import Time
import astropy.units as u

from alpaca.camera import Camera
from alpaca.focuser import Focuser
from alpaca.telescope import Telescope

import panolib as pl

import config

HOST = config.SEESTAR
HA_LIMIT = 175.0
SLEW_TIMEOUT = 180
HA_NEUTRAL = config.HOME_HA                 # RA-axis angle at home = cable neutral
HA_U_MIN = config.HOME_HA - config.CCW_LIMIT + config.LIMIT_MARGIN
HA_U_MAX = config.HOME_HA + config.CW_LIMIT - config.LIMIT_MARGIN
UNWRAP = os.path.join(pl.ROOT, "data", "ra_unwrap.json")
from alpaca.telescope import TelescopeAxes
PRIMARY, SECONDARY = TelescopeAxes.axisPrimary, TelescopeAxes.axisSecondary


def lst_deg():
    return Time.now().sidereal_time("apparent", longitude=pl.LON * u.deg).deg


def wrap180(x):
    return (x + 180.0) % 360.0 - 180.0


class Scope:
    def __init__(self, host=HOST, log=print):
        self.log = log
        self.t, self.c, self.f = Telescope(host, 0), Camera(host, 1), Focuser(host, 1)
        for d in (self.t, self.c, self.f):
            d.Connected = True

    # ---- mount ---------------------------------------------------------
    def position(self):
        t = self.t
        ra_h, dec, alt, az = t.RightAscension, t.Declination, t.Altitude, t.Azimuth
        ha = wrap180(lst_deg() - ra_h * 15.0)
        return {"ra_h": ra_h, "dec": dec, "ha": ha, "alt": alt, "az": az}

    # Cable-safe motion: NEVER goto. RA axis moves only via MoveAxis with the unwrapped
    # hour angle tracked in data/ra_unwrap.json. Neutral = HA -90 (home angle); allowed
    # window HA_U_MIN..HA_U_MAX (unwrapped). Positive primary rate = +HA = clockwise.
    def _load_u(self):
        try:
            return json.load(open(UNWRAP))["ha_unwrapped"]
        except Exception:
            raise RuntimeError(f"no {UNWRAP}: unwrapped HA unknown, refusing to move RA")

    def _save_u(self, u, note=""):
        json.dump({"ha_unwrapped": round(u, 3), "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "note": note}, open(UNWRAP, "w"))

    def ha_unwrapped(self):
        """Read HA and update the unwrapped value (assumes < 180 deg since last read)."""
        u_prev = self._load_u()
        ha = wrap180(lst_deg() - self.t.RightAscension * 15.0)
        u = u_prev + wrap180(ha - wrap180(u_prev))
        self._save_u(u)
        return u

    def _axis_to(self, axis, target, read, guard=None, tol=0.3):
        """Closed-loop MoveAxis: fast 5 deg/s, then 1, then 0.3 deg/s approach."""
        for _ in range(40):
            cur = read()
            err = target - cur
            if abs(err) <= tol:
                return cur
            if abs(err) > 6:
                rate, dur = 5.0, (abs(err) - 3) / 5.0
            elif abs(err) > 1.5:
                rate, dur = 1.0, (abs(err) - 0.6) / 1.0
            else:
                rate, dur = 0.3, abs(err) / 0.3
            dur = min(dur, 20.0)
            sign = 1 if err > 0 else -1
            try:
                self.t.MoveAxis(axis, sign * rate)
                t_end = time.time() + dur
                while time.time() < t_end:
                    time.sleep(min(1.0, max(0.0, t_end - time.time())))
                    if guard and dur > 2:
                        guard(read())
            finally:
                self.t.MoveAxis(axis, 0.0)
            time.sleep(0.8)
        raise RuntimeError(f"axis {axis} did not converge to {target}")

    def _ha_guard(self, u):
        if not (HA_U_MIN - 3 <= u <= HA_U_MAX + 3):
            self.t.MoveAxis(PRIMARY, 0.0)
            raise RuntimeError(f"CABLE GUARD: unwrapped HA {u:.1f} outside window")

    def move_hadec(self, ha, dec, settle=1.5):
        """Move to (HA, Dec). HA is mapped into the cable window [HA_U_MIN, HA_U_MAX]."""
        if not -85 <= dec <= 89.5:
            raise ValueError(f"Dec {dec:.1f} out of range")
        u_t = ha
        while u_t > HA_U_MAX:
            u_t -= 360
        while u_t < HA_U_MIN:
            u_t += 360
        if not HA_U_MIN <= u_t <= HA_U_MAX:
            raise ValueError(f"HA {ha:.1f} not reachable inside cable window")
        if self.t.Tracking:
            self.t.Tracking = False
        self._axis_to(PRIMARY, u_t, self.ha_unwrapped, guard=self._ha_guard)
        self._axis_to(SECONDARY, dec, lambda: self.t.Declination)
        time.sleep(settle)
        return self.position()

    # the old goto path is gone on purpose (it wound the cable 2026-11-08)
    def slew_hadec(self, ha, dec, settle=1.5, track_after=False):
        return self.move_hadec(ha, dec, settle)

    def abort(self):
        for ax in (PRIMARY, SECONDARY):
            try:
                self.t.MoveAxis(ax, 0.0)
            except Exception as e:
                self.log(f"stop axis failed: {e}")
        try:
            self.t.AbortSlew()
        except Exception as e:
            self.log(f"abort failed: {e}")

    # ---- camera --------------------------------------------------------
    def expose(self, exp_s, gain, timeout_extra=90):
        c = self.c
        c.Gain = int(gain)
        c.StartExposure(float(exp_s), True)
        t0 = time.time()
        time.sleep(min(exp_s, 1.0))
        while not c.ImageReady:
            if time.time() - t0 > exp_s + timeout_extra:
                try:
                    c.AbortExposure()
                except Exception:
                    pass
                raise RuntimeError("exposure timeout")
            time.sleep(0.3)
        return np.array(c.ImageArray).T.astype(np.uint16)  # -> [y][x]

    def save_fits(self, path, data, exp_s, gain, pos, date_obs, extra=None):
        h = fits.Header()
        h["DATE-OBS"] = date_obs
        h["EXPTIME"] = exp_s
        h["GAIN"] = gain
        h["BAYERPAT"] = "GRBG"
        h["INSTRUME"] = "Seestar S30 Pro wide"
        h["FOCALLEN"] = pl.FOCAL_MM
        h["XPIXSZ"] = h["YPIXSZ"] = 1.6
        h["SITELAT"], h["SITELONG"] = pl.LAT, pl.LON
        h["FOCUSPOS"] = self.f.Position
        for k, key in (("ra_h", "MNT_RA"), ("dec", "MNT_DEC"), ("ha", "MNT_HA"),
                       ("alt", "MNT_ALT"), ("az", "MNT_AZ")):
            h[key] = pos[k]
        h["RA"] = pos["ra_h"] * 15.0
        h["DEC"] = pos["dec"]
        for k, v in (extra or {}).items():
            h[k] = v
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fits.writeto(path, data, h, overwrite=True)

    # ---- focuser -------------------------------------------------------
    def focus_to(self, pos, timeout=60):
        pos = int(max(0, min(self.f.MaxStep, pos)))
        self.f.Move(pos)
        t0 = time.time()
        time.sleep(0.5)
        while self.f.IsMoving:
            if time.time() - t0 > timeout:
                raise RuntimeError("focuser timeout")
            time.sleep(0.3)
        return self.f.Position


def camsnap(out, url=None):
    """One frame from the optional IP camera watching the scope (config [ipcam] rtsp_url)."""
    import av
    os.makedirs(os.path.dirname(out), exist_ok=True)
    url = url or config.IPCAM_RTSP
    if not url:
        return None
    c = av.open(url, options={"rtsp_transport": "tcp"}, timeout=15)
    try:
        for fr in c.decode(video=0):
            fr.to_image().save(out, quality=85)
            return out
    finally:
        c.close()


def green(data):
    """GRBG superpixel green channel (half resolution), float."""
    d = data.astype(np.float32)
    return (d[0::2, 0::2] + d[1::2, 1::2]) / 2


def star_hfr(data, nmax=60, box=10):
    """Median half-flux radius (superpixels) of bright unsaturated stars, plus count."""
    g = green(data)
    bg = np.median(g)
    sig = 1.4826 * np.median(np.abs(g - bg)) + 1e-6
    from numpy.lib.stride_tricks import sliding_window_view as swv
    k = 7
    pad = np.pad(g, k // 2, mode="edge")
    mx = swv(pad, (k, k)).max(axis=(2, 3))
    cand = np.argwhere((g == mx) & (g > bg + 12 * sig) & (g < 60000))
    cand = [p for p in cand if box <= p[0] < g.shape[0] - box and box <= p[1] < g.shape[1] - box]
    cand.sort(key=lambda p: -g[p[0], p[1]])
    yy, xx = np.mgrid[-box:box + 1, -box:box + 1]
    hfrs = []
    for y, x in cand[:nmax]:
        s = g[y - box:y + box + 1, x - box:x + box + 1] - bg
        s = np.clip(s, 0, None)
        tot = s.sum()
        if tot <= 0:
            continue
        cy, cx = (s * yy).sum() / tot, (s * xx).sum() / tot
        r = np.hypot(yy - cy, xx - cx)
        hfrs.append((s * r).sum() / tot)
    return (float(np.median(hfrs)) if hfrs else float("nan")), len(hfrs)
