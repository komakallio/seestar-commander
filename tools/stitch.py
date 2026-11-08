"""Stitch all frames into an equirectangular panorama (alt ALT_MIN..90, full azimuth).

Uses data/pano/guided/orientations.json (from mount_model.py --no-guided): per-frame
camera<-ENU rotation and the shared lens (projections radial model) of the best solve.
Output layout follows Stellarium's spherical landscape: left edge = azimuth 90 (east),
azimuth increasing to the right, top = alt 90, bottom = ALT_MIN.

Writes data/pano/out/pano_lin.npy (float32 linear, NaN where uncovered) and
pano_preview.png (stretched, 8-bit) plus coverage count.

usage: stitch.py [--width 4096] [--alt-min -10]
"""
import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl

OUT = os.path.join(pl.PANO_DIR, "out")


def project(lens, R, enu):
    """ENU directions (N,3) -> pixel x, y (N,), NaN outside the field."""
    v = enu @ R.T
    x = -v[:, 0] if lens["mirror"] else v[:, 0]
    y = v[:, 1]
    rho = np.hypot(x, y)
    theta = np.arctan2(rho, v[:, 2])
    k = lens["k"]
    t2 = theta * theta
    s = np.zeros_like(theta)
    for c in reversed(k):
        s = s * t2 + c
    r = s * theta
    with np.errstate(invalid="ignore", divide="ignore"):
        px = lens["x0"] + r * x / rho
        py = lens["y0"] + lens["a"] * r * y / rho
    bad = (theta > math.radians(40)) | (v[:, 2] <= 0)
    px[bad] = np.nan
    py[bad] = np.nan
    return px, py


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=4096)
    ap.add_argument("--alt-min", type=float, default=-10.0)
    ap.add_argument("--edge", type=float, default=20, help="px to ignore at frame borders")
    a = ap.parse_args()

    orient = json.load(open(os.path.join(pl.PANO_DIR, "guided", "orientations.json")))
    lens = json.load(open(orient["lens_solution"]))["model"]["lens"]
    W = a.width
    H = int(round(W * (90 - a.alt_min) / 360))
    az = (90 + (np.arange(W) + 0.5) / W * 360) % 360
    alt = 90 - (np.arange(H) + 0.5) / H * (90 - a.alt_min)
    AZ, ALT = np.meshgrid(np.radians(az), np.radians(alt))
    enu = np.stack([np.cos(ALT) * np.sin(AZ), np.cos(ALT) * np.cos(AZ), np.sin(ALT)], -1).reshape(-1, 3)

    fm = json.load(open(os.path.join(pl.PANO_DIR, "flat_model.json")))
    acc = np.zeros(W * H, np.float64)
    wsum = np.zeros(W * H, np.float64)
    count = np.zeros(W * H, np.uint8)
    for fo in orient["frames"]:
        png = os.path.join(pl.PANO_DIR, "green", f"frame_{fo['id']:03d}.png")
        img = np.array(Image.open(png)).astype(np.float32)
        h, w = img.shape
        # bias (black level) and radial vignetting flat
        img -= fm["bias"]
        yy, xx = np.mgrid[0:h, 0:w]
        r2 = ((xx + 0.5 - fm["x0"]) ** 2 + (yy + 0.5 - fm["y0"]) ** 2) / fm["rnorm"] ** 2
        flat = sum(c * r2 ** i for i, c in enumerate(fm["coef"]))
        img /= np.clip(flat, 0.2, None)
        R = np.array(fo["R"]).reshape(3, 3)
        px, py = project(lens, R, enu)
        m = (px >= a.edge) & (px < w - 1 - a.edge) & (py >= a.edge) & (py < h - 1 - a.edge)
        ix, iy = px[m], py[m]
        x0, y0 = np.floor(ix).astype(int), np.floor(iy).astype(int)
        fx, fy = ix - x0, iy - y0
        val = (img[y0, x0] * (1 - fx) * (1 - fy) + img[y0, x0 + 1] * fx * (1 - fy)
               + img[y0 + 1, x0] * (1 - fx) * fy + img[y0 + 1, x0 + 1] * fx * fy)
        # feather: distance to the nearest frame edge
        wgt = np.minimum.reduce([ix, w - 1 - ix, iy, h - 1 - iy]) + 1.0
        acc[m] += val * wgt
        wsum[m] += wgt
        count[m] += 1
        print(f"#{fo['id']:3d} {fo['source']:6s} {m.sum():8d} px", flush=True)

    pano = np.full(W * H, np.nan, np.float32)
    has = wsum > 0
    pano[has] = acc[has] / wsum[has]
    pano = pano.reshape(H, W)
    os.makedirs(OUT, exist_ok=True)
    np.save(os.path.join(OUT, "pano_lin.npy"), pano)
    np.save(os.path.join(OUT, "coverage.npy"), count.reshape(H, W))
    json.dump({"width": W, "height": H, "alt_min": a.alt_min, "alt_max": 90, "left_edge_az": 90,
               "az_direction": "increasing to the right"}, open(os.path.join(OUT, "pano_meta.json"), "w"))
    v = pano[np.isfinite(pano)]
    lo, hi = np.percentile(v, 0.5), np.percentile(v, 99.8)
    s = np.clip((np.nan_to_num(pano, nan=lo) - lo) / (hi - lo), 0, 1)
    s = np.arcsinh(s * 8) / np.arcsinh(8)
    Image.fromarray((s * 255).astype(np.uint8)).save(os.path.join(OUT, "pano_preview.png"))
    print(f"{W}x{H}, covered {has.mean() * 100:.1f}%")


if __name__ == "__main__":
    main()
