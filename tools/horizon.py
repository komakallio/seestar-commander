"""Extract the horizon line (highest non-sky altitude per azimuth) from the stitched panorama.

Scans each azimuth column from the zenith down. Sky brightness is tracked with a running
median; the first pixel where the star-filtered image departs from it by more than
--drop (darker: trees, walls) or --rise (brighter: lit structures), persistently for
--persist degrees, marks the horizon. The line is then median-filtered over azimuth.

Writes data/pano/out/horizon.json, horizon_overlay.png.
usage: horizon.py [--step 0.5] [--drop 0.25] [--rise 0.6]
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import median_filter, grey_opening, gaussian_filter, uniform_filter

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl

OUT = os.path.join(pl.PANO_DIR, "out")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=float, default=0.5, help="output azimuth step, deg")
    ap.add_argument("--drop", type=float, default=0.25, help="relative darkening that counts as foreground")
    ap.add_argument("--rise", type=float, default=0.6, help="relative brightening that counts as foreground")
    ap.add_argument("--persist", type=float, default=1.0, help="deg the departure must last")
    ap.add_argument("--start-alt", type=float, default=80.0, help="scan starts here")
    ap.add_argument("--star-px", type=int, default=5, help="grey-opening size that removes stars")
    ap.add_argument("--smooth-px", type=float, default=1.5)
    ap.add_argument("--bg-px", type=float, default=25, help="sky model blur, px")
    ap.add_argument("--branch-drop", type=float, default=0.10)
    ap.add_argument("--branch-win", type=int, default=15)
    ap.add_argument("--branch-frac", type=float, default=0.30)
    a = ap.parse_args()

    pano = np.load(os.path.join(OUT, "pano_lin.npy"))
    meta = json.load(open(os.path.join(OUT, "pano_meta.json")))
    H, W = pano.shape
    dpp = (meta["alt_max"] - meta["alt_min"]) / H  # deg per pixel (same both axes)
    raw = np.nan_to_num(pano, nan=0.0)
    floor = np.percentile(raw, 1)
    row_of = lambda alt: int(round((meta["alt_max"] - alt) / dpp))
    r0 = row_of(a.start_alt)
    persist = max(1, int(round(a.persist / dpp)))
    win = int(round(4 / dpp))

    # (1) solid foreground: strong departure from the running sky median (stars removed
    #     with a 7x7 median, which also removes thin branches)
    img = median_filter(raw, size=(7, 7)) - floor + 1.0
    hz1 = np.full(W, H - 1)
    for x in range(W):
        col = img[:, x]
        hist = list(col[max(0, r0 - win):r0 + 1])
        run = 0
        for r in range(r0, H):
            sky = np.median(hist[-win:])
            v = col[r]
            if (v < sky * (1 - a.drop)) or (v > sky * (1 + a.rise)):
                run += 1
                if run >= persist:
                    hz1[x] = r - persist + 1
                    break
            else:
                run = 0
                hist.append(v)

    # (2) leafless branches: thin dark texture against the large-scale sky. Stars are
    #     removed by a grey opening (bright points only), the sky model is a broad blur,
    #     so smooth brightness steps between frames don't count.
    op = gaussian_filter(grey_opening(raw, size=(a.star_px, a.star_px)), a.smooth_px) - floor + 1.0
    bg = gaussian_filter(op, a.bg_px)
    dark = (op < bg * (1 - a.branch_drop)).astype(np.float32)
    frac = uniform_filter(dark, size=a.branch_win)
    branch = frac > a.branch_frac
    hz2 = np.full(W, H - 1)
    for x in range(W):
        hits = np.nonzero(branch[r0:, x])[0]
        if hits.size:
            hz2[x] = r0 + hits[0]
    hz_row = np.minimum(hz1, hz2)
    np.save(os.path.join(OUT, "branch_mask.npy"), branch)
    hz_alt = meta["alt_max"] - (hz_row + 0.5) * dpp
    hz_alt = median_filter(hz_alt, size=max(3, int(round(1.0 / dpp)) | 1), mode="wrap")
    az_px = (meta["left_edge_az"] + (np.arange(W) + 0.5) / W * 360) % 360
    order = np.argsort(az_px)
    az_s, alt_s = az_px[order], hz_alt[order]
    out_az = np.arange(0, 360, a.step)
    # max within each output bin (conservative: the highest obstruction)
    out_alt = []
    for z in out_az:
        d = np.abs((az_s - z + 180) % 360 - 180)
        out_alt.append(float(alt_s[d <= a.step / 2 + 1e-9].max()))
    res = {"az": out_az.tolist(), "alt": [round(x, 2) for x in out_alt],
           "params": vars(a), "source": "stitched night panorama 2026-11-08"}
    json.dump(res, open(os.path.join(OUT, "horizon.json"), "w"), indent=0)

    prev = Image.open(os.path.join(OUT, "pano_preview.png")).convert("RGB")
    d = ImageDraw.Draw(prev)
    pts = [(x, int(hz_row_s)) for x, hz_row_s in enumerate((meta["alt_max"] - hz_alt) / dpp)]
    d.line(pts, fill=(255, 60, 60), width=3)
    for alt in (0, 10, 20, 30, 45, 60):
        y = int((meta["alt_max"] - alt) / dpp)
        d.line([(0, y), (W, y)], fill=(80, 140, 255), width=1)
        d.text((4, y - 12), f"{alt}", fill=(80, 140, 255))
    for az in range(0, 360, 45):
        x = int(((az - meta["left_edge_az"]) % 360) / 360 * W)
        d.line([(x, 0), (x, H)], fill=(80, 140, 255), width=1)
        d.text((x + 3, 3), {0: "N", 90: "E", 180: "S", 270: "W"}.get(az, str(az)), fill=(255, 255, 0))
    prev.save(os.path.join(OUT, "horizon_overlay.png"))
    print(f"horizon: alt min {min(out_alt):.1f} max {max(out_alt):.1f} median {np.median(out_alt):.1f}")


if __name__ == "__main__":
    main()
