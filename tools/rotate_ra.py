"""Rotate the RA (primary) axis by an explicit amount with MoveAxis. Positive = increasing HA
= clockwise on the author's wedge (verify on yours). Steps of <= --step deg, camera frame after each.

usage: rotate_ra.py DEG [--rate 5] [--step 45]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from alpaca.telescope import TelescopeAxes
from scope import Scope, camsnap

ap = argparse.ArgumentParser()
ap.add_argument("deg", type=float)
ap.add_argument("--rate", type=float, default=5.0)
ap.add_argument("--step", type=float, default=45.0)
a = ap.parse_args()

s = Scope()
AX = TelescopeAxes.axisPrimary
sign = 1 if a.deg > 0 else -1
left = abs(a.deg)
total = 0.0
prev = s.position()
print(f"start HA {prev['ha']:.1f} Dec {prev['dec']:.2f}", flush=True)
while left > 0.5:
    d = min(a.step, left)
    try:
        s.t.MoveAxis(AX, sign * a.rate)
        time.sleep(d / a.rate)
    finally:
        s.t.MoveAxis(AX, 0.0)
    time.sleep(1.5)
    p = s.position()
    moved = ((p["ha"] - prev["ha"] + 180) % 360 - 180)
    total += moved
    left -= abs(moved) if abs(moved) > 0.2 else d
    print(f"  moved {moved:+.1f} (total {total:+.1f})  HA {p['ha']:.1f} Dec {p['dec']:.2f} alt {p['alt']:.1f} az {p['az']:.1f}", flush=True)
    if abs(p["dec"] - prev["dec"]) > 1:
        sys.exit("Dec changed unexpectedly, stopping")
    if moved * sign < 0:
        sys.exit("moved the wrong way, stopping")
    prev = p
stamp = time.strftime("%H%M%SZ", time.gmtime())
print("cam:", camsnap(f"data/cam/rotate_{stamp}.jpg"))
