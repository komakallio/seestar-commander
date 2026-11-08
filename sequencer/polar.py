"""Polar alignment: three-point measurement (TPPA-style) and a live adjust loop for the wedge knobs.

Measure: plate-solve the telephoto at N >= 3 positions that differ only in the RA axis angle (cable-safe
`move_hadec` inside the window, Dec fixed). Each solve is turned into an *apparent* direction in the
local horizon frame (ICRS -> FK5 of date, hour angle from LST, alt/az, plus refraction), i.e. where the
optics physically point. Those directions lie on a circle around the physical RA axis; the plane fitted
through them gives the axis, which is compared with the refracted (apparent) celestial pole.

Adjust: the RA axis stays put (tracking optional; its rotation is taken from the mount's unwrapped hour
angle). Each new solve gives the current direction v. Turning the knobs rotates the whole mount by
Q = Rz(-b) * R_hinge(a) (az knob about the vertical, alt knob about the horizontal hinge perpendicular to
the axis), so v = Q * Rot(axis, du) * v_ref; (a, b) are fitted and the axis becomes Q * axis.

Vectors are unit vectors in a local East-North-Up frame. Angles in degrees unless named *_arcmin.
"""
import copy
import json
import math
import os
import threading

import numpy as np

from . import astro, imaging

UP = np.array([0.0, 0.0, 1.0])
HA_RATE_MIN = astro.SIDEREAL_DEG_S * 60.0


# ---- geometry -------------------------------------------------------------------------------------

def enu(alt, az):
    a, z = np.radians(alt), np.radians(az)
    return np.stack([np.cos(a) * np.sin(z), np.cos(a) * np.cos(z), np.sin(a)], -1)


def altaz_of(v):
    v = np.asarray(v, float)
    v = v / np.linalg.norm(v, axis=-1, keepdims=True)
    return np.degrees(np.arcsin(np.clip(v[..., 2], -1, 1))), np.degrees(np.arctan2(v[..., 0], v[..., 1])) % 360.0


def rot(axis, ang_deg):
    """Right-handed rotation matrix about `axis` by ang_deg (Rodrigues)."""
    k = np.asarray(axis, float)
    k = k / np.linalg.norm(k)
    a = math.radians(ang_deg)
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(a) * K + (1 - math.cos(a)) * K @ K


def _sph(lon, lat, phi):
    """(ha, dec) -> (alt, az) and, by symmetry, (az, alt) -> (ha, dec), at latitude phi. Arrays, deg."""
    h, d, p = np.radians(lon), np.radians(lat), math.radians(phi)
    sa = np.sin(p) * np.sin(d) + np.cos(p) * np.cos(d) * np.cos(h)
    b = np.degrees(np.arcsin(np.clip(sa, -1, 1)))
    a = np.degrees(np.arctan2(-np.cos(d) * np.sin(h), np.sin(d) * np.cos(p) - np.cos(d) * np.cos(h) * np.sin(p)))
    return b, a % 360.0


def hadec_to_altaz(ha, dec, lat):
    return _sph(ha, dec, lat)


def altaz_to_hadec(alt, az, lat):
    dec, ha = _sph(az, alt, lat)
    return astro.wrap180(ha), dec


def refraction_arcmin(alt_true, pressure_hpa=1010.0, temp_c=10.0):
    """Saemundsson's refraction for a true (geometric) altitude, arcmin."""
    h = np.maximum(np.asarray(alt_true, float), -1.0)
    return 1.02 / np.tan(np.radians(h + 10.3 / (h + 5.11))) * (pressure_hpa / 1010.0) * (283.0 / (273.0 + temp_c))


def apparent_alt(alt_true, p=1010.0, t=10.0):
    return np.asarray(alt_true, float) + refraction_arcmin(alt_true, p, t) / 60.0


def true_alt(alt_app, p=1010.0, t=10.0):
    alt_app = np.asarray(alt_app, float)
    h = alt_app - refraction_arcmin(alt_app, p, t) / 60.0
    for _ in range(5):
        h = h - (apparent_alt(h, p, t) - alt_app)
    return h


def pole_vector(lat, p=1010.0, t=10.0):
    """Apparent (refracted) direction of the elevated celestial pole."""
    return enu(float(apparent_alt(abs(lat), p, t)), 0.0 if lat >= 0 else 180.0)


def hinge(axis):
    """Horizontal axis of the altitude adjustment for a polar axis (east for a northern pole)."""
    h = np.cross(axis, UP)
    return h / np.linalg.norm(h)


def knob_rotation(a, b, axis):
    """Mount rotation for an alt knob turn a (deg, + raises the axis) and an az knob turn b (+ moves the
    axis east, i.e. increasing azimuth), starting from polar axis `axis`."""
    return rot(UP, -b) @ rot(hinge(axis), a)


def axis_from_error(alt_arcmin, az_arcmin, lat, p=1010.0, t=10.0):
    """Polar axis with the given error against the refracted pole (az_arcmin is the on-sky arc)."""
    P = pole_vector(lat, p, t)
    alt_p, az_p = altaz_of(P)
    alt = float(alt_p) + alt_arcmin / 60.0
    return enu(alt, float(az_p) + az_arcmin / 60.0 / math.cos(math.radians(alt)))


def mount_rotation(alt_arcmin, az_arcmin, lat, p=1010.0, t=10.0):
    """Rotation taking a perfectly aligned mount (axis on the refracted pole) to the misaligned one:
    the alt knob about the pole's hinge, then the az knob about the vertical."""
    P = pole_vector(lat, p, t)
    alt_p = float(altaz_of(P)[0])
    alt = alt_p + alt_arcmin / 60.0
    daz = az_arcmin / 60.0 / math.cos(math.radians(alt))
    return rot(UP, -daz) @ rot(hinge(P), alt_arcmin / 60.0)


def correction_rotation(axis, lat, p=1010.0, t=10.0):
    """Mount rotation that brings `axis` onto the refracted pole by the knobs (inverse of mount_rotation)."""
    e = axis_error(axis, lat, p, t)
    return mount_rotation(e["alt_arcmin"], e["az_arcmin"], lat, p, t).T


def axis_error(axis, lat, p=1010.0, t=10.0):
    """Error of a polar axis against the refracted pole: alt (+ = too high) and az (+ = east of the pole,
    arcmin of sky arc), total, in arcmin."""
    P = pole_vector(lat, p, t)
    alt_a, az_a = altaz_of(axis)
    alt_p, az_p = altaz_of(P)
    d_alt = (float(alt_a) - float(alt_p)) * 60.0
    d_az = astro.wrap180(float(az_a) - float(az_p)) * math.cos(math.radians(float(alt_a))) * 60.0
    tot = math.degrees(math.acos(float(np.clip(np.dot(axis, P) / np.linalg.norm(axis), -1, 1)))) * 60.0
    return {"alt_arcmin": d_alt, "az_arcmin": d_az, "total_arcmin": tot}


def fit_axis(vs, du, lat, p=1010.0, t=10.0):
    """Plane fit through N >= 3 apparent directions taken at RA-axis angles du (deg, mount unwrapped HA).

    Returns the axis, the rotation sign (direction + angle about the axis per + mount HA), the plane rms
    (arcmin; zero for exactly 3 points), the radius, and the angle residual: how far each point is from
    where the encoder angles put it on the circle (arcmin on the sky)."""
    V = np.asarray(vs, float)
    du = np.asarray(du, float)
    if len(V) < 3:
        raise ValueError("need at least 3 points")
    c = V.mean(0)
    _, sv, vt = np.linalg.svd(V - c)
    n = vt[-1]
    P = pole_vector(lat, p, t)
    if np.dot(n, P) < 0:
        n = -n
    dist = (V - c) @ n
    plane_rms = float(np.sqrt(np.mean(dist ** 2))) * 180 / math.pi * 60
    radius = np.degrees(np.arccos(np.clip(V @ n, -1, 1)))
    e1 = V[0] - np.dot(V[0], n) * n
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(n, e1)
    phi = np.degrees(np.arctan2(V @ e2, V @ e1))
    dphi = np.array([astro.wrap180(x - phi[0]) for x in phi])
    ddu = du - du[0]
    sign = 1.0 if np.dot(dphi, ddu) >= 0 else -1.0
    ang_res = np.array([astro.wrap180(a - sign * b) for a, b in zip(dphi, ddu)])
    ang_res = ang_res - ang_res.mean()
    ang_res_arcmin = ang_res * np.sin(np.radians(radius)) * 60.0
    return {"axis": n, "sign": sign, "plane_rms_arcmin": plane_rms,
            "radius_deg": float(radius.mean()), "radius_spread_arcmin": float(np.ptp(radius) * 60.0),
            "angle_residual_arcmin": float(np.max(np.abs(ang_res_arcmin))), "span_deg": float(np.ptp(dphi)),
            "singular": [float(x) for x in sv]}


def fit_knobs(v_ref, v_obs, axis, x0=(0.0, 0.0)):
    """Knob turns (a, b) in deg with knob_rotation(a, b, axis) @ v_ref = v_obs (Gauss-Newton), plus
    the conditioning (smallest singular value of the Jacobian, 1 = ideal, near 0 = degenerate)."""
    x = np.array(x0, float)
    eps = 1e-3

    def f(x):
        return knob_rotation(x[0], x[1], axis) @ v_ref

    smin = 1.0
    for _ in range(8):
        r = v_obs - f(x)
        J = np.stack([(f(x + [eps, 0]) - f(x - [eps, 0])) / (2 * eps),
                      (f(x + [0, eps]) - f(x - [0, eps])) / (2 * eps)], 1)
        dx = np.linalg.lstsq(J, r, rcond=None)[0]
        x = x + dx
        smin = float(np.linalg.svd(J, compute_uv=False)[-1] * 180 / math.pi)
        if np.max(np.abs(dx)) < 1e-8:
            break
    return x, smin


def describe(err, cfg, tol=None):
    """Words and arrows for the operator. Default: the direction the axis must move. With
    [polar] alt_knob_sign / az_knob_sign (+1: clockwise raises / moves east; -1: the opposite) the
    knob direction is added."""
    tol = cfg.tolerance_arcmin if tol is None else tol
    out = {}
    for k, val, pos_word, neg_word, sign in (("alt", err["alt_arcmin"], "lower", "raise", cfg.alt_knob_sign),
                                             ("az", err["az_arcmin"], "turn west", "turn east", cfg.az_knob_sign)):
        word = pos_word if val > 0 else neg_word
        arrow = {"lower": "↓", "raise": "↑", "turn west": "→" if cfg.lat < 0 else "←",
                 "turn east": "←" if cfg.lat < 0 else "→"}[word]
        knob = None
        if sign:
            # +1 means clockwise moves the axis up (alt) / east (az)
            want_pos = word in ("raise", "turn east")
            knob = "clockwise" if (want_pos == (sign > 0)) else "counter-clockwise"
        ok = abs(val) <= tol
        out[k] = {"arcmin": round(val, 2), "word": "ok" if ok else word, "arrow": "" if ok else arrow,
                  "move": word, "knob": knob, "ok": ok}
    return out


# ---- frame geometry -------------------------------------------------------------------------------

def tangent(ra, dec, ra0, dec0):
    a, d = np.radians(ra), np.radians(dec)
    a0, d0 = math.radians(ra0), math.radians(dec0)
    cosc = math.sin(d0) * np.sin(d) + math.cos(d0) * np.cos(d) * np.cos(a - a0)
    xi = np.cos(d) * np.sin(a - a0) / cosc
    eta = (math.cos(d0) * np.sin(d) - math.sin(d0) * np.cos(d) * np.cos(a - a0)) / cosc
    return np.degrees(xi), np.degrees(eta)


def untangent(xi, eta, ra0, dec0):
    x, y = np.radians(xi), np.radians(eta)
    a0, d0 = math.radians(ra0), math.radians(dec0)
    rho = np.hypot(x, y)
    c = np.arctan(rho)
    with np.errstate(invalid="ignore", divide="ignore"):
        dec = np.where(rho == 0, d0, np.arcsin(np.cos(c) * math.sin(d0) + y * np.sin(c) * math.cos(d0) / np.where(rho == 0, 1, rho)))
    ra = a0 + np.arctan2(x * np.sin(c), rho * math.cos(d0) * np.cos(c) - y * math.sin(d0) * np.sin(c))
    return np.degrees(ra) % 360.0, np.degrees(dec)


def pix_to_sky(px, py, sol, W, H):
    cd = np.asarray(sol["cd"], float)
    xi, eta = cd @ np.array([np.asarray(px, float) - W / 2, np.asarray(py, float) - H / 2])
    return untangent(xi, eta, sol["ra"], sol["dec"])


def sky_to_pix(ra, dec, sol, W, H):
    xi, eta = tangent(ra, dec, sol["ra"], sol["dec"])
    d = np.linalg.solve(np.asarray(sol["cd"], float), np.array([xi, eta]))
    return d[0] + W / 2, d[1] + H / 2


def icrs_to_app(ra, dec, t, lat, lon, p=1010.0, temp=10.0):
    """ICRS (deg, arrays) at unix time t -> apparent ENU unit vectors (N, 3)."""
    from astropy.coordinates import FK5, SkyCoord
    from astropy.time import Time
    import astropy.units as u
    c = SkyCoord(np.atleast_1d(ra) * u.deg, np.atleast_1d(dec) * u.deg, frame="icrs") \
        .transform_to(FK5(equinox=Time(t, format="unix")))
    ha = astro.lst_deg(t, lon) - c.ra.deg
    alt, az = hadec_to_altaz(ha, c.dec.deg, lat)
    return enu(apparent_alt(alt, p, temp), az)


def app_to_jnow(v, t, lat, lon, p=1010.0, temp=10.0):
    """Apparent ENU vector -> geometric JNow (ra, dec) deg (the simulator's sky)."""
    alt_a, az = altaz_of(v)
    ha, dec = altaz_to_hadec(true_alt(alt_a, p, temp), az, lat)
    return (astro.lst_deg(t, lon) - ha) % 360.0, dec


# ---- configuration --------------------------------------------------------------------------------

class PolarConfig:
    DEFAULTS = dict(dec=65.0, span_deg=30.0, points=3, exposure_s=5.0, gain=80, adjust_exposure_s=None,
                    adjust_timeout_min=20.0, tolerance_arcmin=2.0, track_during_adjust=True, min_alt=15.0,
                    pressure_hpa=1010.0, temperature_c=10.0, alt_knob_sign=0, az_knob_sign=0,
                    solve_retries=2, cable_reserve_min=2.0)

    def __init__(self, lat, **kw):
        self.lat = lat
        for k, v in self.DEFAULTS.items():
            setattr(self, k, kw.get(k, v))
        if self.adjust_exposure_s is None:
            self.adjust_exposure_s = self.exposure_s

    @classmethod
    def from_settings(cls, s):
        raw = dict((s.extra or {}).get("polar", {}))
        return cls(s.lat, **{k: v for k, v in raw.items() if k in cls.DEFAULTS},
                   **({} if "cable_reserve_min" in raw else {"cable_reserve_min": s.cable_reserve_min}))

    def options(self, opts):
        """A copy with per-run overrides (only known keys; dec may be "current")."""
        c = copy.copy(self)
        for k, v in (opts or {}).items():
            if k not in self.DEFAULTS or v is None:
                continue
            d = self.DEFAULTS[k]
            if k == "dec" and v == "current":
                pass
            elif isinstance(d, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif isinstance(d, int):
                v = int(v)
            else:
                v = float(v)
            setattr(c, k, v)
        if "exposure_s" in (opts or {}) and not (opts or {}).get("adjust_exposure_s"):
            c.adjust_exposure_s = c.exposure_s
        return c


# ---- the job -------------------------------------------------------------------------------------

class Stop(RuntimeError):
    pass


class PolarController:
    """Owned by the Service. cmd_* are called from the HTTP/CLI threads; run() is the worker job."""

    def __init__(self, svc):
        self.svc = svc
        self.cfg = PolarConfig.from_settings(svc.s)
        self.lock = threading.RLock()
        self._done_req = False
        self.out = os.path.join(svc.s.data_dir, "polar")
        self.st = {"state": "idle", "phase": None, "msg": None, "points": [], "measured": None,
                   "live": None, "history": [], "target": None, "final": None, "config": self._cfg_view(self.cfg)}

    def _cfg_view(self, c):
        return {k: getattr(c, k) for k in PolarConfig.DEFAULTS} | {"lat": c.lat}

    def _set(self, **kw):
        with self.lock:
            self.st.update(kw)

    def status(self):
        with self.lock:
            s = copy.deepcopy(self.st)
        s["busy"] = self.svc.busy == "polar"
        sim = self.svc.rig.sim
        if sim is not None and getattr(sim.telescope, "polar", None) is not None:
            a, z = sim.telescope.polar
            s["sim_truth"] = {"alt_arcmin": round(a, 2), "az_arcmin": round(z, 2)}
        return s

    # ---- commands ----
    def plan_points(self, c):
        """Unwrapped RA-axis angles and Dec for the measurement, or raise ValueError (refusal)."""
        sc = self.svc.scope
        u0 = sc.ha_unwrapped()
        t = sc.t
        if c.dec == "current":
            if t.AtPark:
                raise ValueError("dec = current, but the scope is parked")
            dec = float(t.Declination)
        else:
            dec = float(c.dec)
            if c.lat < 0 and dec > 0:
                dec = -dec
        if not 20.0 <= abs(dec) <= 85.0:
            raise ValueError(f"Dec {dec:.1f}: use |Dec| 20..85 (60..70 is a good choice)")
        n, span = int(c.points), float(c.span_deg)
        if n < 3 or span < 5:
            raise ValueError("need points >= 3 and span_deg >= 5")
        us = list(np.linspace(u0 - span, u0 + span, n))
        lo, hi = sc.ha_u_min, sc.ha_u_max
        if us[0] < lo or us[-1] > hi:
            room = (max(lo + span, u0 - 1e9), min(hi - span, u0 + 1e9))
            hint = (f"; the centre HA must be within {room[0]:.0f}..{room[1]:.0f} (unwrapped) for +-{span:.0f}"
                    if room[0] <= room[1] else f"; the window is narrower than {2 * span:.0f} deg")
            raise ValueError(f"cable window {lo:.0f}..{hi:.0f} does not allow RA {us[0]:.0f}..{us[-1]:.0f} "
                             f"(+-{span:.0f} around unwrapped HA {u0:.1f}){hint}")
        for u in us:
            if sc.map_ha(astro.wrap180(u)) is None or abs(sc.map_ha(astro.wrap180(u)) - u) > 1e-6:
                raise ValueError(f"unwrapped HA {u:.1f} is not reachable unambiguously in the cable window")
            alt = float(hadec_to_altaz(astro.wrap180(u), dec, self.svc.s.lat)[0])
            if alt < c.min_alt:
                raise ValueError(f"point HA {astro.wrap180(u):.0f} Dec {dec:.0f} is at altitude {alt:.0f} "
                                 f"< {c.min_alt:.0f}; choose another Dec or centre")
        # sweep once and finish (where the adjust loop runs) at the lower end: the az knob turns about the
        # vertical, which moves a high-altitude field little, so a lower last point is better conditioned
        alts = [float(hadec_to_altaz(astro.wrap180(u), dec, self.svc.s.lat)[0]) for u in (us[0], us[-1])]
        if alts[0] < alts[1]:
            us = us[::-1]
        return [float(u) for u in us], dec

    def cmd_start(self, opts=None):
        svc = self.svc
        if svc.busy is not None or not svc.jobs.empty():
            return {"ok": False, "msg": f"busy ({svc.busy or 'queued jobs'}): stop first"}
        if svc._unsafe:
            return {"ok": False, "msg": "unsafe: " + ", ".join(svc.st["safety"]["reasons"])}
        if not getattr(svc.rig.solver, "available", False):
            return {"ok": False, "msg": f"plate solver unavailable ({getattr(svc.rig.solver, 'error', '')})"}
        try:
            c = self.cfg.options(opts)
            us, dec = self.plan_points(c)
        except (ValueError, RuntimeError) as e:
            return {"ok": False, "msg": f"refused: {e}"}
        self._done_req = False
        self._set(state="queued", phase=None, msg=None, points=[], measured=None, live=None, history=[],
                  target=None, final=None, config=self._cfg_view(c),
                  plan={"u": [round(u, 2) for u in us], "dec": round(dec, 2)})
        svc.jobs.put(("polar", {"us": us, "dec": dec, "cfg": c}))
        svc.log(f"polar alignment queued: RA axis at unwrapped HA {', '.join(f'{u:.1f}' for u in us)}, Dec {dec:.1f}")
        return {"ok": True, "msg": "polar alignment started", "u": us, "dec": dec}

    def cmd_stop(self):
        if self.svc.busy != "polar":
            return {"ok": True, "msg": "polar alignment not running"}
        self.svc.log("operator: polar stop")
        self.svc.abort.set()
        self.svc.imager.abort()
        return {"ok": True, "msg": "stopping"}

    def cmd_done(self):
        with self.lock:
            live, meas = self.st.get("live"), self.st.get("measured")
        src = live or meas
        if not src:
            return {"ok": False, "msg": "nothing measured yet"}
        t = self.svc.clock.time()
        rec = {"time": _iso(t), "unix": round(t, 1), "alt_arcmin": src["alt_arcmin"], "az_arcmin": src["az_arcmin"],
               "total_arcmin": src["total_arcmin"], "source": "live" if live else "measured",
               "initial": {k: meas[k] for k in ("alt_arcmin", "az_arcmin", "total_arcmin")} if meas else None,
               "tolerance_arcmin": self.st["config"]["tolerance_arcmin"], "sim": self.svc.rig.sim is not None}
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, "results.jsonl"), "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        self._set(final=rec)
        self.svc.log(f"polar alignment done: alt {rec['alt_arcmin']:+.1f}' az {rec['az_arcmin']:+.1f}' "
                     f"(total {rec['total_arcmin']:.1f}') stored")
        if self.svc.busy == "polar":
            self._done_req = True
            self.svc.abort.set()
            self.svc.imager.abort()
        return {"ok": True, "msg": "stored", "final": rec}

    def cmd_sim_knobs(self, body):
        tel = getattr(self.svc.rig.sim, "telescope", None)
        if tel is None:
            return {"ok": False, "msg": "not a simulator"}
        if "alt" in body or "az" in body:
            tel.set_polar(float(body.get("alt", 0.0)), float(body.get("az", 0.0)))
        else:
            tel.adjust_polar(float(body.get("dalt", 0.0)), float(body.get("daz", 0.0)))
        a, z = tel.polar
        self.svc.log(f"SIM: polar error now alt {a:+.1f}' az {z:+.1f}'")
        return {"ok": True, "alt_arcmin": a, "az_arcmin": z}

    def http_post(self, path, body):
        """(code, body) for POST /api/polar/<cmd> and /api/sim/polar."""
        if path == "/api/sim/polar":
            r = self.cmd_sim_knobs(body)
            return (200 if r["ok"] else 404), r
        cmd = path.rsplit("/", 1)[-1]
        if cmd == "start":
            return 200, self.cmd_start(body)
        if cmd == "stop":
            return 200, self.cmd_stop()
        if cmd == "done":
            return 200, self.cmd_done()
        return 404, {"ok": False, "msg": "unknown polar command"}

    # ---- worker job ----
    def run(self, us, dec, cfg):
        svc, sc = self.svc, self.svc.scope
        sc.interrupt = svc.abort.is_set
        svc._set(state="polar", phase="polar measure")
        self._cfg = cfg
        os.makedirs(self.out, exist_ok=True)
        try:
            svc._unpark_if_needed()
            meas = self._measure(us, dec, cfg)
            self._adjust(meas, cfg)
            self._set(state="finished", phase=None)
        except Stop as e:
            self._set(state="stopped", phase=None, msg=str(e))
            svc.log(f"polar: {e}")
        except Exception as e:  # noqa: BLE001 - Aborted / Interrupted / solver failures
            if svc.abort.is_set():
                why = "done" if self._done_req else ("stopped" if not svc._unsafe else "aborted (unsafe)")
                self._set(state="done" if self._done_req else "stopped", phase=None, msg=why)
                svc.log(f"polar alignment {why}")
            else:
                self._set(state="error", phase=None, msg=str(e))
                raise
        finally:
            sc.interrupt = None
            try:
                if sc.t.Tracking:
                    sc.t.Tracking = False
            except Exception:  # noqa: BLE001
                pass
            if svc.st.get("state") in ("polar", "stopping"):
                svc._set(state="idle", phase=None)

    def _shot(self, cfg, exp, hint):
        """Expose, detect, solve. Returns (frame, green, stars, solution or None, t_mid, u_mid): the
        exposure mid time and the RA-axis angle then (interpolated between reads before and after)."""
        svc, sc, clock = self.svc, self.svc.scope, self.svc.clock
        t_a = clock.time()
        u_a = sc.ha_unwrapped()
        d = svc.imager.expose(exp, cfg.gain, svc.abort)
        t_mid = getattr(svc.imager, "last_start", t_a) + exp / 2.0
        t_b = clock.time()
        u_b = sc.ha_unwrapped()
        u_mid = u_a + (u_b - u_a) * (t_mid - t_a) / max(1e-6, t_b - t_a)
        g = imaging.green(d)
        st = imaging.stars(g, n=120)
        sol = svc.rig.solver.solve(d, ra_hint=hint[0] if hint else None, dec_hint=hint[1] if hint else None,
                                   star_list=st)
        return d, g, st, sol, t_mid, u_mid

    def _hint(self):
        p = self.svc.scope.position()
        return p["ra_h"] * 15.0, p["dec"]

    def _measure(self, us, dec, cfg):
        svc, sc, S = self.svc, self.svc.scope, self.svc.s
        pts, vs, du = [], [], []
        self._set(state="measuring", phase="measure")
        for i, u in enumerate(us):
            svc._check_abort()
            self._set(msg=f"point {i + 1}/{len(us)}: moving RA to unwrapped HA {u:.1f}")
            if i == 0:
                sc.move_hadec(astro.wrap180(u), dec)
            else:
                # Dec is already there: pass the current reading, so only the RA axis moves
                sc.move_hadec(astro.wrap180(u), float(sc.t.Declination))
            if sc.t.Tracking:
                sc.t.Tracking = False
            svc._check_abort()
            sol = None
            exp = cfg.exposure_s
            for attempt in range(1 + int(cfg.solve_retries)):
                self._set(msg=f"point {i + 1}/{len(us)}: exposing {exp:g} s")
                d, g, st, sol, t_mid, u_mid = self._shot(cfg, exp, self._hint())
                imaging.preview(g, os.path.join(svc.out, "latest.jpg"))
                svc._img("latest")
                if sol is not None:
                    break
                svc.log(f"polar point {i + 1}: no solution ({len(st)} stars), retry {attempt + 1}")
                exp *= 2
            if sol is None:
                raise Stop(f"point {i + 1}: plate solve failed")
            v = icrs_to_app(sol["ra"], sol["dec"], t_mid, S.lat, S.lon, cfg.pressure_hpa, cfg.temperature_c)[0]
            alt, az = altaz_of(v)
            pt = {"i": i + 1, "u": round(float(u_mid), 3), "ra": round(float(sol["ra"]), 5),
                  "dec": round(float(sol["dec"]), 5), "t": round(float(t_mid), 2), "alt": round(float(alt), 3), "az": round(float(az), 3), "stars": len(st)}
            pts.append(pt)
            vs.append(v)
            du.append(u_mid)
            with self.lock:
                self.st["points"] = copy.deepcopy(pts)
            svc.log(f"polar point {i + 1}/{len(us)}: HA {astro.wrap180(u_mid):.2f} solved RA {sol['ra']:.3f} "
                    f"Dec {sol['dec']:.3f} (J2000) -> alt {float(alt):.2f} az {float(az):.2f}")
        f = fit_axis(vs, du, S.lat, cfg.pressure_hpa, cfg.temperature_c)
        err = axis_error(f["axis"], S.lat, cfg.pressure_hpa, cfg.temperature_c)
        res = {**{k: round(float(v), 2) for k, v in err.items()}, "words": describe(err, cfg),
               "plane_rms_arcmin": round(f["plane_rms_arcmin"], 2),
               "angle_residual_arcmin": round(f["angle_residual_arcmin"], 2),
               "radius_deg": round(f["radius_deg"], 3), "span_deg": round(f["span_deg"], 2),
               "axis_alt": round(float(altaz_of(f["axis"])[0]), 4), "axis_az": round(float(altaz_of(f["axis"])[1]), 4),
               "pole_alt": round(float(altaz_of(pole_vector(S.lat, cfg.pressure_hpa, cfg.temperature_c))[0]), 4),
               "time": _iso(svc.clock.time())}
        self._set(measured=res, msg="measured")
        with open(os.path.join(self.out, "last_measurement.json"), "w") as fh:
            json.dump({"result": res, "points": pts}, fh, indent=1)
        svc.log(f"polar error: alt {res['alt_arcmin']:+.1f}' ({res['words']['alt']['move']}), az "
                f"{res['az_arcmin']:+.1f}' ({res['words']['az']['move']}), total {res['total_arcmin']:.1f}'; "
                f"fit residual {res['angle_residual_arcmin']:.2f}' (angles), {res['plane_rms_arcmin']:.2f}' (plane)")
        return {"axis": f["axis"], "sign": f["sign"], "v_ref": vs[-1], "u_ref": du[-1], "sol": sol}

    def _adjust(self, meas, cfg):
        svc, sc, S = self.svc, self.svc.scope, self.svc.s
        lat, lon, pr, tc = S.lat, S.lon, cfg.pressure_hpa, cfg.temperature_c
        self._set(state="adjusting", phase="adjust", msg="turn the knobs; the numbers update after each solve")
        svc._set(phase="polar adjust")
        u_now = sc.ha_unwrapped()
        need = cfg.adjust_timeout_min + cfg.cable_reserve_min
        left = (sc.ha_u_max - u_now) / HA_RATE_MIN
        tracking = bool(cfg.track_during_adjust) and left >= need
        if cfg.track_during_adjust and not tracking:
            svc.log(f"polar adjust: tracking off ({left:.0f} min of cable room < {need:.0f})")
        if tracking:
            sc.t.Tracking = True
        t_end = svc.clock.time() + cfg.adjust_timeout_min * 60.0
        A0, v_ref, u_ref, sign = meas["axis"], meas["v_ref"], meas["u_ref"], meas["sign"]
        hint = (meas["sol"]["ra"], meas["sol"]["dec"])
        star = None
        n = 0
        while True:
            svc._check_abort()
            if svc.clock.time() > t_end:
                raise Stop(f"adjust timeout ({cfg.adjust_timeout_min:g} min)")
            if tracking and (sc.ha_u_max - sc.ha_unwrapped()) / HA_RATE_MIN < cfg.cable_reserve_min:
                sc.t.Tracking = False
                tracking = False
                svc.log("polar adjust: cable reserve reached, tracking off")
            d, g, st, sol, t_mid, u_mid = self._shot(cfg, cfg.adjust_exposure_s, hint)
            n += 1
            if sol is None:
                self._set(msg=f"frame {n}: no solution ({len(st)} stars)")
                continue
            hint = (sol["ra"], sol["dec"])
            H, W = d.shape
            # centre, two pixel offsets (for the local pixel -> direction map) and the star, in one go
            star = self._pick_star(st, sol, W, H, star)
            pxs = [W / 2, W / 2 + 100, W / 2] + ([star["x"]] if star else [])
            pys = [H / 2, H / 2, H / 2 + 100] + ([star["y"]] if star else [])
            ras, decs = pix_to_sky(np.array(pxs), np.array(pys), sol, W, H)
            V = icrs_to_app(ras, decs, t_mid, lat, lon, pr, tc)
            v_obs = V[0]
            v_r = rot(A0, sign * (u_mid - u_ref)) @ v_ref
            x, smin = fit_knobs(v_r, v_obs, A0)
            fit_res = float(np.degrees(np.linalg.norm(knob_rotation(x[0], x[1], A0) @ v_r - v_obs))) * 60.0
            if fit_res > 1.0:
                self._set(msg=f"frame {n}: knob fit failed ({fit_res:.1f}' off), skipped")
                continue
            A = knob_rotation(x[0], x[1], A0) @ A0
            err = axis_error(A, lat, pr, tc)
            live = {**{k: round(float(v), 2) for k, v in err.items()}, "words": describe(err, cfg), "frame": n,
                    "time": _iso(svc.clock.time()), "conditioning": round(smin, 3), "tracking": tracking,
                    "stars": len(st), "knobs_deg": [round(float(x[0]), 4), round(float(x[1]), 4)],
                    "ra_axis_deg": round(float(u_mid - u_ref), 3), "fit_arcmin": round(fit_res, 3)}
            tgt = None
            if star:
                J = np.stack([(V[1] - V[0]) / 100.0, (V[2] - V[0]) / 100.0], 1)
                C = correction_rotation(A, lat, pr, tc)
                w = C.T @ V[3]
                dxy = np.linalg.lstsq(J, w - V[3], rcond=None)[0]      # from the star, first order
                rx, ry = float(star["x"] + dxy[0]), float(star["y"] + dxy[1])
                dist = math.hypot(rx - star["x"], ry - star["y"])
                per = dist / err["total_arcmin"] if err["total_arcmin"] > 0.2 else 60.0 / (S.pix_arcsec or 3.74)
                tol_px = max(3.0, cfg.tolerance_arcmin * per)
                crop = self._crop(g, star["x"], star["y"], rx, ry, tol_px)
                tgt = {"star": [round(star["x"], 1), round(star["y"], 1)], "reticle": [round(rx, 1), round(ry, 1)],
                       "distance_px": round(dist, 1), "tolerance_px": round(tol_px, 1), "frame": [W, H],
                       "crop": crop, "flux": round(star["flux"], 0)}
            with self.lock:
                self.st["live"] = live
                self.st["target"] = tgt
                self.st["history"] = (self.st["history"] + [{"t": round(svc.clock.time(), 1), "alt": live["alt_arcmin"],
                                                             "az": live["az_arcmin"], "total": live["total_arcmin"]}])[-600:]
                self.st["msg"] = f"frame {n}: total {live['total_arcmin']:.1f}'"
            imaging.preview(g, os.path.join(svc.out, "latest.jpg"))
            svc._img("latest")

    def _pick_star(self, st, sol, W, H, prev):
        """Keep following the previous star (by sky position), else the brightest one away from the edges."""
        cand = [(2 * float(s[1]), 2 * float(s[0]), float(s[2])) for s in st]
        if not cand:
            return None
        if prev is not None:
            px, py = sky_to_pix(prev["ra"], prev["dec"], sol, W, H)
            best = min(cand, key=lambda c: math.hypot(c[0] - px, c[1] - py))
            if math.hypot(best[0] - px, best[1] - py) < max(20.0, 0.02 * W):
                ra, dec = pix_to_sky(best[0], best[1], sol, W, H)
                return {"x": best[0], "y": best[1], "flux": float(best[2]), "ra": float(ra), "dec": float(dec)}
        inner = [c for c in cand if 0.2 * W < c[0] < 0.8 * W and 0.2 * H < c[1] < 0.8 * H] or cand
        b = max(inner, key=lambda c: c[2])
        ra, dec = pix_to_sky(b[0], b[1], sol, W, H)
        return {"x": b[0], "y": b[1], "flux": float(b[2]), "ra": float(ra), "dec": float(dec)}

    def _crop(self, g, sx, sy, rx, ry, tol_px, out=360):
        """Square crop (full-resolution coordinates) holding the star and the reticle; saved as a JPEG."""
        from PIL import Image
        cx, cy = (sx + rx) / 2, (sy + ry) / 2
        half = max(150.0, abs(sx - rx) / 2 + 80, abs(sy - ry) / 2 + 80, tol_px + 40)
        x0, y0, size = cx - half, cy - half, 2 * half
        # green superpixels are half resolution
        gx0, gy0, gs = int(math.floor(x0 / 2)), int(math.floor(y0 / 2)), max(2, int(math.ceil(size / 2)))
        buf = np.zeros((gs, gs), np.float32)
        h, w = g.shape
        ys0, xs0 = max(0, gy0), max(0, gx0)
        ys1, xs1 = min(h, gy0 + gs), min(w, gx0 + gs)
        if ys1 > ys0 and xs1 > xs0:
            buf[ys0 - gy0:ys1 - gy0, xs0 - gx0:xs1 - gx0] = g[ys0:ys1, xs0:xs1]
            inside = buf[ys0 - gy0:ys1 - gy0, xs0 - gx0:xs1 - gx0]
            lo, hi = np.percentile(inside, 20), np.percentile(inside, 99.9)
            img = np.clip((buf - lo) / max(hi - lo, 1e-6), 0, 1) ** 0.5
        else:
            img = buf
        im = Image.fromarray((img * 255).astype(np.uint8)).resize((out, out), Image.BILINEAR)
        imaging._save_atomic(im, os.path.join(self.svc.out, "polar_crop.jpg"))
        self.svc._img("polar_crop")
        return {"x0": round(gx0 * 2.0, 1), "y0": round(gy0 * 2.0, 1), "size": round(gs * 2.0, 1), "px": out}


def _iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
