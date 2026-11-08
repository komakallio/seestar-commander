"""Write data/pano/out/points.js for web/horizon_editor.html from horizon2.json.

usage: editor_points.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl

OUT = os.path.join(pl.PANO_DIR, "out")
h = json.load(open(os.path.join(OUT, "horizon2.json")))
meta = json.load(open(os.path.join(OUT, "pano_meta.json")))
pts, seen = [], set()
for az, alt in sorted([[round(a % 360, 2), b] for a, b in h["points"]]):
    if round(az, 1) in seen:
        continue
    seen.add(round(az, 1))
    pts.append([az, alt])
dense = [[round(a, 2), b] for a, b in zip(h["dense_az"][::2], h["dense_alt"][::2])]
with open(os.path.join(OUT, "points.js"), "w") as fh:
    fh.write("window.HZ_POINTS = " + json.dumps(pts) + ";\n")
    fh.write("window.HZ_AUTO = " + json.dumps(dense) + ";\n")
    fh.write("window.HZ_META = " + json.dumps(meta) + ";\n")
print(f"{len(pts)} points -> {OUT}/points.js")
