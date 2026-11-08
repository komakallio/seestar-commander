"""Cable-safe single slew, with IP-camera frames before and after.

usage: goto.py --alt 60 --az 180            (converted to HA/Dec)
       goto.py --ha 0 --dec 30
       goto.py --where                      (just print position)
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl
from scope import Scope, camsnap

ap = argparse.ArgumentParser()
ap.add_argument("--alt", type=float)
ap.add_argument("--az", type=float)
ap.add_argument("--ha", type=float)
ap.add_argument("--dec", type=float)
ap.add_argument("--where", action="store_true")
ap.add_argument("--no-cam", action="store_true")
ap.add_argument("--track", action="store_true", help="leave tracking on after the slew")
a = ap.parse_args()

s = Scope()
pos = s.position()
print("now:", {k: round(v, 2) for k, v in pos.items()})
if a.where:
    sys.exit()
if a.alt is not None:
    ha, dec = pl.altaz_to_hadec(a.alt, a.az)
else:
    ha, dec = a.ha, a.dec
print(f"target HA {ha:.2f} Dec {dec:.2f}")
stamp = time.strftime("%H%M%SZ", time.gmtime())
cam = os.path.join(pl.ROOT, "data", "cam")
if not a.no_cam:
    print("cam before:", camsnap(os.path.join(cam, f"goto_{stamp}_before.jpg")))
try:
    pos = s.slew_hadec(ha, dec, track_after=a.track)
except KeyboardInterrupt:
    s.abort()
    raise
print("after:", {k: round(v, 2) for k, v in pos.items()})
if not a.no_cam:
    print("cam after:", camsnap(os.path.join(cam, f"goto_{stamp}_after.jpg")))
