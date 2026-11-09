"""Dithering: small offsets of the pointing between lights, so hot pixels and other sensor defects land
on different sky pixels and the stack's sigma clipping removes them.

How the offsets are made (the hard part). A dither is tens of arcseconds (12-20 telephoto pixels of
3.74"), below what the closed-loop axis moves in tools/scope.py can do: their fine stage runs at
0.05 deg/s without compensating the MoveAxis command delays (measured on one S30 Pro: RA starts 0.28 s
and stops 0.26 s after the call, Dec 0.23 s / 0.12 s, spread 2-3 ms) and each position read takes about
0.25 s, so below about 0.01 deg (36") they undershoot or hunt, and they turn tracking off. A dither is
instead a pair of **timed MoveAxis pulses** with tracking left on (`Scope.pulse_axis`): a slow rate
(`rate_deg_s`, default 0.01 deg/s = 36"/s) held for offset / rate plus the axis's lead (start minus stop
delay). Timing errors are a few ms, i.e. well under an arcsecond at that rate. The RA axis is never
reversed (like ST-4 guiding): a pulse against the tracking direction runs at most `ra_no_reverse` x the
sidereal rate, so it only slows the axis and gear backlash doesn't enter. Dec reverses; `dec_backlash_as`
is added on a direction change (0 until measured). The achieved move is checked afterwards from the
next frame's registration offset (`Response`).

Pattern: random inside a disc of radius dither_px around the **centred position** (not a random walk),
each new position at least half the radius away from the current one and from the last few, so every
dither moves the hot pixels by several superpixels. Drift (polar error, tracking rate) still carries the
field away; a re-centre with a plate solve every N dithers (or once the measured drift exceeds a limit)
brings it back.
"""
import math
from dataclasses import dataclass

import numpy as np

from . import astro


@dataclass
class DitherConfig:
    """`[dither]` in config.toml: how the mount makes the small moves (per scope, not per plan)."""
    rate_deg_s: float = 0.01        # pulse rate relative to the sky (each axis)
    ra_lead_s: float = 0.02         # RA: start delay - stop delay of MoveAxis, s (one S30 Pro: 0.28 - 0.26)
    dec_lead_s: float = 0.11        # Dec: 0.23 - 0.12
    ra_moveaxis: str = "adds"       # "adds": a MoveAxis rate on RA adds to sidereal tracking; "replaces"
    ra_no_reverse: float = 0.9      # against tracking: at most this x sidereal, so RA never reverses (0 = off)
    dec_backlash_as: float = 0.0    # added to a Dec pulse that reverses direction
    runaway_s: float = 60.0         # a pulse is refused unless its rate held this long stays inside the window
    max_pulse_s: float = 60.0

    @classmethod
    def from_dict(cls, d):
        d = dict(d or {})
        known = set(cls.__dataclass_fields__)
        c = cls(**{k: v for k, v in d.items() if k in known})
        if c.ra_moveaxis not in ("adds", "replaces"):
            raise ValueError(f"[dither] ra_moveaxis must be 'adds' or 'replaces', not {c.ra_moveaxis!r}")
        if c.rate_deg_s <= 0:
            raise ValueError("[dither] rate_deg_s must be > 0")
        return c


class Pattern:
    """Offsets (x, y) in pixels, uniform inside a disc of radius r, each at least sep * r from the current
    position and the last `memory` ones (the farthest of 64 candidates if none qualifies)."""

    def __init__(self, radius_px, seed=0, memory=6, sep=0.5):
        self.r, self.memory, self.sep = float(radius_px), int(memory), float(sep)
        self.rng = np.random.default_rng(seed)
        self.recent = []

    def next(self, cur=(0.0, 0.0)):
        best, best_d = None, -1.0
        others = self.recent[-self.memory:] + [tuple(cur)]
        for _ in range(64):
            rr, th = self.r * math.sqrt(self.rng.random()), 2 * math.pi * self.rng.random()
            p = (rr * math.cos(th), rr * math.sin(th))
            d = min(math.hypot(p[0] - q[0], p[1] - q[1]) for q in others)
            if d > best_d:
                best, best_d = p, d
            if d >= self.sep * self.r:
                break
        self.recent.append(best)
        return best


def axis_moves(d_east_as, d_north_as, dec_deg):
    """Sky offset (arcsec east, north) -> (RA-axis angle, Dec) change in degrees. +HA = west."""
    cosd = max(math.cos(math.radians(dec_deg)), 0.05)
    return -d_east_as / 3600.0 / cosd, d_north_as / 3600.0


def pulses(cfg: DitherConfig, du_deg, ddec_deg, tracking=True, last_dec_sign=0):
    """The MoveAxis pulses for an axis move: [(axis, rate, seconds, lead_s, base_rate)], axis 0 = RA.
    Rates are what MoveAxis gets; seconds the wanted motion time at the relative rate."""
    out = []
    sid = astro.SIDEREAL_DEG_S
    if du_deg:
        rel = cfg.rate_deg_s
        if du_deg < 0 and tracking and cfg.ra_no_reverse > 0:
            rel = min(rel, cfg.ra_no_reverse * sid)
        add = sid if (tracking and cfg.ra_moveaxis == "replaces") else 0.0
        rate = add + math.copysign(rel, du_deg)
        base = sid if (tracking and cfg.ra_moveaxis == "adds") else 0.0
        out.append((0, rate, abs(du_deg) / rel, cfg.ra_lead_s, base))
    if ddec_deg:
        s = 1 if ddec_deg > 0 else -1
        extra = cfg.dec_backlash_as / 3600.0 if (last_dec_sign and s != last_dec_sign) else 0.0
        out.append((1, s * cfg.rate_deg_s, (abs(ddec_deg) + extra) / cfg.rate_deg_s, cfg.dec_lead_s, 0.0))
    return out


def shift_matrix(cd):
    """2x2: pointing move (arcsec east, north) -> shift of the stars in the image (x, y), full-resolution
    px, from a solve's local CD matrix (px offset -> tangent plane xi east / eta north, degrees). The
    stars move opposite to the pointing. None if there's no usable CD."""
    if cd is None:
        return None
    M = np.asarray(cd, float)
    if M.shape != (2, 2) or abs(np.linalg.det(M)) < 1e-14:
        return None
    return -np.linalg.inv(M) / 3600.0


class Response:
    """What the dithers really did, from the registration offsets of the lights.

    Each frame adds (minutes, commanded offset (east, north) arcsec, measured image offset (x, y) px,
    segment); a segment ends at a re-centre. Between consecutive frames of a segment,
        measured change = A @ commanded change + v * dt
    with A (px per arcsec of commanded offset) and the drift v (px/min) fitted by least squares once
    there are 3 dithers in 2-D. With a plate solve's orientation A0 (shift_matrix), K = A0^-1 A is the
    achieved / commanded move: its diagonal is the east and north scale (1.0 = moves as commanded)."""

    def __init__(self, a0=None):
        self.a0 = None if a0 is None else np.asarray(a0, float)
        self.frames = []
        self.fit = None

    def add(self, t_min, cmd_as, meas_px, seg):
        self.frames.append((float(t_min), np.asarray(cmd_as, float), np.asarray(meas_px, float), int(seg)))
        self._refit()

    def _pairs(self):
        f = self.frames
        return [(f[i][0] - f[i - 1][0], f[i][1] - f[i - 1][1], f[i][2] - f[i - 1][2])
                for i in range(1, len(f)) if f[i][3] == f[i - 1][3]]

    def _refit(self):
        P = self._pairs()
        moves = np.array([dc for _, dc, _ in P if np.hypot(*dc) > 0]) if P else np.zeros((0, 2))
        if len(moves) < 3 or np.linalg.matrix_rank(moves, tol=1e-6) < 2:
            self.fit = None
            return
        X = np.array([[dc[0], dc[1], dt] for dt, dc, _ in P])     # unknowns per image axis: A row, v
        Y = np.array([dm for _, _, dm in P])
        sol, *_ = np.linalg.lstsq(X, Y, rcond=None)
        A, v = sol[:2].T, sol[2]
        res = Y - X @ sol
        self.fit = {"A": A, "v_px_min": v, "rms_px": float(np.sqrt((res ** 2).sum(1).mean())), "n": len(moves)}

    def matrix(self):
        """Best sky -> px matrix: the fit, else the plate solve's orientation, else None."""
        if self.fit is not None:
            return self.fit["A"]
        return self.a0

    def predicted(self, dcmd_as):
        A = self.matrix()
        return None if A is None else A @ np.asarray(dcmd_as, float)

    def drift_path(self):
        """Measured offsets with the dithers taken out, px: one point per frame (None without a matrix).
        Increments across a re-centre are left out, so the path is continuous."""
        A = self.matrix()
        if A is None or not self.frames:
            return None
        out, cur = [np.zeros(2)], np.zeros(2)
        f = self.frames
        for i in range(1, len(f)):
            if f[i][3] == f[i - 1][3]:
                cur = cur + (f[i][2] - f[i - 1][2]) - A @ (f[i][1] - f[i - 1][1])
            out.append(cur.copy())
        return out

    def segment_drift_px(self):
        """Drift (px) accumulated since the last re-centre, or None."""
        A = self.matrix()
        if A is None or not self.frames:
            return None
        seg = self.frames[-1][3]
        f = [x for x in self.frames if x[3] == seg]
        d = np.zeros(2)
        for i in range(1, len(f)):
            d += (f[i][2] - f[i - 1][2]) - A @ (f[i][1] - f[i - 1][1])
        return float(np.hypot(*d))

    def scale(self):
        """Achieved / commanded moves: {"east", "north", "cross"} from K = A0^-1 A, or None."""
        if self.fit is None or self.a0 is None:
            return None
        K = np.linalg.solve(self.a0, self.fit["A"])
        return {"east": round(float(K[0, 0]), 3), "north": round(float(K[1, 1]), 3),
                "cross": round(float(max(abs(K[0, 1]), abs(K[1, 0]))), 3)}

    def summary(self):
        s = {"frames": len(self.frames), "fitted": self.fit is not None,
             "oriented": self.a0 is not None}
        if self.fit is not None:
            s.update(rms_px=round(self.fit["rms_px"], 2), moves=self.fit["n"],
                     drift_px_min=[round(float(x), 2) for x in self.fit["v_px_min"]])
        sc = self.scale()
        if sc:
            s["scale"] = sc
        return s
