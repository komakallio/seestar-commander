"""Package the stitched panorama + horizon as a Stellarium landscape and NINA horizon file.

Outputs in data/pano/out/:
  stellarium/<id>/landscape.ini, panorama.png (sky transparent), horizon.txt
  <id>.zip                         Stellarium: Configuration > Sky and viewing options >
                                   Landscape > Add/remove landscapes > Install
  <id>.hrz                         NINA custom horizon (az alt per line, 1 deg step)
usage: package.py [--id my_site_pier] [--name "..."] [--author "..."]
"""
import argparse
import json
import os
import shutil
import sys
import zipfile

import numpy as np
from PIL import Image
from scipy.ndimage import median_filter

sys.path.insert(0, os.path.dirname(__file__))
import config
import panolib as pl

OUT = os.path.join(pl.PANO_DIR, "out")


def dms(x):
    s = "+" if x >= 0 else "-"
    x = abs(x)
    d = int(x)
    m = int((x - d) * 60)
    sec = (x - d - m / 60) * 3600
    return f"{s}{d}d{m}'{sec:.1f}\""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="seestar_horizon")
    ap.add_argument("--name", default=f"{config.SITE_NAME} (SeeStar horizon)")
    ap.add_argument("--author", default="")
    ap.add_argument("--alpha-margin", type=float, default=-0.4, help="deg of foreground kept above the line (negative: cut below it so the polygon line stays visible)")
    a = ap.parse_args()

    meta = json.load(open(os.path.join(OUT, "pano_meta.json")))
    h2p = os.path.join(OUT, "horizon2.json")
    if os.path.exists(h2p):  # dense v2 line -> 0.5 deg bins, max per bin (conservative)
        h2 = json.load(open(h2p))
        daz, dalt = np.array(h2["dense_az"]) % 360, np.array(h2["dense_alt"])
        bins = np.arange(0, 360, 0.5)
        alts = [float(dalt[np.abs((daz - b + 180) % 360 - 180) <= 0.25 + 1e-9].max()) for b in bins]
        hz = {"az": bins.tolist(), "alt": [round(x, 2) for x in alts]}
    else:
        hz = json.load(open(os.path.join(OUT, "horizon.json")))
    prev = np.array(Image.open(os.path.join(OUT, "pano_preview.png")))
    H, W = prev.shape
    dpp = (meta["alt_max"] - meta["alt_min"]) / H
    # per-column horizon from the 0.5 deg list (interpolated, circular)
    az_cols = (meta["left_edge_az"] + (np.arange(W) + 0.5) / W * 360) % 360
    hz_az = np.array(hz["az"] + [360.0])
    hz_alt = np.array(hz["alt"] + [hz["alt"][0]])
    col_alt = np.interp(az_cols, hz_az, hz_alt)
    rows = np.arange(H)[:, None]
    alt_rows = meta["alt_max"] - (rows + 0.5) * dpp
    alpha = (alt_rows <= col_alt[None, :] + a.alpha_margin).astype(np.uint8) * 255
    alpha = median_filter(alpha, size=3)
    rgba = np.dstack([prev, prev, prev, alpha])

    d = os.path.join(OUT, "stellarium", a.id)
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(d)
    Image.fromarray(rgba, "RGBA").save(os.path.join(d, "panorama.png"))
    # Stellarium polygon: 0.5 deg bins centred at 0.25, 0.75, ... (max per bin). Verified
    # 2026-11-08 in Stellarium 25.3: lists with a vertex at exactly az 0.0 silently fail to
    # build the polygon (no line drawn); mid-bin sampling works.
    h2 = json.load(open(os.path.join(OUT, "horizon2.json")))
    daz, dalt = np.array(h2["dense_az"]) % 360, np.array(h2["dense_alt"])
    out_pts = []
    for z in np.arange(0.25, 360, 0.5):
        sel = np.abs((daz - z + 180) % 360 - 180) <= 0.25 + 1e-9
        out_pts.append([z, float(dalt[sel].max())])
    with open(os.path.join(d, "horizon.txt"), "w") as fh:
        for z, h in out_pts:
            fh.write(f"{z:.2f} {h:.2f}\n")
    print(f"polygon: {len(out_pts)} points")
    ini = f"""[landscape]
name = {a.name}
type = spherical
author = {a.author}
description = Panorama shot with the SeeStar wide-angle camera and plate-solved; sky transparent above the measured horizon (seestar-commander).
maptex = panorama.png
maptex_top = {meta['alt_max']}
maptex_bottom = {meta['alt_min']}
angle_rotatez = 0
polygonal_horizon_list = horizon.txt
horizon_line_color = .9,.3,.3
minimal_brightness = 0.15

[location]
planet = Earth
latitude = {dms(config.LAT)}
longitude = {dms(config.LON)}
altitude = {int(config.ELEVATION)}
name = {config.SITE_NAME}
"""
    open(os.path.join(d, "landscape.ini"), "w").write(ini)
    zpath = os.path.join(OUT, a.id + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in ("landscape.ini", "panorama.png", "horizon.txt"):
            z.write(os.path.join(d, fn), f"{a.id}/{fn}")

    # NINA: 1 deg steps, max of the 0.5 deg bins (conservative)
    az = np.array(hz["az"])
    alt = np.array(hz["alt"])
    with open(os.path.join(OUT, a.id + ".hrz"), "w") as fh:
        for z in range(360):
            sel = np.abs((az - z + 180) % 360 - 180) <= 0.5 + 1e-9
            fh.write(f"{z} {alt[sel].max():.1f}\n")
    print("wrote", zpath, "and", os.path.join(OUT, a.id + ".hrz"))


if __name__ == "__main__":
    main()
