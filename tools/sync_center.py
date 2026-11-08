"""Sync the mount to a plate-solved J2000 position, then move to a target (default M31).

usage: sync_center.py SOLVED_RA_DEG SOLVED_DEC_DEG ["00h42m44s +41d16m09s"]
"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from astropy.coordinates import SkyCoord, FK5
from astropy.time import Time
import astropy.units as u
from scope import Scope, lst_deg, wrap180
solved_ra, solved_dec = float(sys.argv[1]), float(sys.argv[2])
now = Time.now(); jnow = FK5(equinox=now)
true_now = SkyCoord(solved_ra*u.deg, solved_dec*u.deg, frame='icrs').transform_to(jnow)
target = SkyCoord(sys.argv[3] if len(sys.argv) > 3 else '00h42m44.3s +41d16m09s', frame='icrs').transform_to(jnow)
s = Scope()
p = s.position(); u0 = s.ha_unwrapped()
print("mount before sync", round(p['ra_h']*15,3), round(p['dec'],3), "unwrapped", round(u0,2))
s.t.SyncToCoordinates(true_now.ra.hour, true_now.dec.deg)
time.sleep(1); u1 = s.ha_unwrapped(); p = s.position()
print("after sync", round(p['ra_h']*15,3), round(p['dec'],3), "unwrapped", round(u1,2))
ha = wrap180(lst_deg() - target.ra.deg)
p = s.move_hadec(ha, target.dec.deg); s.t.Tracking = True
print("at target (JNow) HA", round(ha,2), "mount", round(p['ra_h']*15,3), round(p['dec'],3), "alt", round(p['alt'],1))
