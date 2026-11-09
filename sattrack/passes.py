"""Find satellite passes and decide which part of each one the mount can follow.

A pass is sampled at a fixed step (default 0.1 s) between rise and set. A sample is trackable when it
is above the local horizon (and min_alt) and passes every guard rule (cable window, Dec range, rate
limit, runaway). The RA-axis branch (HA + k*360) is chosen to give the longest trackable stretch.
Verdict: ok (the whole visible part), trim (a shorter stretch of at least min_track_s), reject.
"""
import math
from dataclasses import dataclass, field

import numpy as np

from . import geometry as geo

GEO_SPAN_S = 3600.0   # longest stretch planned per pass (GEO, GNSS)


@dataclass
class Pass:
    norad: int
    name: str
    rise: float                     # unix; altitude 0 events
    set: float
    verdict: str = "reject"
    reasons: dict = field(default_factory=dict)     # rule -> seconds violated while visible
    summary: dict = field(default_factory=dict)
    samples: dict | None = None     # trackable stretch at the sample step (t relative to its start)


def _runs(mask):
    """(start, stop) index pairs of True runs."""
    m = np.concatenate(([False], np.asarray(mask, bool), [False]))
    d = np.diff(m.astype(int))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def _longest(mask):
    r = _runs(mask)
    return max(r, key=lambda p: p[1] - p[0]) if r else None


def horizon_alt(horizon, az):
    if horizon is None:
        return np.zeros_like(np.asarray(az, float))
    return np.interp(np.asarray(az, float) % 360.0, horizon.az, horizon.alt, period=360.0)


def find_passes(sat, site, t0, t1, max_span_s=GEO_SPAN_S):
    """[(rise, set)] in unix seconds over [t0, t1], altitude 0, each clipped to the span and to
    max_span_s from its start (slow objects such as GEO and GNSS stay up for hours or always)."""
    t, ev = sat.find_events(site.topos(), geo.times(t0), geo.times(t1), altitude_degrees=0.0)
    unix = [(ti.utc_datetime().timestamp(), int(e)) for ti, e in zip(t, ev)]
    up0 = float(geo.track(sat, site, [t0])["alt"][0]) > 0
    out, start = [], (t0 if up0 else None)
    for tu, e in unix:
        if e == 0:
            start = tu
        elif e == 2 and start is not None:
            out.append((start, tu))
            start = None
    if start is not None:
        out.append((start, t1))
    return [(a, min(b, a + max_span_s)) for a, b in out]


def analyse(sat, site, rise, set_, limits, horizon=None, min_alt=10.0, step=0.1, u_now=None,
            min_track_s=60.0, norad=0, name="", keep_samples=True):
    n = max(2, int(math.floor((set_ - rise) / step)) + 1)
    unix = rise + np.arange(n) * step
    tr = geo.track(sat, site, unix)
    vis = tr["alt"] > np.maximum(min_alt, horizon_alt(horizon, tr["az"]))
    p = Pass(norad=int(norad), name=name, rise=float(rise), set=float(set_))
    if not vis.any():
        p.reasons = {"below_horizon": round(n * step, 1)}
        p.summary = {"peak_alt": round(float(tr["alt"].max()), 1)}
        return p
    u0 = geo.unwrap_ha(tr["ha"])
    dec = tr["dec"]
    ru, au = geo.rates(u0, step)
    rd, ad = geo.rates(dec, step)
    vis_run = _longest(vis)

    best = None
    for k in range(-2, 3):
        u = u0 + 360.0 * k
        bad = limits.all_bad(u, dec, ru, rd)
        anybad = np.zeros(n, bool)
        for v in bad.values():
            anybad |= v
        run = _longest(vis & ~anybad)
        length = (run[1] - run[0]) if run else 0
        if run:
            seg = slice(*run)
            margin = float(min((u[seg] - limits.u_min).min(), (limits.u_max - u[seg]).min()))
            move = abs(u[run[0]] - u_now) if u_now is not None else 0.0
        else:
            margin, move = -1e9, 0.0
        key = (length, -move, margin)
        if best is None or key > best[0]:
            best = (key, k, u, bad, run)
    _, k, u, bad, run = best
    p.reasons = {r: round(float((v & vis).sum()) * step, 1) for r, v in bad.items() if (v & vis).any()}

    vis_len = (vis_run[1] - vis_run[0]) * step
    trk_len = (run[1] - run[0]) * step if run else 0.0
    if run and trk_len >= vis_len - 2 * step:
        p.verdict = "ok"
    elif trk_len >= min_track_s:
        p.verdict = "trim"
    else:
        p.verdict = "reject"

    vs = slice(*vis_run)
    s = {"peak_alt": round(float(tr["alt"].max()), 1),
         "visible_start": float(unix[vis_run[0]]), "visible_s": round(vis_len, 1),
         "visible_peak_rate_ra": round(float(np.abs(ru[vs]).max()), 3),
         "visible_peak_rate_dec": round(float(np.abs(rd[vs]).max()), 3),
         "visible_max_dec": round(float(dec[vs].max()), 1),
         "branch": k, "track_s": round(trk_len, 1)}
    if run:
        seg = slice(*run)
        s.update({"track_start": float(unix[run[0]]), "track_end": float(unix[run[1] - 1]),
                  "u_start": round(float(u[run[0]]), 2), "u_min": round(float(u[seg].min()), 2),
                  "u_max": round(float(u[seg].max()), 2),
                  "dec_min": round(float(dec[seg].min()), 2), "dec_max": round(float(dec[seg].max()), 2),
                  "peak_rate_ra": round(float(np.abs(ru[seg]).max()), 3),
                  "peak_rate_dec": round(float(np.abs(rd[seg]).max()), 3),
                  "peak_accel_ra": round(float(np.abs(au[seg]).max()), 4),
                  "peak_accel_dec": round(float(np.abs(ad[seg]).max()), 4),
                  "cable_margin": round(float(min((u[seg] - limits.u_min).min(),
                                                  (limits.u_max - u[seg]).min())), 1),
                  "track_peak_alt": round(float(tr["alt"][seg].max()), 1)})
        if keep_samples:
            p.samples = {"step": step, "t0": float(unix[run[0]]),
                         "u": np.round(u[seg], 5).tolist(), "dec": np.round(dec[seg], 5).tolist(),
                         "rate_u": np.round(ru[seg], 6).tolist(), "rate_dec": np.round(rd[seg], 6).tolist(),
                         "alt": np.round(tr["alt"][seg], 3).tolist(), "az": np.round(tr["az"][seg], 3).tolist()}
    p.summary = s
    return p
