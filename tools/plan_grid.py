"""Create data/pano/state.json + status.js with the planned grid (no SeeStar contact).

usage: plan_grid.py [--rows 5,25,45,65] [--spacing 22] [--no-zenith] [--force]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl

ap = argparse.ArgumentParser()
ap.add_argument("--rows", default="5,25,45,65")
ap.add_argument("--spacing", type=float, default=22.0)
ap.add_argument("--no-zenith", action="store_true")
ap.add_argument("--force", action="store_true", help="overwrite existing state with acquired frames")
a = ap.parse_args()

if os.path.exists(pl.STATE) and not a.force:
    st = pl.load_state()
    if any(f["status"] != "planned" for f in st["frames"]):
        sys.exit("state has acquired frames; use --force to replace")

rows = tuple(float(x) for x in a.rows.split(","))
frames = pl.plan_grid(rows, a.spacing, not a.no_zenith)
pl.save_state(pl.new_state(frames), f"plan: {len(frames)} frames, rows {rows}")
print(f"{len(frames)} frames; FOV {pl.FOV_SHORT:.1f} x {pl.FOV_LONG:.1f} deg")
for f in frames:
    p = f["planned"]
    print(f"  {f['id']:3d} row {f['row']} alt {p['alt']:5.1f} az {p['az']:6.1f}  HA {p['ha']:7.2f} Dec {p['dec']:6.2f}")
