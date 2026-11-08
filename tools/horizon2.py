"""Horizon v2: ground-connected foreground segmentation of the (flat-fielded) panorama.

1. Remove stars with a grey opening (bright points/thin bright trails), smooth lightly.
2. Local sky level: 80th-percentile filter at a scale of a few degrees, so it follows
   the real sky gradient but sits on the sky side of tree edges.
3. Foreground = darker than sky by --dark (trees, branches, walls) or brighter by
   --bright over a sizeable area (lit structures). Small gaps closed so sparse
   leafless branches join up.
4. Keep only foreground connected to the ground (a band below --ground-alt is forced
   foreground), so noise blobs in open sky can never count.
5. Horizon(az) = topmost kept pixel; dense output + simplified point list for editing.

Writes data/pano/out/horizon2.json, horizon2_mask.png, horizon2_overlay.png
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl

OUT = os.path.join(pl.PANO_DIR, "out")


def rdp(points, eps):
    """Ramer-Douglas-Peucker simplification of an (N,2) polyline."""
    if len(points) < 3:
        return points
    a, b = points[0], points[-1]
    ab = b - a
    n = np.hypot(*ab) or 1e-9
    d = np.abs(ab[0] * (points[:, 1] - a[1]) - ab[1] * (points[:, 0] - a[0])) / n
    i = int(np.argmax(d))
    if d[i] > eps:
        left = rdp(points[:i + 1], eps)
        right = rdp(points[i:], eps)
        return np.vstack([left[:-1], right])
    return np.vstack([a, b])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dark", type=float, default=0.10)
    ap.add_argument("--bright", type=float, default=0.6)
    ap.add_argument("--sky-deg", type=float, default=3.0, help="sky-level filter size")
    ap.add_argument("--close-px", type=int, default=5)
    ap.add_argument("--ground-alt", type=float, default=3.0)
    ap.add_argument("--simplify", type=float, default=0.15, help="deg tolerance for the edit points")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    pano = np.load(os.path.join(OUT, "pano_lin.npy"))
    meta = json.load(open(os.path.join(OUT, "pano_meta.json")))
    H, W = pano.shape
    dpp = (meta["alt_max"] - meta["alt_min"]) / H
    raw = np.nan_to_num(pano, nan=np.nanmedian(pano))
    op = ndi.gaussian_filter(ndi.grey_opening(raw, size=(5, 5)), 1.2)
    # sky level: percentile filter on a 2x downsample (speed), wrap in azimuth
    k = max(3, int(round(a.sky_deg / dpp / 2)) | 1)
    small = op[::2, ::2]
    pad = k
    padded = np.concatenate([small[:, -pad:], small, small[:, :pad]], axis=1)  # azimuth wrap
    sky_small = ndi.percentile_filter(padded, 80, size=k, mode="nearest")[:, pad:-pad]
    sky = ndi.zoom(sky_small, (H / small.shape[0], W / small.shape[1]), order=1)[:H, :W]
    sky = np.maximum(sky, 1e-3)
    ratio = op / sky
    if a.stats:
        r = ratio[int((90 - 80) / dpp):int((90 - 60) / dpp), :]
        print("open-sky ratio percentiles 0.1/1/5/50:", np.percentile(r, [0.1, 1, 5, 50]).round(3))
    dark = ratio < 1 - a.dark
    bright = ndi.binary_opening(ratio > 1 + a.bright, structure=np.ones((7, 7)))
    fg = dark | bright
    fg = ndi.binary_closing(fg, structure=np.ones((a.close_px, a.close_px)))
    ground_row = int((meta["alt_max"] - a.ground_alt) / dpp)
    fg[ground_row:, :] = True
    lab, n = ndi.label(fg, structure=np.ones((3, 3)))
    # azimuth wrap: merge labels touching both image edges
    keep_labels = set(np.unique(lab[ground_row:, :])) - {0}
    left, right = lab[:, 0], lab[:, -1]
    changed = True
    while changed:
        changed = False
        for l, r in zip(left, right):
            if l and r and ((l in keep_labels) != (r in keep_labels)):
                keep_labels |= {l, r}
                changed = True
    mask = np.isin(lab, list(keep_labels))
    top = np.argmax(mask, axis=0).astype(float)
    # columns with nothing above the forced ground band (e.g. at the image wrap): fill
    bad = top >= ground_row - 1
    if bad.any() and (~bad).any():
        idx = np.arange(W)
        top[bad] = np.interp(idx[bad], np.concatenate([idx[~bad] - W, idx[~bad], idx[~bad] + W]),
                             np.tile(top[~bad], 3))
    top = top.round().astype(int)
    hz_alt = meta["alt_max"] - (top + 0.5) * dpp

    az_cols = (meta["left_edge_az"] + (np.arange(W) + 0.5) / W * 360) % 360
    order = np.argsort(az_cols)
    dense = np.stack([az_cols[order], hz_alt[order]], 1)
    simple = rdp(dense, a.simplify)
    res = {"dense_az": dense[:, 0].round(3).tolist(), "dense_alt": dense[:, 1].round(2).tolist(),
           "points": simple.round(2).tolist(), "params": vars(a)}
    json.dump(res, open(os.path.join(OUT, "horizon2.json"), "w"))
    Image.fromarray((mask * 255).astype(np.uint8)).save(os.path.join(OUT, "horizon2_mask.png"))
    prev = Image.open(os.path.join(OUT, "pano_preview.png")).convert("RGB")
    d = ImageDraw.Draw(prev)
    d.line([(x, int(top[x])) for x in range(W)], fill=(255, 60, 60), width=2)
    prev.save(os.path.join(OUT, "horizon2_overlay.png"))
    print(f"{len(simple)} edit points; horizon alt min {hz_alt.min():.1f} max {hz_alt.max():.1f}")


if __name__ == "__main__":
    main()
