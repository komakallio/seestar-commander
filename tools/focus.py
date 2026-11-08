"""Star focus for the wide camera: sweep Focuser 1, measure median star HFR, fit, move to best.

Point the scope at open sky first (goto.py). Does not slew.
usage: focus.py [--exp 2] [--gain 200] [--center CURRENT] [--range 160] [--step 40] [--fine 10]
"""
import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl
from scope import Scope, star_hfr

ap = argparse.ArgumentParser()
ap.add_argument("--exp", type=float, default=2.0)
ap.add_argument("--gain", type=int, default=200)
ap.add_argument("--center", type=int)
ap.add_argument("--range", type=int, default=160)
ap.add_argument("--step", type=int, default=40)
ap.add_argument("--fine", type=int, default=10)
ap.add_argument("--save", action="store_true", help="save every focus frame as FITS")
a = ap.parse_args()

s = Scope()
out = os.path.join(pl.ROOT, "data", "focus", time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
os.makedirs(out, exist_ok=True)
start = s.f.Position
center = a.center if a.center is not None else start
print(f"focuser at {start}, sweeping around {center}")
results = []


def measure(p):
    pos = s.focus_to(p)
    data = s.expose(a.exp, a.gain)
    hfr, n = star_hfr(data)
    results.append({"pos": pos, "hfr": hfr, "n": n})
    print(f"  pos {pos:4d}  HFR {hfr:5.2f} px(superpixel)  stars {n}")
    if a.save:
        s.save_fits(os.path.join(out, f"f{pos:04d}.fits"), data, a.exp, a.gain, s.position(),
                    time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()))
    return hfr


def fit_best(pts):
    pts = [(p, h) for p, h in pts if np.isfinite(h)]
    if len(pts) < 3:
        return None
    p, h = np.array(pts).T
    i = int(np.argmin(h))
    sel = slice(max(0, i - 2), min(len(p), i + 3))
    if len(p[sel]) >= 3:
        A, B, C = np.polyfit(p[sel], h[sel], 2)
        if A > 0:
            return float(-B / (2 * A))
    return float(p[i])


try:
    # approach from below to keep backlash consistent
    coarse = list(range(max(0, center - a.range), min(1023, center + a.range) + 1, a.step))
    s.focus_to(max(0, coarse[0] - a.step))
    for p in coarse:
        measure(p)
    best = fit_best([(r["pos"], r["hfr"]) for r in results])
    if best is None:
        raise SystemExit("not enough stars; is the scope pointing at clear sky? focuser returned to start")
    print(f"coarse best ~{best:.0f}")
    fine = list(range(int(best) - 2 * a.step // 2, int(best) + 2 * a.step // 2 + 1, a.fine))
    s.focus_to(max(0, fine[0] - a.step))
    n0 = len(results)
    for p in fine:
        measure(p)
    best2 = fit_best([(r["pos"], r["hfr"]) for r in results[n0:]]) or best
    s.focus_to(max(0, int(round(best2)) - a.step))
    final = s.focus_to(int(round(best2)))
    hfr, n = star_hfr(s.expose(a.exp, a.gain))
    print(f"BEST focus {final}: HFR {hfr:.2f} ({n} stars)  [start was {start}]")
    json.dump({"start": start, "best": final, "hfr": hfr, "sweep": results,
               "exp": a.exp, "gain": a.gain}, open(os.path.join(out, "focus.json"), "w"), indent=1)
except BaseException:
    print(f"interrupted/failed; returning focuser to {start}")
    s.focus_to(start)
    raise
