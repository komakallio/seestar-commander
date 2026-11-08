"""Fit a mount model from blind-solved frames, then guided-solve every frame.

Model: camera<-ENU  R = C . B(H, Dec) . P . Q
  Q        ENU -> ideal hour-angle/Dec frame (site latitude)
  P        small rotation: polar misalignment + HA zero point
  B(H,Dec) = Ry(Dec) Rz(H): the mount's two axes at the reported HA/Dec
  C        camera mounting on the Dec axis (absorbs Dec zero point and roll)

Steps: orient blind solutions (guided_solve.ts orient) -> fit C, P -> predict a
start orientation for every acquired frame -> guided_solve.ts solve (lens fixed).

usage: PROJECTIONS_DIR=... mount_model.py [--lens-frame ID] [--only-unsolved]
"""
import argparse
import json
import math
import os
import subprocess
import sys

import numpy as np
from astropy.io import fits
from astropy.time import Time
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation as Rot

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl

PROJ = os.environ["PROJECTIONS_DIR"]
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(pl.PANO_DIR, "guided")
D2R = math.pi / 180


def Ry(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def Rz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def Qmat():
    cols = []
    for alt, az in ((0, 90), (0, 0), (90, 0)):  # E, N, U
        h, d = pl.altaz_to_hadec(alt, az)
        cols.append(pl._vec(h, d))
    return np.array(cols).T


QM = Qmat()


def B(ha, dec):
    return Ry(dec * D2R) @ Rz(ha * D2R)


def predict(params, ha, dec):
    C = Rot.from_rotvec(params[:3]).as_matrix()
    P = Rot.from_rotvec(params[3:6]).as_matrix()
    return C @ B(ha, dec) @ P @ QM


def angle_deg(A, Bm):
    return math.degrees(Rot.from_matrix(A.T @ Bm).magnitude())


def mid_ms(f):
    h = fits.getheader(os.path.join(pl.PANO_DIR, f["file"]))
    return (Time(h["DATE-OBS"], scale="utc").unix + h["EXPTIME"] / 2) * 1000


def node(mode, job):
    path = os.path.join(OUT, f"job_{mode}.json")
    os.makedirs(OUT, exist_ok=True)
    json.dump(job, open(path, "w"))
    p = subprocess.run(["node", os.path.join(HERE, "guided_solve.ts"), mode, path],
                       capture_output=True, text=True, env={**os.environ})
    if p.returncode:
        sys.exit(p.stderr[-3000:])
    return p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lens-frame", type=int, help="blind-solved frame whose lens to use")
    ap.add_argument("--max-rms", type=float, default=0.6)
    ap.add_argument("--no-guided", action="store_true", help="only fit the model and write orientations")
    a = ap.parse_args()

    st = pl.load_state()
    fr = {f["id"]: f for f in st["frames"]}
    summ = json.load(open(os.path.join(pl.PANO_DIR, "solve.json")))
    good = [r for r in summ.values() if r["solved"] and r["rms"] is not None and r["rms"] <= a.max_rms]
    if len(good) < 3:
        sys.exit("need >= 3 good blind solves")
    site = {"lat": pl.LAT, "lon": pl.LON, "height": 40}
    job = {"site": site, "frames": [
        {"id": r["id"], "timeMs": mid_ms(fr[r["id"]]),
         "solution": os.path.join(pl.PANO_DIR, r["solution"])} for r in good]}
    obs = json.loads(node("orient", job))

    def resid(params):
        out = []
        for o in obs:
            f = fr[o["id"]]
            Rp = predict(params, f["actual"]["ha"], f["actual"]["dec"])
            out.append((np.array(o["R"]).reshape(3, 3) - Rp).ravel())
        return np.concatenate(out)

    f0 = fr[obs[0]["id"]]
    C0 = np.array(obs[0]["R"]).reshape(3, 3) @ (B(f0["actual"]["ha"], f0["actual"]["dec"]) @ QM).T
    x0 = np.concatenate([Rot.from_matrix(C0).as_rotvec(), np.zeros(3)])
    fit = least_squares(resid, x0)
    p = fit.x
    print(f"fit on {len(obs)} blind solves; polar/HA-zero rotation {math.degrees(np.linalg.norm(p[3:])):.2f} deg")
    for o in obs:
        f = fr[o["id"]]
        print(f"  #{o['id']:3d} residual {angle_deg(np.array(o['R']).reshape(3, 3), predict(p, f['actual']['ha'], f['actual']['dec'])):.3f} deg")
    json.dump({"params": p.tolist(), "frames_used": [o["id"] for o in obs]},
              open(os.path.join(OUT, "mount_model.json"), "w"), indent=1)

    lens_id = a.lens_frame or max(good, key=lambda r: r["stars"])["id"]
    # per-frame orientation for stitching: blind solve if good, else model prediction
    solved = {o["id"]: np.array(o["R"]).reshape(3, 3) for o in obs}
    orient = {"lens_solution": os.path.join(pl.PANO_DIR, summ[str(lens_id)]["solution"]), "frames": []}
    for f in st["frames"]:
        if f["status"] != "acquired":
            continue
        pred = predict(p, f["actual"]["ha"], f["actual"]["dec"])
        R, src = (solved[f["id"]], "solved") if f["id"] in solved else (pred, "model")
        orient["frames"].append({"id": f["id"], "R": R.ravel().tolist(), "source": src,
                                 "model_vs_solved_deg": angle_deg(R, pred) if src == "solved" else None})
    json.dump(orient, open(os.path.join(OUT, "orientations.json"), "w"))
    print(f"orientations: {sum(x['source'] == 'solved' for x in orient['frames'])} solved, "
          f"{sum(x['source'] == 'model' for x in orient['frames'])} from model; lens from #{lens_id}")
    if a.no_guided:
        return
    frames = []
    for f in st["frames"]:
        if f["status"] != "acquired":
            continue
        frames.append({"id": f["id"], "timeMs": mid_ms(f),
                       "png": os.path.join(pl.PANO_DIR, "green", f"frame_{f['id']:03d}.png"),
                       "start": predict(p, f["actual"]["ha"], f["actual"]["dec"]).ravel().tolist()})
    missing = [f for f in frames if not os.path.exists(f["png"])]
    if missing:
        print(f"note: {len(missing)} frames have no green PNG yet; skipped")
        frames = [f for f in frames if os.path.exists(f["png"])]
    job = {"site": site, "lens_solution": os.path.join(pl.PANO_DIR, summ[str(lens_id)]["solution"]),
           "frames": frames, "out_dir": os.path.join(OUT, "frames"), "grid_step": 40}
    print(f"guided solve of {len(frames)} frames, lens from #{lens_id}")
    print(node("solve", job))


if __name__ == "__main__":
    main()
