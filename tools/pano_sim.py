"""Offline coverage and timing simulation for a panorama plan (no SeeStar contact).

Geometry matches panolib.footprint(): gnomonic frame, long sensor side along the
Dec axis (CAMERA_LONG_AXIS), optional extra roll about the boresight.

Coverage is checked numerically on a near-uniform (Fibonacci) sky grid above alt_min.
For every sky point we take the largest "edge margin" over all frames: the angular
distance from the point to the nearest edge of a frame that contains it. A seam
between two frames that overlap by w degrees has worst-case margin w/2, so
2 * min(margin) is the minimum overlap of the plan (it also counts how far the
frames reach past alt_min at the bottom).

Timing: frames are taken in plan order, starting from home. RA moves first, then
Dec (sequential, closed-loop MoveAxis). See TimeModel.

usage: pano_sim.py STATE.json [--alt-min -10] [--min-overlap 4]
"""
import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl

D2R = math.pi / 180
BANDS = ((-10, 0), (0, 15), (15, 30), (30, 50), (50, 70), (70, 90.01))


# ---- geometry -------------------------------------------------------------
def hadec_vec(ha, dec):
    h, d = np.radians(ha), np.radians(dec)
    return np.stack([np.cos(d) * np.cos(h), -np.cos(d) * np.sin(h), np.sin(d)], axis=-1)


def altaz_to_hadec(alt, az, lat=pl.LAT):
    a, A, p = np.radians(alt), np.radians(az), lat * D2R
    sd = np.sin(p) * np.sin(a) + np.cos(p) * np.cos(a) * np.cos(A)
    dec = np.arcsin(np.clip(sd, -1, 1))
    ha = np.arctan2(-np.sin(A) * np.cos(a), np.cos(p) * np.sin(a) - np.sin(p) * np.cos(a) * np.cos(A))
    return np.degrees(ha), np.degrees(dec)


def vec_altaz(v, lat=pl.LAT):
    """HA/Dec unit vectors -> (alt, az) degrees."""
    ha = np.degrees(np.arctan2(-v[..., 1], v[..., 0]))
    dec = np.degrees(np.arcsin(np.clip(v[..., 2], -1, 1)))
    h, d, p = np.radians(ha), np.radians(dec), lat * D2R
    sa = np.sin(p) * np.sin(d) + np.cos(p) * np.cos(d) * np.cos(h)
    az = np.arctan2(-np.cos(d) * np.sin(h), np.sin(d) * np.cos(p) - np.cos(d) * np.cos(h) * np.sin(p))
    return np.degrees(np.arcsin(np.clip(sa, -1, 1))), np.degrees(az) % 360


@dataclass
class Camera:
    fov_short: float = pl.FOV_SHORT
    fov_long: float = pl.FOV_LONG
    long_axis: str = pl.CAMERA_LONG_AXIS
    roll: float = 0.0  # extra rotation about the boresight, degrees

    def half_tans(self):
        hx, hy = math.tan(self.fov_short / 2 * D2R), math.tan(self.fov_long / 2 * D2R)
        return (hy, hx) if self.long_axis == "ra" else (hx, hy)


def frame_axes(ha, dec, cam):
    """Boresight c, 'x' (RA-ish) and 'y' (Dec-ish) unit vectors, arrays (..., 3)."""
    ha, dec = np.asarray(ha, float), np.asarray(dec, float)
    h, d = np.radians(ha), np.radians(dec)
    c = hadec_vec(ha, dec)
    n = np.stack([-np.sin(d) * np.cos(h), np.sin(d) * np.sin(h), np.cos(d)], axis=-1)
    e = np.cross(n, c)
    if cam.roll:
        r = cam.roll * D2R
        e, n = math.cos(r) * e + math.sin(r) * n, -math.sin(r) * e + math.cos(r) * n
    return c, e, n


def margins(P, ha, dec, cam):
    """Edge margin (deg) of points P (M,3) in each frame -> (F, M); <0 outside."""
    hx, hy = cam.half_tans()
    c, e, n = frame_axes(np.atleast_1d(ha), np.atleast_1d(dec), cam)
    pc, px, py = c @ P.T, e @ P.T, n @ P.T
    mx = (hx * pc - np.abs(px)) / math.sqrt(1 + hx * hx)
    my = (hy * pc - np.abs(py)) / math.sqrt(1 + hy * hy)
    m = np.degrees(np.arcsin(np.clip(np.minimum(mx, my), -1, 1)))
    m[pc <= 0] = -90.0
    return m


def outline(ha, dec, cam, n_per_edge=12):
    """Frame outline as [[alt, az], ...] (same format as panolib.footprint)."""
    hx, hy = cam.half_tans()
    c, e, n = frame_axes(ha, dec, cam)
    corners = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
    pts = []
    for i in range(4):
        (x0, y0), (x1, y1) = corners[i], corners[(i + 1) % 4]
        for k in range(n_per_edge):
            t = k / n_per_edge
            v = c + (x0 + (x1 - x0) * t) * e + (y0 + (y1 - y0) * t) * n
            pts.append(v / np.linalg.norm(v))
    alt, az = vec_altaz(np.array(pts))
    return [[round(float(a), 2), round(float(z), 2)] for a, z in zip(alt, az)]


def sky_grid(alt_min=-10.0, spacing=1.0, lat=pl.LAT):
    """Fibonacci points above alt_min; returns (P_hadec (M,3), alt (M,))."""
    n = int(41253 / spacing ** 2)
    i = np.arange(n) + 0.5
    z = 1 - 2 * i / n  # sin(alt)
    az = np.degrees(np.pi * (1 + 5 ** 0.5) * i) % 360
    alt = np.degrees(np.arcsin(z))
    keep = alt >= alt_min
    alt, az = alt[keep], az[keep]
    ha, dec = altaz_to_hadec(alt, az, lat)
    return hadec_vec(ha, dec), alt


def best_margin(P, has, decs, cam, chunk=64):
    out = np.full(len(P), -90.0)
    for i in range(0, len(has), chunk):
        out = np.maximum(out, margins(P, has[i:i + chunk], decs[i:i + chunk], cam).max(axis=0))
    return out


def coverage(has, decs, cam=Camera(), alt_min=-10.0, min_overlap=4.0, spacing=0.5):
    P, alt = sky_grid(alt_min, spacing)
    m = best_margin(P, np.asarray(has), np.asarray(decs), cam)
    res = {"min_overlap": round(float(2 * m.min()), 2), "bands": []}
    for lo, hi in BANDS:
        sel = (alt >= max(lo, alt_min)) & (alt < hi)
        if not sel.any():
            continue
        res["bands"].append({
            "band": f"{lo:g}..{min(hi, 90):g}",
            "covered": round(100 * float((m[sel] >= 0).mean()), 2),
            "with_overlap": round(100 * float((m[sel] >= min_overlap / 2).mean()), 2),
            "min_overlap": round(float(2 * m[sel].min()), 2),
        })
    res["covered"] = round(100 * float((m >= 0).mean()), 3)
    res["with_overlap"] = round(100 * float((m >= min_overlap / 2).mean()), 3)
    return res


# ---- timing ---------------------------------------------------------------
@dataclass
class TimeModel:
    """Per frame: [RA move] + [Dec move] + settle + exposure + readout + download.

    Each axis that moves costs axis_overhead + |delta| * sec_per_deg (closed-loop
    MoveAxis: fast leg, 1 deg/s and 0.3 deg/s approach legs, Alpaca round trips).
    'nominal' is the brief's model (one 3.5 s overhead per move, 5 deg/s).
    'measured' is fitted to data/pano/capture.log of 2026-11-08 (see calibrate()).
    """
    name: str = "measured"
    axis_overhead: float = 9.5
    sec_per_deg: float = 0.28
    move_overhead: float = 0.0   # once per frame if anything moves
    settle: float = 1.5
    readout: float = 2.0         # exposure start/ImageReady polling, FITS write, position reads
    download: float = 10.0
    tol: float = 0.3

    def move(self, dha, ddec):
        t = 0.0
        for d in (dha, ddec):
            if abs(d) > self.tol:
                t += self.axis_overhead + abs(d) * self.sec_per_deg
        if t:
            t += self.move_overhead
        return t

    def frame(self, exp):
        return self.settle + exp + self.readout + self.download


NOMINAL = TimeModel("nominal", axis_overhead=0.0, sec_per_deg=0.2, move_overhead=3.5, settle=0.0, readout=0.0)
MEASURED = TimeModel()


def calibrate(log_path, exp=20.0, base=None):
    """Fit axis_overhead and sec_per_deg to a capture.log (slew start -> acquired)."""
    base = base or TimeModel()
    sl, ac = {}, {}
    for line in open(log_path):
        m = re.match(r"(\d+):(\d+):(\d+)Z #(\d+) row \d+: slew to .*\(HA (\S+) Dec (\S+)\)", line)
        if m:
            sl[int(m[4])] = (int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]), float(m[5]), float(m[6]))
        m = re.match(r"(\d+):(\d+):(\d+)Z #(\d+) acquired", line)
        if m:
            ac[int(m[4])] = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
    A, y = [], []
    for i in sorted(ac):
        if i - 1 in sl and i in sl:
            dh, dd = abs(sl[i][1] - sl[i - 1][1]), abs(sl[i][2] - sl[i - 1][2])
            n_ax = (dh > base.tol) + (dd > base.tol)
            A.append([n_ax, dh + dd])
            y.append(ac[i] - sl[i][0] - base.frame(exp))
    (o, s), *_ = np.linalg.lstsq(np.array(A, float), np.array(y), rcond=None)
    first, last = min(sl), max(ac)
    return {"axis_overhead": float(o), "sec_per_deg": float(s), "n": len(y),
            "resid_std": float(np.std(np.array(y) - np.array(A) @ [o, s])),
            "wall_s": ac[last] - sl[first][0], "frames": len(ac)}


def timeline(seq, exp, tm, home=(-90.0, -90.0), return_home=False):
    """seq: list of (ha_unwrapped, dec). Returns totals and move stats."""
    pos = home
    total, moves = 0.0, []
    path = list(seq) + ([home] if return_home else [])
    for k, (u, d) in enumerate(path):
        dha, ddec = u - pos[0], d - pos[1]
        total += tm.move(dha, ddec)
        if k < len(seq):
            total += tm.frame(exp)
        moves.append(dha)
        pos = (u, d)
    us = [u for u, _ in seq]
    return {"total_s": total, "largest_ra_move": max(abs(m) for m in moves),
            "largest_ra_move_between_frames": max([abs(m) for m in moves[1:len(seq)]] or [0]),
            "ra_total": sum(abs(m) for m in moves),
            "u_min": min(us), "u_max": max(us)}


def plan_seq(frames):
    return [(f["planned"].get("ha_u", f["planned"]["ha"]), f["planned"]["dec"]) for f in frames]


def summarise(name, frames, cam=Camera(), alt_min=-10.0, min_overlap=4.0, home=(-90.0, -90.0),
              day_exp=0.01, night_exp=20.0, spacing=0.5):
    seq = plan_seq(frames)
    cov = coverage([s[0] for s in seq], [s[1] for s in seq], cam, alt_min, min_overlap, spacing)
    out = {"name": name, "frames": len(frames), "coverage": cov}
    for tm in (MEASURED, NOMINAL):
        for label, exp in (("night", night_exp), ("day", day_exp)):
            out[f"{label}_{tm.name}"] = timeline(seq, exp, tm, home)
    t = out["night_measured"]
    out["ra_extremes_vs_home"] = (round(t["u_min"] - home[0], 1), round(t["u_max"] - home[0], 1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("state")
    ap.add_argument("--alt-min", type=float, default=-10.0)
    ap.add_argument("--min-overlap", type=float, default=4.0)
    ap.add_argument("--home-ha", type=float, default=-90.0)
    ap.add_argument("--calibrate", help="capture.log to fit the measured time model on (read-only)")
    a = ap.parse_args()
    if a.calibrate:
        print(json.dumps(calibrate(a.calibrate), indent=1))
    st = json.load(open(a.state))
    s = summarise(os.path.basename(a.state), st["frames"], alt_min=a.alt_min, min_overlap=a.min_overlap,
                  home=(a.home_ha, -90.0))
    print(json.dumps(s, indent=1))


if __name__ == "__main__":
    main()
