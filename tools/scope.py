"""SeeStar S30 Pro control over Alpaca: cable-safe HA/Dec slews and wide-camera exposures.

Mount is on an EQ wedge (AlignmentMode polar, topocentric RA/Dec), CanSlewAltAz=false.
Cable safety: the RA axis only moves by closed-loop MoveAxis inside a configured window
around the home hour angle (config [mount]); the unwrapped HA is persisted in
data/ra_unwrap.json. Firmware gotos can take the long way round and wind the cable.
"""
import json
import math
import os
import threading
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


def lst_deg(t=None):
    """Local apparent sidereal time in degrees, now or at unix time t."""
    tt = Time.now() if t is None else Time(t, format="unix")
    return tt.sidereal_time("apparent", longitude=pl.LON * u.deg).deg


class Interrupted(RuntimeError):
    """A motion was stopped on request (Scope.interrupt returned true)."""


def wrap180(x):
    return (x + 180.0) % 360.0 - 180.0


class Scope:
    """Mount (plus the wide camera and its focuser).

    The keyword arguments let the sequencer inject simulated devices, a scaled clock and its own
    state paths; with none given this is the plain Alpaca setup used by the tools/ scripts.
      clock      object with .time() and .sleep(s) (default: the time module)
      lst        callable -> local sidereal time in degrees (default: astropy at clock time)
      window     (home_ha, ha_u_min, ha_u_max) in unwrapped degrees (default: from config)
      interrupt  callable; when it returns true, a running axis move stops and raises Interrupted
      lock_owner opt-in exclusive use (tools/scopelock.py): take the sequencer's API lock, or the file
                 lock if the service isn't running, before connecting; raises scopelock.LockBusy if
                 someone else has the scope. Default: $SEESTAR_LOCK_OWNER (reason: $SEESTAR_LOCK_REASON),
                 only for the plain Alpaca setup. Released by close() or at exit; once the lock is lost
                 (revoked, expired), axis moves stop and raise Interrupted.
    """

    def __init__(self, host=HOST, log=print, telescope=None, camera=None, focuser=None,
                 clock=None, lst=None, unwrap_path=None, window=None, lock_owner=None, lock_reason=None,
                 lock_ttl_s=120.0):
        self.log = log
        self.scopelock = None
        if lock_owner is None and telescope is None:
            lock_owner = os.environ.get("SEESTAR_LOCK_OWNER") or None
        if lock_owner:
            import atexit
            from scopelock import ScopeLock
            reason = lock_reason if lock_reason is not None else os.environ.get("SEESTAR_LOCK_REASON", "")
            self.scopelock = ScopeLock(lock_owner, reason, ttl_s=lock_ttl_s, log=log).acquire()
            atexit.register(self.close)
        self.clock = clock or time
        self._lst = lst or (lambda: lst_deg(self.clock.time()))
        self.unwrap_path = unwrap_path or UNWRAP
        self.ha_neutral, self.ha_u_min, self.ha_u_max = window or (HA_NEUTRAL, HA_U_MIN, HA_U_MAX)
        self.interrupt = None
        self._ulock = threading.RLock()
        if telescope is None:
            telescope, camera, focuser = Telescope(host, 0), Camera(host, 1), Focuser(host, 1)
        self.t, self.c, self.f = telescope, camera, focuser
        for d in (self.t, self.c, self.f):
            if d is not None:
                d.Connected = True

    # ---- mount ---------------------------------------------------------
    def position(self):
        t = self.t
        ra_h, dec, alt, az = t.RightAscension, t.Declination, t.Altitude, t.Azimuth
        ha = wrap180(self._lst() - ra_h * 15.0)
        return {"ra_h": ra_h, "dec": dec, "ha": ha, "alt": alt, "az": az}

    # Cable-safe motion: NEVER goto. RA axis moves only via MoveAxis with the unwrapped
    # hour angle tracked in data/ra_unwrap.json. Neutral = HA -90 (home angle); allowed
    # window HA_U_MIN..HA_U_MAX (unwrapped). Positive primary rate = +HA = clockwise.
    def _load_u(self):
        try:
            with open(self.unwrap_path) as fh:
                return json.load(fh)["ha_unwrapped"]
        except Exception:
            raise RuntimeError(f"no {self.unwrap_path}: unwrapped HA unknown, refusing to move RA")

    def _save_u(self, u, note=""):
        tmp = f"{self.unwrap_path}.{os.getpid()}.{threading.get_ident()}.tmp"   # unique: several writers
        with open(tmp, "w") as fh:
            json.dump({"ha_unwrapped": round(u, 3),
                       "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock.time())),
                       "note": note}, fh)
        os.replace(tmp, self.unwrap_path)

    def ha_unwrapped(self):
        """Read HA and update the unwrapped value (assumes < 180 deg since last read)."""
        with self._ulock:
            u_prev = self._load_u()
            ha = wrap180(self._lst() - self.t.RightAscension * 15.0)
            u = u_prev + wrap180(ha - wrap180(u_prev))
            self._save_u(u)
            return u

    def close(self):
        """Release the scope lock, if this Scope took one."""
        if self.scopelock is not None:
            lk, self.scopelock = self.scopelock, None
            lk.release()

    def _check_interrupt(self, axis):
        if self.interrupt and self.interrupt():
            self.t.MoveAxis(axis, 0.0)
            raise Interrupted("motion interrupted")
        if self.scopelock is not None and self.scopelock.lost.is_set():
            self.t.MoveAxis(axis, 0.0)
            raise Interrupted(f"scope lock lost: {self.scopelock.lost_msg}")

    def _axis_to(self, axis, target, read, guard=None, tol=0.3):
        """Closed-loop MoveAxis: fast 5 deg/s, then 1, then 0.3, then a 0.05 deg/s fine approach.

        tol is the stop tolerance in degrees of axis angle. 0.3 is fine for general moves; centring
        must pass a much smaller one (first real run: a 0.3 deg tolerance left 9-13' residuals)."""
        for _ in range(40):
            cur = read()
            tgt = target() if callable(target) else target   # callable: a moving target (sky rotation)
            err = tgt - cur
            if abs(err) <= tol:
                return cur
            if abs(err) > 6:
                rate, dur = 5.0, (abs(err) - 3) / 5.0
            elif abs(err) > 1.5:
                rate, dur = 1.0, (abs(err) - 0.6) / 1.0
            elif abs(err) > 0.4:
                rate, dur = 0.3, (abs(err) - 0.15) / 0.3
            else:
                rate, dur = 0.05, abs(err) / 0.05
            dur = min(dur, 20.0)
            sign = 1 if err > 0 else -1
            self._check_interrupt(axis)
            try:
                self.t.MoveAxis(axis, sign * rate)
                t_end = self.clock.time() + dur
                while self.clock.time() < t_end:
                    self.clock.sleep(min(1.0, max(0.0, t_end - self.clock.time())))
                    if guard and dur > 2:
                        guard(read())
                    self._check_interrupt(axis)
            finally:
                self.t.MoveAxis(axis, 0.0)
            self.clock.sleep(0.8)
        raise RuntimeError(f"axis {axis} did not converge to {target}")

    def _ha_guard(self, u):
        if not (self.ha_u_min - 3 <= u <= self.ha_u_max + 3):
            self.t.MoveAxis(PRIMARY, 0.0)
            raise RuntimeError(f"CABLE GUARD: unwrapped HA {u:.1f} outside window")

    def move_hadec(self, ha, dec, settle=1.5, tol=0.3):
        """Move to (HA, Dec). HA is mapped into the cable window [HA_U_MIN, HA_U_MAX].
        tol: per-axis stop tolerance in degrees (pass ~0.02 for centring)."""
        if not -85 <= dec <= 89.5:
            raise ValueError(f"Dec {dec:.1f} out of range")
        u_t = self.map_ha(ha)
        if u_t is None:
            raise ValueError(f"HA {ha:.1f} not reachable inside cable window")
        if self.t.Tracking:
            self.t.Tracking = False
        self._axis_to(PRIMARY, u_t, self.ha_unwrapped, guard=self._ha_guard, tol=tol)
        self._axis_to(SECONDARY, dec, lambda: self.t.Declination, tol=tol)
        self.clock.sleep(settle)
        return self.position()

    def move_radec(self, ra_deg, dec, tol=0.3, settle=1.5, track=True):
        """Move to a sky position (JNow RA in degrees, Dec) and start tracking on arrival.

        Unlike move_hadec, the hour-angle target is recomputed from the current sidereal time on
        every approach step, and Dec is moved first. With tracking off the sky keeps turning at
        15"/s, so a fixed HA target is several arcminutes stale after a 20-30 s move (first real
        run: 5' residual after a sync)."""
        if not -85 <= dec <= 89.5:
            raise ValueError(f"Dec {dec:.1f} out of range")

        def u_target():
            u = self.map_ha(wrap180(self._lst() - ra_deg))
            if u is None:
                raise ValueError(f"RA {ra_deg:.2f} not reachable inside cable window now")
            return u
        u_target()                                  # refuse early if unreachable
        if self.t.Tracking:
            self.t.Tracking = False
        self._axis_to(SECONDARY, dec, lambda: self.t.Declination, tol=tol)
        self._axis_to(PRIMARY, u_target, self.ha_unwrapped, guard=self._ha_guard, tol=tol)
        if track:
            self.t.Tracking = True
        self.clock.sleep(settle)
        return self.position()

    def map_ha(self, ha):
        """Unwrapped angle inside the cable window for a wrapped HA, or None if unreachable."""
        u_t = ha
        while u_t > self.ha_u_max:
            u_t -= 360
        while u_t < self.ha_u_min:
            u_t += 360
        return u_t if self.ha_u_min <= u_t <= self.ha_u_max else None

    def park_guarded(self, guard_deg=10.0, timeout=120.0, pre_dec=-80.0):
        """Cable-safe park: tracking off, RA to the home angle by axis moves, Dec down, then the
        firmware Park() in a thread while the unwrapped HA is watched; abort if RA turns more than
        guard_deg. pre_dec stays clear of the Dec MoveAxis stop (about -84.6, shifted by syncs); the
        firmware Park folds the rest. Returns a dict; parked=True only if AtPark and no abort. No hooks."""
        t = self.t
        if t.AtPark:
            return {"parked": True, "already": True, "aborted": False, "max_dev": 0.0}
        if t.Tracking:
            t.Tracking = False
        self.move_hadec(self.ha_neutral, pre_dec)
        u0 = self.ha_unwrapped()
        err = []

        def do_park():
            try:
                t.Park()
            except Exception as e:  # noqa: BLE001 - reported to the caller
                err.append(str(e))
        th = threading.Thread(target=do_park, daemon=True)
        th.start()
        t0 = self.clock.time()
        aborted, maxdev = False, 0.0
        while self.clock.time() - t0 < timeout:
            self.clock.sleep(0.3)
            uu = self.ha_unwrapped()
            maxdev = max(maxdev, abs(uu - u0))
            if abs(uu - u0) > guard_deg:
                self.abort()
                aborted = True
                self.log(f"PARK ABORTED: RA moved {uu - u0:+.1f} deg during firmware Park")
                break
            if not th.is_alive() and not t.Slewing:
                break
        th.join(timeout=5)
        self.clock.sleep(1.0)
        u1 = self.ha_unwrapped()
        return {"parked": bool(t.AtPark) and not aborted and not err, "aborted": aborted,
                "max_dev": round(float(maxdev), 2), "u": round(float(u1), 2), "dec": round(float(t.Declination), 2),
                "error": err}

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
