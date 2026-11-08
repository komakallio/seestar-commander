"""Blind-solve acquired frames with the projections solver (solver.defocus.fi core).

Each frame -> debayered green 16-bit PNG (1080 x 1920) -> `node scripts/solve-blind.ts`
-> data/pano/solutions/frame_###.json. Results summary in data/pano/solve.json (kept
separate from state.json, which capture.py owns).

usage: PROJECTIONS_DIR=<clone> solve_frames.py [--watch] [--redo]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

import numpy as np
from astropy.io import fits
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl
from scope import green

PROJ = os.environ.get("PROJECTIONS_DIR")
GREEN = os.path.join(pl.PANO_DIR, "green")
SOLS = os.path.join(pl.PANO_DIR, "solutions")
SUMMARY = os.path.join(pl.PANO_DIR, "solve.json")


def load_summary():
    try:
        return json.load(open(SUMMARY))
    except Exception:
        return {}


def save_summary(s):
    tmp = SUMMARY + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, SUMMARY)


def solve_one(f):
    os.makedirs(GREEN, exist_ok=True)
    os.makedirs(SOLS, exist_ok=True)
    fid = f"frame_{f['id']:03d}"
    png = os.path.join(GREEN, fid + ".png")
    if not os.path.exists(png):
        g = green(fits.getdata(os.path.join(pl.PANO_DIR, f["file"])))
        Image.fromarray(np.clip(g, 0, 65535).astype(np.uint16)).save(png)
    sol = os.path.join(SOLS, fid + ".json")
    t0 = time.time()
    p = subprocess.run(["node", "scripts/solve-blind.ts", png, "--solution", sol],
                       cwd=PROJ, capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    solved = "\n  Solved." in out or re.search(r"^\s*Solved\.", out, re.M) is not None
    m = re.search(r"Winner: (\d+) stars, RMS ([\d.]+) px", out)
    det = re.search(r"(\d+) detections", out)
    return {"id": f["id"], "solved": bool(solved), "stars": int(m.group(1)) if m else 0,
            "rms": float(m.group(2)) if m else None, "detections": int(det.group(1)) if det else 0,
            "solution": os.path.relpath(sol, pl.PANO_DIR) if os.path.exists(sol) else None,
            "seconds": round(time.time() - t0, 1),
            "tail": out.strip().splitlines()[-3:]}


def run(redo=False):
    st = pl.load_state()
    summ = load_summary()
    n = 0
    for f in st["frames"]:
        if f["status"] != "acquired" or not f.get("file"):
            continue
        if str(f["id"]) in summ and not redo:
            continue
        r = solve_one(f)
        summ[str(f["id"])] = r
        save_summary(summ)
        n += 1
        print(f"#{f['id']:3d} {'SOLVED' if r['solved'] else 'failed'}  stars {r['stars']:4d}  "
              f"rms {r['rms']}  det {r['detections']}  {r['seconds']}s", flush=True)
    return n


if __name__ == "__main__":
    if not PROJ:
        sys.exit("set PROJECTIONS_DIR to a projections clone with node_modules")
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--redo", action="store_true")
    a = ap.parse_args()
    run(a.redo)
    while a.watch:
        time.sleep(20)
        try:
            run()
        except Exception as e:
            print("solve error:", e, flush=True)
