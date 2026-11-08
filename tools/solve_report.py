"""Compare solved frame centres (J2000 -> alt/az at DATE-OBS) with mount-reported pointing."""
import json, math, os, subprocess, sys
import numpy as np
from astropy.coordinates import SkyCoord, AltAz, EarthLocation
from astropy.io import fits
from astropy.time import Time
import astropy.units as u
sys.path.insert(0, os.path.dirname(__file__))
import panolib as pl
PROJ = os.environ["PROJECTIONS_DIR"]
loc = EarthLocation(lat=pl.LAT * u.deg, lon=pl.LON * u.deg, height=40 * u.m)
summ = json.load(open(os.path.join(pl.PANO_DIR, "solve.json")))
st = pl.load_state(); fr = {f["id"]: f for f in st["frames"]}
rows = []
for k, r in sorted(summ.items(), key=lambda kv: int(kv[0])):
    if not r["solved"]:
        continue
    f = fr[int(k)]
    g = json.loads(subprocess.run(["node", "tools/solution_grid.ts", os.path.join(pl.PANO_DIR, r["solution"]), "2000"],
                                  capture_output=True, text=True, env={**os.environ}).stdout)
    v = g["center"]
    ra, dec = math.degrees(math.atan2(v[1], v[0])) % 360, math.degrees(math.asin(v[2]))
    h = fits.getheader(os.path.join(pl.PANO_DIR, f["file"]))
    t = Time(h["DATE-OBS"]) + h["EXPTIME"] / 2 * u.s
    aa = SkyCoord(ra * u.deg, dec * u.deg, frame="icrs").transform_to(AltAz(obstime=t, location=loc, pressure=0 * u.hPa))
    da = aa.alt.deg - f["actual"]["alt"]; dz = ((aa.az.deg - f["actual"]["az"] + 180) % 360 - 180) * math.cos(math.radians(aa.alt.deg))
    rows.append((int(k), r["stars"], r["rms"], aa.alt.deg, aa.az.deg, da, dz))
    print(f"#{int(k):3d} stars {r['stars']:4d} rms {r['rms']:.2f}  solved alt {aa.alt.deg:6.2f} az {aa.az.deg:7.2f}  "
          f"minus mount: dAlt {da:+.2f}  dAz*cos {dz:+.2f}")
