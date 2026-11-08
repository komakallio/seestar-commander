"""Reproject acquired frames onto the monitor's allsky projection -> data/pano/allsky.png.

Projection matches monitor.html: zenith at centre, radius R=500 at ALT_MIN=-20, north up,
east left; the PNG covers the SVG viewBox -560..560. Frames are binned to ~270x480,
stretched per frame, and feather-blended. Camera orientation assumption (until plate
solving refines it): image row 0 = +Dec side, column 0 = +RA-direction (east) side.

usage: render_allsky.py [--size 900] [--watch]
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np
from astropy.io import fits
from PIL import Image

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl

R, ALT_MIN, VB = 500.0, -20.0, 560.0
SMALL = os.path.join(pl.PANO_DIR, "small")
OUT = os.path.join(pl.PANO_DIR, "allsky.png")


def small_frame(f):
    """Binned green channel (H, W) = (480, 270), stretched to 0..1; cached as .npy."""
    os.makedirs(SMALL, exist_ok=True)
    cache = os.path.join(SMALL, f"frame_{f['id']:03d}.npy")
    if os.path.exists(cache):
        return np.load(cache)
    d = fits.getdata(os.path.join(pl.PANO_DIR, f["file"])).astype(np.float32)
    g = (d[0::2, 0::2] + d[1::2, 1::2]) / 2          # 1920 x 1080
    g = g.reshape(480, 4, 270, 4).mean(axis=(1, 3))   # 480 x 270
    lo, hi = np.percentile(g, 0.5), np.percentile(g, 99.7)
    s = np.clip((g - lo) / max(hi - lo, 1), 0, 1)
    s = np.arcsinh(s * 6) / np.arcsinh(6)
    np.save(cache, s.astype(np.float32))
    return s


def sky_grid(size):
    u = np.linspace(-VB, VB, size)
    X, Y = np.meshgrid(u, u)
    r = np.hypot(X, Y)
    alt = 90 - r / R * (90 - ALT_MIN)
    az = np.degrees(np.arctan2(-X, -Y)) % 360           # E left, N up
    valid = alt >= ALT_MIN
    a, A, p = np.radians(alt), np.radians(az), math.radians(pl.LAT)
    sd = np.sin(p) * np.sin(a) + np.cos(p) * np.cos(a) * np.cos(A)
    dec = np.arcsin(np.clip(sd, -1, 1))
    ha = np.arctan2(-np.sin(A) * np.cos(a), np.cos(p) * np.sin(a) - np.sin(p) * np.cos(a) * np.cos(A))
    v = np.stack([np.cos(dec) * np.cos(ha), -np.cos(dec) * np.sin(ha), np.sin(dec)], axis=-1)
    return v, valid


def render(size=900):
    st = pl.load_state()
    frames = [f for f in st["frames"] if f["status"] == "acquired" and f.get("file")]
    v, valid = sky_grid(size)
    acc = np.zeros((size, size), np.float32)
    wsum = np.zeros((size, size), np.float32)
    hx = math.tan(math.radians(pl.FOV_SHORT / 2))
    hy = math.tan(math.radians(pl.FOV_LONG / 2))
    for f in frames:
        try:
            img = small_frame(f)
        except Exception as e:
            print(f"skip #{f['id']}: {e}")
            continue
        H, W = img.shape
        ha, dec = math.radians(f["actual"]["ha"]), math.radians(f["actual"]["dec"])
        c = np.array([math.cos(dec) * math.cos(ha), -math.cos(dec) * math.sin(ha), math.sin(dec)])
        n = np.array([-math.sin(dec) * math.cos(ha), math.sin(dec) * math.sin(ha), math.cos(dec)])
        e = np.cross(n, c)
        z = v @ c
        with np.errstate(divide="ignore", invalid="ignore"):
            x, y = (v @ e) / z, (v @ n) / z
        m = valid & (z > 0.2) & (np.abs(x) < hx) & (np.abs(y) < hy)
        px = ((1 - x[m] / hx) / 2 * (W - 1)).round().astype(int)
        py = ((1 - y[m] / hy) / 2 * (H - 1)).round().astype(int)
        w = (1 - np.abs(x[m] / hx)) * (1 - np.abs(y[m] / hy)) + 1e-3
        acc[m] += img[py, px] * w
        wsum[m] += w
    out = np.zeros((size, size, 4), np.uint8)
    has = wsum > 0
    val = np.zeros_like(acc)
    val[has] = acc[has] / wsum[has]
    out[..., 0] = out[..., 1] = out[..., 2] = (val * 255).astype(np.uint8)
    out[..., 3] = np.where(has, 255, 0)
    tmp = OUT + ".tmp.png"
    Image.fromarray(out, "RGBA").save(tmp)
    os.replace(tmp, OUT)
    info = {"frames": len(frames), "epoch": time.time(), "size": size}
    with open(os.path.join(pl.PANO_DIR, "render.js"), "w") as fh:
        fh.write("window.PANO_RENDER = " + json.dumps(info) + ";\n")
    return len(frames)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=900)
    ap.add_argument("--watch", action="store_true")
    a = ap.parse_args()
    if not a.watch:
        print(render(a.size), "frames rendered")
        sys.exit()
    last = None
    while True:
        try:
            st = pl.load_state()
            key = tuple(f["id"] for f in st["frames"] if f["status"] == "acquired")
            if key != last:
                print(time.strftime("%H:%M:%S"), render(a.size), "frames rendered", flush=True)
                last = key
        except Exception as e:
            print("render error:", e, flush=True)
        time.sleep(10)
