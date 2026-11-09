"""Satellite position in the mount frame of an equatorial (wedge) mount.

SGP4 (Skyfield) -> topocentric RA/Dec of date (the mount's JNow) -> hour angle -> unwrapped hour
angle of the RA axis. Refraction is not applied: the mount model comes from plate-solve syncs.

An HA/Dec path does not depend on when it is flown: the RA axis turns with the Earth, so a pass can
be replayed with the mount at any time and the axes move exactly as they would on the night.
"""
from dataclasses import dataclass

import numpy as np


def wrap180(x):
    return (np.asarray(x, float) + 180.0) % 360.0 - 180.0


@dataclass
class Site:
    lat: float
    lon: float
    elevation_m: float = 0.0

    def topos(self):
        from skyfield.api import wgs84
        return wgs84.latlon(self.lat, self.lon, elevation_m=self.elevation_m)


_TS = None


def timescale():
    global _TS
    if _TS is None:
        from skyfield.api import load
        _TS = load.timescale()
    return _TS


def times(unix):
    """Skyfield Time for unix seconds (scalar or array)."""
    return timescale().utc(1970, 1, 1, 0, 0, np.asarray(unix, float))


def track(sat, site, unix):
    """Arrays over unix times: ra, dec (deg, of date), ha (wrapped), alt, az, range_km."""
    t = times(unix)
    pos = (sat - site.topos()).at(t)
    ra, dec, dist = pos.radec(epoch="date")
    alt, az, _ = pos.altaz()
    last = t.gast * 15.0 + site.lon
    ra_deg = ra.hours * 15.0
    return {"ra": ra_deg, "dec": dec.degrees, "ha": wrap180(last - ra_deg),
            "alt": alt.degrees, "az": az.degrees, "range_km": dist.km}


def unwrap_ha(ha):
    """Continuous HA along a path (no 360 jumps), starting in [-180, 180)."""
    return np.unwrap(np.asarray(ha, float), period=360.0)


def rates(x, step):
    """First and second time derivatives of a uniformly sampled series (deg/s, deg/s^2)."""
    v = np.gradient(x, step)
    return v, np.gradient(v, step)
