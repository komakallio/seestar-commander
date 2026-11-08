"""One wide-camera exposure at the current pointing. Never moves the mount.

usage: test_exposure.py OUT.fits [exposure_s] [gain]
"""
import os
import sys
import time
import numpy as np
from astropy.io import fits
from alpaca.camera import Camera
from alpaca.telescope import Telescope
from alpaca.focuser import Focuser

sys.path.insert(0, os.path.dirname(__file__))
import config
HOST = config.SEESTAR
out = sys.argv[1]
exp = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
gain = int(sys.argv[3]) if len(sys.argv) > 3 else 80

t, c, f = Telescope(HOST, 0), Camera(HOST, 1), Focuser(HOST, 1)
for d in (t, c, f):
    d.Connected = True
c.Gain = gain

alt, az, fpos = t.Altitude, t.Azimuth, f.Position
t0 = time.time()
date_obs = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
c.StartExposure(exp, True)
while not c.ImageReady:
    time.sleep(0.2)
t1 = time.time()
img = np.array(c.ImageArray)
t2 = time.time()
print(f"exposed+ready {t1 - t0:.1f}s, download {t2 - t1:.1f}s, array {img.shape} {img.dtype}, "
      f"min {img.min()} median {np.median(img):.0f} max {img.max()}")

# Alpaca ImageArray is [x][y]; FITS wants [y][x]
data = img.T.astype(np.uint16)
h = fits.Header()
h["DATE-OBS"] = date_obs
h["EXPTIME"] = exp
h["GAIN"] = gain
h["BAYERPAT"] = "GRBG" if (c.BayerOffsetX, c.BayerOffsetY) == (1, 0) else "UNKNOWN"
h["XBAYROFF"], h["YBAYROFF"] = c.BayerOffsetX, c.BayerOffsetY
h["MNT_ALT"], h["MNT_AZ"] = alt, az
h["FOCUSPOS"] = fpos
h["INSTRUME"] = c.Name
fits.writeto(out, data, h, overwrite=True)
print("wrote", out)
