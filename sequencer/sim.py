"""Simulated SeeStar devices with the same attribute/method names as the alpyca clients, so the real
motion code (tools/scope.py: closed-loop MoveAxis, unwrapped HA, guarded Park) runs unchanged.

What is modelled:
  - mount on an EQ wedge: mechanical RA-axis angle (unwrapped, so tests can assert the cable never
    left its window), Dec axis that stops at -84.6 under MoveAxis, tracking with a rate error
    (drift), a fixed index error in HA and Dec (`pointing_error`) that SyncToCoordinates hides, tracking
    refused below the horizon, a firmware Park that goes to the home angle the *wrong* way round when
    it starts far from home and, like the real firmware, forgets the sync once it is parked
    (`park_drops_sync`), and firmware gotos that are refused and counted (the sequencer must never call
    them);
  - optional polar misalignment (`polar_error=(alt_arcmin, az_arcmin)` of the RA axis against the
    refracted pole, with refraction): the true pointing and the camera roll follow the misaligned axis,
    so tracking drifts and the solver sees it; `adjust_polar` / `set_polar` stand in for the operator
    turning the wedge knobs;
  - telephoto camera rendering a deterministic random star field at the true pointing (no stars on
    the Dark filter or with the roof closed), with bias, dark current and noise;
  - filter wheel (Dark/IR/LP), focuser, dew-heater switch, Alpaca-like dome, settable safety source,
    and a plate solver that knows the truth.
"""
import math
import threading

import numpy as np

from . import astro


class SimError(RuntimeError):
    pass


class SimTelescope:
    def __init__(self, clock, lat, lon, home_ha=-90.0, ccw_limit=90.0, cw_limit=270.0,
                 pointing_error=(1.2, -0.8), track_error=0.0155, start_parked=True, polar_error=None,
                 pressure_hpa=1010.0, temp_c=10.0, park_drops_sync=True):
        self.clock, self.lat, self.lon = clock, lat, lon
        self.park_drops_sync = park_drops_sync
        self.polar = None if polar_error is None else (float(polar_error[0]), float(polar_error[1]))
        self.refr = (pressure_hpa, temp_c)
        self._polar_R = None
        self.home_ha = home_ha
        self.phys_min, self.phys_max = home_ha - ccw_limit, home_ha + cw_limit
        self.err_ha, self.err_dec = pointing_error
        self.track_error = track_error
        self.Connected = False
        self._lock = threading.RLock()
        self._u = home_ha + 0.0          # mechanical RA-axis angle (unwrapped HA), deg
        self._dec = -90.0 if start_parked else -84.6
        self._rate = [0.0, 0.0]
        self._tracking = False
        self._parked = start_parked
        self._park_target = None
        self._off_ha = self._off_dec = 0.0
        self._t = clock.time()
        self.u_min_seen = self.u_max_seen = self._u
        self.cable_violation = False
        self.firmware_gotos = 0
        self.park_calls = 0
        self.calls = []

    # ---- internal -------------------------------------------------------
    def _advance(self):
        now = self.clock.time()
        dt = max(0.0, now - self._t)
        self._t = now
        if dt == 0:
            return
        if self._park_target is not None:
            pu, pd = self._park_target
            step = 4.0 * dt
            self._u += max(-step, min(step, pu - self._u))
            self._dec += max(-step, min(step, pd - self._dec))
            if abs(self._u - pu) < 1e-6 and abs(self._dec - pd) < 1e-6:
                self._park_target = None
                self._parked = True
                if self.park_drops_sync:          # the firmware resets its model at park
                    self._off_ha = self._off_dec = 0.0
        elif not self._parked:
            self._u += self._rate[0] * dt
            if self._tracking:
                self._u += astro.SIDEREAL_DEG_S * (1.0 + self.track_error) * dt
            self._dec = max(-84.6, min(90.0, self._dec + self._rate[1] * dt))
        self.u_min_seen = min(self.u_min_seen, self._u)
        self.u_max_seen = max(self.u_max_seen, self._u)
        if not self.phys_min <= self._u <= self.phys_max:
            self.cable_violation = True

    def _lst(self):
        return astro.lst_deg(self.clock.time(), self.lon)

    def reported_hadec(self):
        with self._lock:
            self._advance()
            return astro.wrap180(self._u + self._off_ha), self._dec + self._off_dec

    # ---- polar misalignment ---------------------------------------------------------------------
    def set_polar(self, alt_arcmin, az_arcmin):
        """Polar axis error against the refracted pole (az in arcmin of sky arc, + = east)."""
        with self._lock:
            self._advance()
            self.polar = (float(alt_arcmin), float(az_arcmin))
            self._polar_R = None

    def adjust_polar(self, dalt_arcmin, daz_arcmin):
        """The operator turns the knobs: change the error by (dalt, daz) arcmin."""
        a, z = self.polar or (0.0, 0.0)
        self.set_polar(a + dalt_arcmin, z + daz_arcmin)

    def _mount_to_jnow(self, u, dec_m, t):
        """Mechanical (HA, Dec) of the optics -> true JNow (ra, dec) at time t, through the misaligned
        axis and refraction (polar model only)."""
        from . import polar as pm
        lat = self.lat
        p, tc = self.refr
        if self._polar_R is None:
            self._polar_R = pm.mount_rotation(self.polar[0], self.polar[1], lat, p, tc)
        lat_p = float(pm.apparent_alt(abs(lat), p, tc)) * (1 if lat >= 0 else -1)
        alt, az = pm.hadec_to_altaz(astro.wrap180(u), dec_m, lat_p)
        v = self._polar_R @ pm.enu(alt, az)
        ra, dec = pm.app_to_jnow(v, t, lat, self.lon, p, tc)
        return float(ra), float(dec)

    def _true(self, u, t):
        if self.polar is None:
            ha = u + self.err_ha
            return (astro.lst_deg(t, self.lon) - ha) % 360.0, max(-90.0, min(90.0, self._dec + self.err_dec))
        return self._mount_to_jnow(u + self.err_ha, self._dec + self.err_dec, t)

    def true_pa(self):
        """Position angle (deg, east of north) of the camera's 'up' (mount +Dec) on the sky; 0 without
        the polar model."""
        if self.polar is None:
            return 0.0
        with self._lock:
            self._advance()
            t = self.clock.time()
            u, d = self._u + self.err_ha, self._dec + self.err_dec
            ra1, d1 = self._mount_to_jnow(u, d, t)
            ra2, d2 = self._mount_to_jnow(u, d + 0.05, t)
        a1, a2, dd1, dd2 = map(math.radians, (ra1, ra2, d1, d2))
        return math.degrees(math.atan2(math.sin(a2 - a1) * math.cos(dd2),
                                       math.cos(dd1) * math.sin(dd2) - math.sin(dd1) * math.cos(dd2) * math.cos(a2 - a1)))

    def true_radec(self):
        """Where the optics really point (JNow), deg."""
        with self._lock:
            self._advance()
            return self._true(self._u, self.clock.time())

    def true_radec_at(self, t):
        """True pointing at an earlier time t, assuming only tracking since then."""
        with self._lock:
            self._advance()
            rate = astro.SIDEREAL_DEG_S * (1.0 + self.track_error) if self._tracking else 0.0
            u_t = self._u - rate * (self._t - t)
            return self._true(u_t, t)

    @property
    def mech_u(self):
        with self._lock:
            self._advance()
            return self._u

    # ---- Alpaca surface ---------------------------------------------------
    @property
    def RightAscension(self):
        ha, _ = self.reported_hadec()
        return ((self._lst() - ha) % 360.0) / 15.0

    @property
    def Declination(self):
        return self.reported_hadec()[1]

    @property
    def Altitude(self):
        ha, dec = self.reported_hadec()
        return astro.hadec_to_altaz(ha, dec, self.lat)[0]

    @property
    def Azimuth(self):
        ha, dec = self.reported_hadec()
        return astro.hadec_to_altaz(ha, dec, self.lat)[1]

    @property
    def Tracking(self):
        return self._tracking

    @Tracking.setter
    def Tracking(self, on):
        with self._lock:
            self._advance()
            if on and self._parked:
                raise SimError("parked")
            if on and self.Altitude < 0:
                raise SimError("below horizon")
            self._tracking = bool(on)

    @property
    def AtPark(self):
        with self._lock:
            self._advance()
            return self._parked

    @property
    def Slewing(self):
        with self._lock:
            self._advance()
            return self._park_target is not None or any(r != 0 for r in self._rate)

    def MoveAxis(self, axis, rate):
        with self._lock:
            self._advance()
            if self._parked and rate != 0:
                raise SimError("parked: Unpark first")
            if abs(rate) > 6.0:
                raise SimError("rate out of range")
            self._rate[int(axis)] = float(rate)

    def AbortSlew(self):
        with self._lock:
            self._advance()
            self._park_target = None
            self._rate = [0.0, 0.0]

    def SyncToCoordinates(self, ra_h, dec):
        with self._lock:
            self._advance()
            self.calls.append(("sync", ra_h, dec))
            self._off_ha = astro.wrap180((self._lst() - ra_h * 15.0) - self._u)
            self._off_dec = dec - self._dec

    def _goto(self, *a, **k):
        self.firmware_gotos += 1
        raise SimError("sim: firmware gotos are not allowed (cable safety)")

    SlewToCoordinates = SlewToCoordinatesAsync = SlewToTarget = SlewToTargetAsync = _goto
    SlewToAltAz = SlewToAltAzAsync = FindHome = _goto

    def Park(self):
        """Firmware park: always turns RA in the negative direction to the home angle (+1 deg slop)."""
        with self._lock:
            self._advance()
            self.park_calls += 1
            self._tracking = False
            self._rate = [0.0, 0.0]
            # goes to the home angle in the *synced* (reported) frame, always turning RA negative
            target = self.home_ha + 1.0 - self._off_ha
            while target > self._u + 2.0:
                target -= 360.0
            self._park_target = (target, -90.0)

    def Unpark(self):
        with self._lock:
            self._advance()
            self._parked = False
            if self._dec < -84.6:
                self._dec = -84.6


class SimFilterWheel:
    def __init__(self, clock, names=("Dark", "IR", "LP"), start=1):
        self.clock = clock
        self.Names = list(names)
        self._pos = start
        self._until = 0.0
        self.Connected = False

    @property
    def Position(self):
        return -1 if self.clock.time() < self._until else self._pos

    @Position.setter
    def Position(self, i):
        if not 0 <= i < len(self.Names):
            raise SimError("bad filter position")
        if i != self._pos:
            self._until = self.clock.time() + 1.5
        self._pos = i

    def current(self):
        return self.Names[self._pos]


class SimFocuser:
    def __init__(self):
        self.Position, self.MaxStep, self.IsMoving, self.Connected = 1600, 2600, False, False

    def Move(self, p):
        self.Position = int(p)


class SimSwitch:
    def __init__(self):
        self.Connected, self.MaxSwitch, self._on = False, 1, [False]

    def GetSwitchName(self, i):
        return "Dew heater"

    def GetSwitch(self, i):
        return self._on[i]

    def SetSwitch(self, i, v):
        self._on[i] = bool(v)


class SimDome:
    """ShutterStatus: 0 open, 1 closed, 2 opening, 3 closing (ASCOM). Opening and closing take move_s.
    Test modes: refuse = "reason" makes Open/CloseShutter raise (like an Alpaca driver error);
    stuck = True accepts commands but never finishes moving."""

    def __init__(self, clock, open_=True, move_s=30.0):
        self.clock, self.move_s, self.Connected = clock, move_s, False
        self._state, self._target, self._until = (0 if open_ else 1), None, 0.0
        self.close_calls = 0
        self.open_calls = 0
        self.refuse = None
        self.stuck = False

    def _upd(self):
        if self._target is not None and not self.stuck and self.clock.time() >= self._until:
            self._state, self._target = self._target, None

    @property
    def ShutterStatus(self):
        self._upd()
        if self._target is not None:
            return 2 if self._target == 0 else 3
        return self._state

    @property
    def Slewing(self):
        self._upd()
        return self._target is not None

    def _move(self, target):
        if self.refuse:
            raise SimError(self.refuse)
        self._upd()
        if self._target is None and self._state == target:
            return
        self._target, self._until = target, self.clock.time() + self.move_s

    def CloseShutter(self):
        self.close_calls += 1
        self._move(1)

    def OpenShutter(self):
        self.open_calls += 1
        self._move(0)

    def is_open(self):
        return self.ShutterStatus == 0


class SimSafety:
    def __init__(self):
        self.safe, self.reasons = True, []

    def set(self, safe, reasons=()):
        self.safe, self.reasons = bool(safe), list(reasons)

    def state(self):
        return self.safe, list(self.reasons)


class SimSky:
    """Deterministic random stars in 1x1 deg (RA, Dec) cells, plus optional extended blobs."""

    def __init__(self, density=150, seed=1):
        self.density, self.seed = density, seed
        self.blobs = []          # (ra, dec, sigma_deg, peak)
        self._cache = {}

    def add_blob(self, ra, dec, sigma_deg=0.25, peak=4000.0):
        self.blobs.append((ra, dec, sigma_deg, peak))

    def cell(self, i, j):
        key = (i, j)
        if key not in self._cache:
            rng = np.random.default_rng((self.seed * 1000003 + (i % 360) * 1009 + j) & 0x7FFFFFFF)
            n = int(self.density * max(0.05, math.cos(math.radians(j + 0.5))))
            ra = (i + rng.random(n)) % 360.0
            dec = j + rng.random(n)
            flux = 2.0e4 * rng.pareto(1.3, n) + 3.0e3      # counts in 60 s at gain 80
            self._cache[key] = (ra, dec, np.minimum(flux, 3e6))
        return self._cache[key]

    def stars_near(self, ra0, dec0, radius):
        out = []
        for j in range(int(math.floor(dec0 - radius)), int(math.floor(dec0 + radius)) + 1):
            if not -90 <= j < 90:
                continue
            w = radius / max(0.05, math.cos(math.radians(min(89.0, abs(j) + 1))))
            for i in range(int(math.floor(ra0 - w)), int(math.floor(ra0 + w)) + 1):
                out.append(self.cell(i % 360, j))
        if not out:
            return np.zeros(0), np.zeros(0), np.zeros(0)
        return tuple(np.concatenate([o[k] for o in out]) for k in range(3))


def _tangent(ra, dec, ra0, dec0):
    """Gnomonic projection (deg) of (ra, dec) arrays about (ra0, dec0): xi east, eta north."""
    a, d = np.radians(ra), np.radians(dec)
    a0, d0 = math.radians(ra0), math.radians(dec0)
    cosc = math.sin(d0) * np.sin(d) + math.cos(d0) * np.cos(d) * np.cos(a - a0)
    xi = np.cos(d) * np.sin(a - a0) / cosc
    eta = (math.cos(d0) * np.sin(d) - math.sin(d0) * np.cos(d) * np.cos(a - a0)) / cosc
    return np.degrees(xi), np.degrees(eta), cosc


class SimCamera:
    """Telephoto camera. ImageArray is [x][y] like Alpaca (the client transposes)."""

    def __init__(self, clock, telescope, wheel, sky, dome=None, width=1080, height=1920,
                 pix_arcsec=7.48, seeing_px=2.4, download_s=3.0, seed=7):
        self.clock, self.tel, self.wheel, self.sky, self.dome = clock, telescope, wheel, sky, dome
        self.CameraXSize, self.CameraYSize = width, height
        self.pix_arcsec, self.seeing_px, self.download_s = pix_arcsec, seeing_px, download_s
        self.Gain, self.Connected = 80, False
        self.Offset, self.BinX, self.Name, self.SensorName = 0, 1, "Sim telephoto", "SIM585"
        self.temp_c, self.temp_rate_c_h = 8.0, -0.3       # uncooled sensor, slowly cooling
        self._temp_t0 = clock.time()
        self._exp = None
        self._rng = np.random.default_rng(seed)
        self.last_truth = None
        self.exposures = 0
        self.aborts = 0

    @property
    def CCDTemperature(self):
        return round(self.temp_c + self.temp_rate_c_h * (self.clock.time() - self._temp_t0) / 3600.0, 2)

    def set_temp(self, c, rate_c_h=0.0):
        self.temp_c, self.temp_rate_c_h, self._temp_t0 = c, rate_c_h, self.clock.time()

    def StartExposure(self, duration, light=True):
        if self._exp is not None and not self._exp.get("done"):
            raise SimError("exposure already running")
        t0 = self.clock.time()
        self._exp = {"t0": t0, "dur": float(duration), "light": bool(light), "gain": self.Gain,
                     "start": self.tel.true_radec(), "filter": self.wheel.current(),
                     "pa": self.tel.true_pa() if hasattr(self.tel, "true_pa") else 0.0,
                     "open": self.dome.is_open() if self.dome else True}
        self.exposures += 1

    @property
    def ImageReady(self):
        e = self._exp
        return bool(e) and self.clock.time() >= e["t0"] + e["dur"] + self.download_s

    @property
    def CameraState(self):
        if not self._exp or self.ImageReady:
            return 0
        return 2

    def AbortExposure(self):
        if self._exp and not self.ImageReady:
            self.aborts += 1
        self._exp = None

    @property
    def ImageArray(self):
        e = self._exp
        if not e or not self.ImageReady:
            raise SimError("no image ready")
        if "img" not in e:
            e["end"] = self.tel.true_radec_at(e["t0"] + e["dur"])
            e["img"] = self._render(e)
            e["done"] = True
        return e["img"].T

    def _render(self, e):
        W, H = self.CameraXSize, self.CameraYSize
        dur, gain = e["dur"], e["gain"]
        g = gain / 80.0
        temp = self.CCDTemperature
        img = np.full((H, W), 400.0 + 1.5 * dur * 2 ** ((temp - 8.0) / 6.0), np.float32)
        hot = self._hot_pixels(H, W)
        img[hot[0], hot[1]] += hot[2] * dur * 2 ** ((temp - 8.0) / 6.0)
        sky_on = e["light"] and e["filter"] != "Dark" and e["open"]
        if sky_on:
            img += 300.0 * dur / 60.0 * g
            (ra_s, dec_s), (ra_e, dec_e) = e["start"], e["end"]
            ra0, dec0 = (ra_s + astro.wrap180(ra_e - ra_s) / 2) % 360.0, (dec_s + dec_e) / 2
            pa = e.get("pa", 0.0)
            cpa, spa = math.cos(math.radians(pa)), math.sin(math.radians(pa))
            self.last_truth = {"ra": ra0, "dec": dec0, "t": e["t0"] + dur / 2, "pa": pa}
            rad = math.hypot(W, H) / 2 * self.pix_arcsec / 3600.0 + 0.2
            ra, dec, flux = self.sky.stars_near(ra0, dec0, rad)
            samples = 5
            for k in range(samples):
                f = (k + 0.5) / samples
                rk = (ra_s + astro.wrap180(ra_e - ra_s) * f) % 360.0
                dk = dec_s + (dec_e - dec_s) * f
                if len(ra):
                    xi, eta, cosc = _tangent(ra, dec, rk, dk)
                    if pa:     # camera 'up' along the mount's +Dec, at position angle pa
                        xi, eta = xi * cpa - eta * spa, xi * spa + eta * cpa
                    x = W / 2 - xi * 3600.0 / self.pix_arcsec
                    y = H / 2 - eta * 3600.0 / self.pix_arcsec
                    ok = (cosc > 0) & (x > -5) & (x < W + 5) & (y > -5) & (y < H + 5)
                    self._stamps(img, x[ok], y[ok], flux[ok] * dur / 60.0 * g / samples)
                for (bra, bdec, bs, bp) in self.sky.blobs:
                    xi, eta, cosc = _tangent(np.array([bra]), np.array([bdec]), rk, dk)
                    if cosc[0] <= 0:
                        continue
                    bx = W / 2 - xi[0] * 3600.0 / self.pix_arcsec
                    by = H / 2 - eta[0] * 3600.0 / self.pix_arcsec
                    sp = bs * 3600.0 / self.pix_arcsec
                    yy, xx = np.mgrid[0:H, 0:W]
                    img += (bp * dur / 60.0 * g / samples
                            * np.exp(-((xx - bx) ** 2 + (yy - by) ** 2) / (2 * sp * sp))).astype(np.float32)
        else:
            self.last_truth = None
        img += self._rng.normal(0, 12.0, img.shape).astype(np.float32)
        img += np.sqrt(np.clip(img, 0, None)) * self._rng.normal(0, 1, img.shape).astype(np.float32) * 0.5
        return np.clip(img, 0, 65535).astype(np.uint16)

    def _hot_pixels(self, H, W):
        if getattr(self, "_hot", None) is None or self._hot[3] != (H, W):
            r = np.random.default_rng(99)
            n = H * W // 2000
            self._hot = (r.integers(0, H, n), r.integers(0, W, n), r.uniform(5, 200, n).astype(np.float32), (H, W))
        return self._hot

    def _stamps(self, img, xs, ys, fl):
        H, W = img.shape
        s = self.seeing_px
        r = int(math.ceil(4 * s))
        off = np.arange(-r, r + 1)
        for x, y, f in zip(xs, ys, fl):
            ix, iy = int(round(x)), int(round(y))
            x0, x1, y0, y1 = max(0, ix - r), min(W, ix + r + 1), max(0, iy - r), min(H, iy + r + 1)
            if x0 >= x1 or y0 >= y1:
                continue
            gx = np.exp(-((off + ix - x)[x0 - ix + r:x1 - ix + r]) ** 2 / (2 * s * s))
            gy = np.exp(-((off + iy - y)[y0 - iy + r:y1 - iy + r]) ** 2 / (2 * s * s))
            img[y0:y1, x0:x1] += (f / (2 * math.pi * s * s)) * np.outer(gy, gx)


class SimSolver:
    """Plate solver that reads the simulator's truth for the last light frame (ICRS out)."""
    name = "sim"

    def __init__(self, camera, min_stars=8, noise_arcsec=5.0):
        self.camera, self.min_stars, self.noise = camera, min_stars, noise_arcsec
        self.available = True
        self.calls = 0

    def solve(self, image, ra_hint=None, dec_hint=None, star_list=None):
        self.calls += 1
        tr = self.camera.last_truth
        if tr is None or (star_list is not None and len(star_list) < self.min_stars):
            return None
        ra, dec = astro.jnow_to_icrs(tr["ra"], tr["dec"], tr["t"])
        rng = np.random.default_rng(self.calls)
        n = rng.normal(0, self.noise / 3600.0, 2)
        # cd: full-resolution pixel offset from the centre -> tangent-plane (xi east, eta north), deg
        p = self.camera.pix_arcsec / 3600.0
        c, s = math.cos(math.radians(tr.get("pa", 0.0))), math.sin(math.radians(tr.get("pa", 0.0)))
        return {"ra": (ra + n[0] / max(0.05, math.cos(math.radians(dec)))) % 360.0, "dec": dec + n[1],
                "scale": self.camera.pix_arcsec, "cd": [[-p * c, -p * s], [p * s, -p * c]]}


class SimWorld:
    """All simulated devices sharing one clock."""

    def __init__(self, clock, lat, lon, home_ha=-90.0, ccw_limit=90.0, cw_limit=270.0, roof=True,
                 roof_open=True, roof_move_s=30.0, width=1080, height=1920, pix_arcsec=7.48,
                 pointing_error=(1.2, -0.8), track_error=0.0155,
                 seed=1, polar_error=None, park_drops_sync=True):
        self.clock = clock
        self.telescope = SimTelescope(clock, lat, lon, home_ha, ccw_limit, cw_limit,
                                      pointing_error=pointing_error, track_error=track_error,
                                      polar_error=polar_error, park_drops_sync=park_drops_sync)
        self.wheel = SimFilterWheel(clock)
        self.focuser = SimFocuser()
        self.switch = SimSwitch()
        self.dome = SimDome(clock, roof_open, roof_move_s) if roof else None
        self.sky = SimSky(seed=seed)
        self.camera = SimCamera(clock, self.telescope, self.wheel, self.sky, self.dome,
                                width=width, height=height, pix_arcsec=pix_arcsec)
        self.safety = SimSafety()
        self.solver = SimSolver(self.camera)
