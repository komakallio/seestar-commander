"""Small, fast astronomy helpers (no IERS tables, no network). Accuracy is far better than the
pointing of the mount: GMST to ~0.1 s, Sun to ~0.01 deg. Times are unix seconds (UTC)."""
import math

D2R = math.pi / 180.0
SIDEREAL_DEG_S = 360.98564736629 / 86400.0      # HA rate of a fixed RA, deg per second


def jd(t):
    return t / 86400.0 + 2440587.5


def gmst_deg(t):
    d = jd(t) - 2451545.0
    T = d / 36525.0
    return (280.46061837 + 360.98564736629 * d + 0.000387933 * T * T) % 360.0


def lst_deg(t, lon):
    return (gmst_deg(t) + lon) % 360.0


def wrap180(x):
    return (x + 180.0) % 360.0 - 180.0


def hadec_to_altaz(ha, dec, lat):
    h, d, p = ha * D2R, dec * D2R, lat * D2R
    sa = math.sin(p) * math.sin(d) + math.cos(p) * math.cos(d) * math.cos(h)
    alt = math.asin(max(-1.0, min(1.0, sa)))
    az = math.atan2(-math.cos(d) * math.sin(h),
                    math.sin(d) * math.cos(p) - math.cos(d) * math.cos(h) * math.sin(p))
    return alt / D2R, (az / D2R) % 360.0


def radec_to_altaz(ra, dec, t, lat, lon):
    return hadec_to_altaz(wrap180(lst_deg(t, lon) - ra), dec, lat)


def sun_radec(t):
    """Low-precision apparent Sun (Astronomical Almanac), degrees."""
    n = jd(t) - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360
    g = ((357.528 + 0.9856003 * n) % 360) * D2R
    lam = (L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) * D2R
    eps = (23.439 - 0.0000004 * n) * D2R
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam)) / D2R % 360
    dec = math.asin(math.sin(eps) * math.sin(lam)) / D2R
    return ra, dec


def sun_alt(t, lat, lon):
    ra, dec = sun_radec(t)
    return radec_to_altaz(ra, dec, t, lat, lon)[0]


def separation(ra1, dec1, ra2, dec2):
    """Great-circle distance, degrees."""
    a1, d1, a2, d2 = ra1 * D2R, dec1 * D2R, ra2 * D2R, dec2 * D2R
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2)
    return math.acos(max(-1.0, min(1.0, c))) / D2R


def icrs_to_jnow(ra, dec, t):
    """J2000/ICRS -> JNow (FK5 of date), via astropy (slow-ish: call once per target)."""
    from astropy.coordinates import FK5, SkyCoord
    from astropy.time import Time
    import astropy.units as u
    c = SkyCoord(ra * u.deg, dec * u.deg, frame="icrs").transform_to(FK5(equinox=Time(t, format="unix")))
    return c.ra.deg, c.dec.deg


def jnow_to_icrs(ra, dec, t):
    from astropy.coordinates import FK5, SkyCoord
    from astropy.time import Time
    import astropy.units as u
    c = SkyCoord(ra * u.deg, dec * u.deg, frame=FK5(equinox=Time(t, format="unix"))).transform_to("icrs")
    return c.ra.deg, c.dec.deg
