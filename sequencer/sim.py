"""Simulated SeeStar devices with the same attribute/method names as the alpyca clients, so the real
motion code (tools/scope.py: closed-loop MoveAxis, unwrapped HA, guarded Park) runs unchanged.

What is modelled:
  - mount on an EQ wedge: mechanical RA-axis angle (unwrapped, so tests can assert the cable never
    left its window), Dec axis that stops at -84.6 under MoveAxis, tracking with a rate error
    (drift), a fixed index error in HA and Dec (`pointing_error`) that SyncToCoordinates hides, tracking
    refused below the horizon, a firmware Park that goes to the home angle the *wrong* way round when
    it starts far from home and, like the real firmware, forgets the sync once it is parked
    (`park_drops_sync`), firmware gotos that are refused and counted (the sequencer must never call
    them), optional MoveAxis start/stop delays per axis (`axis_delays`) and whether a MoveAxis on RA
    adds to or replaces tracking (`moveaxis_replaces_tracking`), for the dither pulses;
  - optional polar misalignment (`polar_error=(alt_arcmin, az_arcmin)` of the RA axis against the
    refracted pole, with refraction): the true pointing and the camera roll follow the misaligned axis,
    so tracking drifts and the solver sees it; `adjust_polar` / `set_polar` stand in for the operator
    turning the wedge knobs;
  - telephoto camera rendering a deterministic random star field at the true pointing (no stars on
    the Dark filter or with the roof closed), with bias, dark current and noise;
  - wide camera (camera 1, own focuser): a sparse star field at the true pointing over a ~33 x 58 deg
    field, optional vignetting, its own exposure timing and an ImageArray transfer time, so it runs
    alongside the telephoto; a wide-field solver that knows the truth (and can be told to fail);
  - telephoto optics: vignetting (corners at about 75 %) and a few dust donuts whose position moves with
    the focuser, applied to everything that comes through the lens (sky, stars, the flat panel);
  - a flat panel (`SimPanel`) in front of both lenses: when it's on, the cameras see its even light
    (scaled by brightness, filter and a per-Bayer-channel colour) instead of the sky;
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
                 pressure_hpa=1010.0, temp_c=10.0, park_drops_sync=True, axis_delays=None,
                 moveaxis_replaces_tracking=False, single_axis_moveaxis=True):
        self.clock, self.lat, self.lon = clock, lat, lon
        # MoveAxis command delays per axis, (start_s, stop_s): a rate takes effect this long after the call
        # (nonzero rate: start delay, zero: stop delay). Default none; tests use the measured ones.
        self.axis_delays = tuple(tuple(map(float, d)) for d in (axis_delays or ((0.0, 0.0), (0.0, 0.0))))
        # False: a MoveAxis rate on RA adds to sidereal tracking; True: it replaces it while nonzero
        self.moveaxis_replaces_tracking = bool(moveaxis_replaces_tracking)
        self._pending = []               # scheduled rate changes (t, axis, rate), from the delays
        # measured on the SeeStar (T3b): a nonzero MoveAxis on one axis stops the other one
        self.single_axis_moveaxis = bool(single_axis_moveaxis)
        self._pulse = {}                 # axis -> (rate deg/s, end time) for PulseGuide
        self.GuideRateRightAscension = self.GuideRateDeclination = 0.0208904
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
            t = now - dt
            while self._pending and self._pending[0][0] <= now:     # rate changes inside this interval
                te, ax, r = self._pending.pop(0)
                self._integrate(max(0.0, te - t))
                t = max(t, te)
                self._rate[ax] = r
            self._integrate(now - t)
            t0 = now - dt
            for ax, (r, end) in list(self._pulse.items()):     # PulseGuide: rate until its end time
                d = max(0.0, min(now, end) - t0)
                if ax == 0:
                    self._u += r * d
                else:
                    self._dec = max(-84.6, min(90.0, self._dec + r * d))
                if now >= end:
                    del self._pulse[ax]
        self.u_min_seen = min(self.u_min_seen, self._u)
        self.u_max_seen = max(self.u_max_seen, self._u)
        if not self.phys_min <= self._u <= self.phys_max:
            self.cable_violation = True

    def _integrate(self, dt):
        if dt <= 0:
            return
        self._u += self._rate[0] * dt
        if self._tracking and not (self.moveaxis_replaces_tracking and self._rate[0] != 0):
            self._u += astro.SIDEREAL_DEG_S * (1.0 + self.track_error) * dt
        self._dec = max(-84.6, min(90.0, self._dec + self._rate[1] * dt))

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
            return self._park_target is not None or any(r != 0 for r in self._rate) or bool(self._pending)

    def MoveAxis(self, axis, rate):
        with self._lock:
            self._advance()
            if self._parked and rate != 0:
                raise SimError("parked: Unpark first")
            if abs(rate) > 6.0:
                raise SimError("rate out of range")
            if not hasattr(axis, "value"):          # like alpyca: needs a TelescopeAxes, not an int
                raise AttributeError(f"'{type(axis).__name__}' object has no attribute 'value'")
            ax, rate = int(axis), float(rate)
            other = 1 - ax
            delay = self.axis_delays[ax][0 if rate != 0 else 1]
            if delay > 0:
                self._pending.append((self.clock.time() + delay, ax, rate))
                if self.single_axis_moveaxis and rate != 0:
                    self._pending.append((self.clock.time() + delay, other, 0.0))
                self._pending.sort(key=lambda e: e[0])
            else:
                self._pending = [e for e in self._pending if e[1] != ax]
                self._rate[ax] = rate
                if self.single_axis_moveaxis and rate != 0:
                    self._pending = [e for e in self._pending if e[1] != other]
                    self._rate[other] = 0.0

    def PulseGuide(self, direction, duration_ms):
        """West = +HA, East = -HA, North = +Dec, South = -Dec (assumed; T3c measures the real one)."""
        with self._lock:
            self._advance()
            if self._parked:
                raise SimError("parked: Unpark first")
            d = int(direction)
            ax = 0 if d in (2, 3) else 1
            rate = self.GuideRateRightAscension if ax == 0 else self.GuideRateDeclination
            sign = {0: 1, 1: -1, 2: -1, 3: 1}[d]
            self._pulse[ax] = (sign * rate, self.clock.time() + duration_ms / 1000.0)

    @property
    def IsPulseGuiding(self):
        with self._lock:
            self._advance()
            return bool(self._pulse)

    def AbortSlew(self):
        with self._lock:
            self._advance()
            self._park_target = None
            self._rate = [0.0, 0.0]
            self._pending = []

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
            self._pending = []
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
            cols = range(int(math.floor(ra0 - w)), int(math.floor(ra0 + w)) + 1)
            if len(cols) >= 360:            # a wide field near the pole: every cell once
                cols = range(360)
            for i in cols:
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


class SimPanel:
    """Flat panel in front of the scope. rate: ADU/s on the telephoto (IR, gain 80, brightness 255)."""
    FILTER_T = {"IR": 1.0, "LP": 0.3, "Dark": 0.0, "none": 1.0}
    CFA = {(0, 0): 1.0, (0, 1): 0.8, (1, 0): 0.6, (1, 1): 1.0}     # GRBG: G R / B G, a warm panel

    def __init__(self, rate=40000.0):
        self.on, self.brightness, self.rate = False, 255, float(rate)
        self.switches = []                   # (on, brightness) history, for tests

    def set(self, on, brightness=None):
        self.on = bool(on)
        if brightness is not None:
            self.brightness = int(brightness)
        self.switches.append((self.on, self.brightness))

    def flux(self, filt):
        """ADU/s at gain 80 through filter filt, 0 when off."""
        return self.rate * self.brightness / 255.0 * self.FILTER_T.get(filt, 1.0) if self.on else 0.0

    def cfa(self, H, W):
        m = np.empty((H, W), np.float32)
        for (dy, dx), v in self.CFA.items():
            m[dy::2, dx::2] = v
        return m


class SimCamera:
    """Telephoto camera. ImageArray is [x][y] like Alpaca (the client transposes)."""

    def __init__(self, clock, telescope, wheel, sky, dome=None, width=1080, height=1920,
                 pix_arcsec=7.48, seeing_px=2.4, download_s=3.0, seed=7, panel=None, focuser=None,
                 optics=True):
        self.clock, self.tel, self.wheel, self.sky, self.dome = clock, telescope, wheel, sky, dome
        self.panel, self.focuser, self.optics = panel, focuser, bool(optics)
        self._ff = None
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
        self.starts = []                     # StartExposure times (tests compare the cadence)

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
                     "open": self.dome.is_open() if self.dome else True,
                     "panel": self.panel.flux(self.wheel.current()) if self.panel else 0.0,
                     "focus": self.focuser.Position if self.focuser else 1600}
        self.exposures += 1
        self.starts.append(t0)

    # dust donuts: (x, y) as a fraction of the frame, outer radius px, depth; they move with focus
    DUST = ((0.30, 0.25, 34.0, 0.14), (0.68, 0.62, 26.0, 0.10), (0.45, 0.80, 44.0, 0.08))

    def flatfield(self, H, W, focus=1600):
        """Multiplicative response of the optics: vignetting x dust donuts (1 = no loss)."""
        key = (H, W, int(focus))
        if self._ff is not None and self._ff[0] == key:
            return self._ff[1]
        if not self.optics:
            ff = np.ones((H, W), np.float32)
        else:
            yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
            r2 = ((xx - W / 2) ** 2 + (yy - H / 2) ** 2) / ((W / 2) ** 2 + (H / 2) ** 2)
            ff = 1.0 - 0.25 * r2
            sh = (int(focus) - 1600) * 0.05             # px per focuser step
            for fx, fy, rad, depth in self.DUST:
                cx, cy = fx * W + sh, fy * H + 0.5 * sh
                r = np.hypot(xx - cx, yy - cy) / rad
                # a donut: a dark ring with a lighter centre (the out-of-focus shadow of a speck)
                ff *= 1.0 - depth * (np.exp(-((r - 0.75) / 0.18) ** 2) + 0.45 * (r < 0.75))
            ff = ff.astype(np.float32)
        self._ff = (key, ff)
        return ff

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
        sig = np.zeros((H, W), np.float32)              # light through the lens (gets the flat-field)
        panel = e.get("panel", 0.0) if e["light"] and e["filter"] != "Dark" else 0.0
        sky_on = e["light"] and e["filter"] != "Dark" and e["open"] and not panel
        if panel:                                       # the panel covers the aperture: no sky
            sig += panel * dur * g * self.panel.cfa(H, W)
            self.last_truth = None
        elif sky_on:
            sig += 300.0 * dur / 60.0 * g
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
                    self._stamps(sig, x[ok], y[ok], flux[ok] * dur / 60.0 * g / samples)
                for (bra, bdec, bs, bp) in self.sky.blobs:
                    xi, eta, cosc = _tangent(np.array([bra]), np.array([bdec]), rk, dk)
                    if cosc[0] <= 0:
                        continue
                    bx = W / 2 - xi[0] * 3600.0 / self.pix_arcsec
                    by = H / 2 - eta[0] * 3600.0 / self.pix_arcsec
                    sp = bs * 3600.0 / self.pix_arcsec
                    yy, xx = np.mgrid[0:H, 0:W]
                    sig += (bp * dur / 60.0 * g / samples
                            * np.exp(-((xx - bx) ** 2 + (yy - by) ** 2) / (2 * sp * sp))).astype(np.float32)
        else:
            self.last_truth = None
        if panel or sky_on:
            img += sig * self.flatfield(H, W, e.get("focus", 1600))
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


class SimWideCamera(SimCamera):
    """Wide camera (Alpaca camera 1): no filter wheel, ~33 x 58 deg field, vignetting (corners at about
    46 % of the centre, as measured), its own exposure state, and an ImageArray that takes transfer_s
    of (simulated) time, like the ~2.5 s download over the LAN. intervals: [start, end, how] of every
    exposure ("done" or "aborted"), so tests can check it never overlapped a mount move."""

    def __init__(self, clock, telescope, sky, dome=None, width=540, height=960, pix_arcsec=220.0,
                 seeing_px=1.0, readout_s=0.5, transfer_s=2.5, vignetting=True, seed=11, panel=None):
        super().__init__(clock, telescope, None, sky, dome, width=width, height=height, pix_arcsec=pix_arcsec,
                         seeing_px=seeing_px, download_s=readout_s, seed=seed, panel=panel, optics=False)
        self.Name, self.SensorName, self.Gain = "Sim wide", "SIMWIDE", 100
        self.transfer_s, self.vignetting = float(transfer_s), bool(vignetting)
        self.intervals = []
        self.downloads = []                  # (start, end) of ImageArray transfers
        self._vig = None

    def StartExposure(self, duration, light=True):
        if self._exp is not None and not self._exp.get("done"):
            raise SimError("exposure already running")
        t0 = self.clock.time()
        self._exp = {"t0": t0, "dur": float(duration), "light": bool(light), "gain": self.Gain,
                     "start": self.tel.true_radec(), "filter": "none",
                     "pa": self.tel.true_pa() if hasattr(self.tel, "true_pa") else 0.0,
                     "open": self.dome.is_open() if self.dome else True,
                     "panel": self.panel.flux("none") * 0.15 if self.panel else 0.0}   # small lens, no filter
        self.exposures += 1
        self.starts.append(t0)
        self.intervals.append([t0, t0 + float(duration), "done"])

    def AbortExposure(self):
        if self._exp and not self.ImageReady:
            self.aborts += 1
            now = self.clock.time()
            if self.intervals and now < self.intervals[-1][1]:
                self.intervals[-1][1:] = [now, "aborted"]
        self._exp = None

    @property
    def ImageArray(self):
        t0 = self.clock.time()
        self.clock.sleep(self.transfer_s)
        img = SimCamera.ImageArray.fget(self)
        self.downloads.append((t0, self.clock.time()))
        return img

    def _vignette(self, H, W):
        if self._vig is None or self._vig.shape != (H, W):
            yy, xx = np.mgrid[0:H, 0:W]
            r2 = ((xx - W / 2) ** 2 + (yy - H / 2) ** 2) / ((W / 2) ** 2 + (H / 2) ** 2)
            self._vig = (1.0 - 0.54 * r2).astype(np.float32) if self.vignetting else np.ones((H, W), np.float32)
        return self._vig

    def _render(self, e):
        W, H = self.CameraXSize, self.CameraYSize
        dur, g = e["dur"], e["gain"] / 100.0
        sig = np.zeros((H, W), np.float32)
        if e["light"] and e.get("panel"):
            sig += e["panel"] * dur * g * self.panel.cfa(H, W)
            self.last_truth = None
        elif e["light"] and e["open"]:
            sig += 120.0 * dur / 10.0 * g
            (ra_s, dec_s), (ra_e, dec_e) = e["start"], e["end"]
            ra0, dec0 = (ra_s + astro.wrap180(ra_e - ra_s) / 2) % 360.0, (dec_s + dec_e) / 2
            pa = e.get("pa", 0.0)
            self.last_truth = {"ra": ra0, "dec": dec0, "t": e["t0"] + dur / 2, "pa": pa,
                               "pix_arcsec": self.pix_arcsec, "width": W, "height": H}
            rad = math.hypot(W, H) / 2 * self.pix_arcsec / 3600.0
            ra, dec, flux = self.sky.stars_near(ra0, dec0, rad)
            if len(ra):
                xi, eta, cosc = _tangent(ra, dec, ra0, dec0)
                if pa:
                    c, s_ = math.cos(math.radians(pa)), math.sin(math.radians(pa))
                    xi, eta = xi * c - eta * s_, xi * s_ + eta * c
                x = W / 2 - xi * 3600.0 / self.pix_arcsec
                y = H / 2 - eta * 3600.0 / self.pix_arcsec
                ok = (cosc > 0.5) & (x > -3) & (x < W + 3) & (y > -3) & (y < H + 3)
                self._stamps(sig, x[ok], y[ok], flux[ok] * dur / 10.0 * g)
        else:
            self.last_truth = None
        img = sig * self._vignette(H, W) + 300.0 + 0.5 * dur
        img += self._rng.normal(0, 8.0, img.shape).astype(np.float32)
        return np.clip(img, 0, 65535).astype(np.uint16)


class SimWideSolver:
    """Wide-field solver standing in for the projections solver: reads the simulated truth that came
    with the frame. fail: None, "none" (no solution) or "raise" (a solver error)."""
    name = "sim-wide"

    def __init__(self):
        self.available, self.error = True, None
        self.fail = None
        self.calls = 0

    def solve(self, job):
        self.calls += 1
        if self.fail == "raise":
            raise RuntimeError("simulated solver crash")
        tr = job.get("truth")
        if self.fail == "none" or not tr:
            return None
        ra, dec = astro.jnow_to_icrs(tr["ra"], tr["dec"], tr["t"])
        p = tr["pix_arcsec"] / 3600.0
        c, s = math.cos(math.radians(tr["pa"])), math.sin(math.radians(tr["pa"]))
        W, H = tr["width"], tr["height"]
        return {"solver": self.name, "ra": ra % 360.0, "dec": dec, "units": "full",
                "tan": {"crpix": [W / 2 + 1.0, H / 2 + 1.0], "crval": [ra % 360.0, dec],
                        "cd": [[-p * c, -p * s], [p * s, -p * c]]}, "stars": 300, "rms_px": 0.4}


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
                 seed=1, polar_error=None, park_drops_sync=True, axis_delays=None,
                 moveaxis_replaces_tracking=False, wide_size=(540, 960), wide_pix_arcsec=220.0,
                 wide_transfer_s=2.5, wide_vignetting=True, tele_optics=True):
        self.clock = clock
        self.telescope = SimTelescope(clock, lat, lon, home_ha, ccw_limit, cw_limit,
                                      pointing_error=pointing_error, track_error=track_error,
                                      polar_error=polar_error, park_drops_sync=park_drops_sync,
                                      axis_delays=axis_delays, moveaxis_replaces_tracking=moveaxis_replaces_tracking)
        self.wheel = SimFilterWheel(clock)
        self.focuser = SimFocuser()
        self.switch = SimSwitch()
        self.dome = SimDome(clock, roof_open, roof_move_s) if roof else None
        self.sky = SimSky(seed=seed)
        self.panel = SimPanel()
        self.camera = SimCamera(clock, self.telescope, self.wheel, self.sky, self.dome,
                                width=width, height=height, pix_arcsec=pix_arcsec, panel=self.panel,
                                focuser=self.focuser, optics=tele_optics)
        self.safety = SimSafety()
        self.solver = SimSolver(self.camera)
        self.wide_sky = SimSky(density=3, seed=seed + 101)          # only the brighter stars show at 55"/px
        self.wide_camera = SimWideCamera(clock, self.telescope, self.wide_sky, self.dome, width=wide_size[0],
                                         height=wide_size[1], pix_arcsec=wide_pix_arcsec,
                                         transfer_s=wide_transfer_s, vignetting=wide_vignetting,
                                         panel=self.panel)
        self.wide_focuser = SimFocuser()
        self.wide_focuser.Position, self.wide_focuser.MaxStep = 512, 1023
        self.wide_solver = SimWideSolver()
